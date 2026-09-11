"""Two-model debate protocol.

Flow (at most ``debate.max_rounds`` rebuttal rounds, default **1**)::

    item ──> DeepSeek V4 Flash  : structured JSON proposal
             GPT-5.6 Sol        : agree | counter
             (if counter) DeepSeek V4 Flash : one rebuttal
             GPT-5.6 Sol        : FINAL verdict + provenance

Unresolved items are appended to ``state/disputes.jsonl``.  Every verdict carries
provenance (which model said what, on which provider, in how many rounds) so a
result can always be traced back to its source.

Routing
-------
Both roles walk the ``(provider, model)`` route chain configured in
``config/settings.json`` — :data:`agent.router.Router` fails a route over to the
next one and only raises :class:`~agent.router.GlobalHalt` when no route for a
model can serve the request.  A ``GlobalHalt`` means "the AI is unavailable": the
caller records ``status=ai_unavailable`` and finishes its deterministic work
instead of failing the run.

Model fallback
--------------
A route is a ``(provider, model)`` pair, so when *no provider* can serve a model
the request also **falls back across models**: the proposer tries
``debate.proposer_models`` and the critic ``debate.critic_models`` in order, and
the pair that answered is recorded in the provenance.  The two opinions stay
independent: the critic prefers a model other than the one that wrote the
proposal and only reuses it when that is the only working model left — a
``same_model_fallback`` that is flagged in the provenance (and in ``PROGRESS.md``)
instead of being hidden.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from . import llm, paths, router as router_mod
from .config import Settings
from .util import Log, append_jsonl, now_iso, read_json

SYSTEM_PROPOSER = (
    "You are a meticulous SSC exam question classifier. "
    "Reply with a single JSON object and nothing else."
)

SYSTEM_CRITIC = (
    "You are a strict reviewer of another model's classification of an SSC exam question. "
    "Verify against the supplied taxonomy vocabulary. Reply with a single JSON object and nothing else."
)

VERDICT_AGREE = "agree"
VERDICT_COUNTER = "counter"


def ordered_candidates(
    models: Sequence[str],
    *,
    first: Optional[str] = None,
    last: Optional[str] = None,
) -> List[str]:
    """De-duplicated candidate list with *first* moved to the front and *last* to the back.

    *last* is how the two roles stay independent: the critic prefers any model
    other than the one that wrote the proposal, and only reaches for it when
    nothing else can serve — the ``same_model_fallback`` case.
    """

    out: List[str] = []
    for name in models:
        value = str(name).strip()
        if value and value not in out:
            out.append(value)
    if first:
        if first in out:
            out.remove(first)
        out.insert(0, first)
    if last and last in out and last != first:
        out.remove(last)
        out.append(last)
    return out


def same_model_fallback(provenance: Dict[str, Any]) -> bool:
    """True when one model effectively spoke for both sides of the debate.

    The two opinions must stay independent, so a run where every proposer-side
    and every critic-side call was served by the *same* model is flagged rather
    than silently reported as a two-model agreement.
    """

    proposer: set = set()
    critic: set = set()
    for role in ("proposer", "rebuttal"):
        call = provenance.get(role)
        if isinstance(call, dict) and call.get("model"):
            proposer.add(str(call["model"]))
    for role in ("critic", "final"):
        call = provenance.get(role)
        if isinstance(call, dict) and call.get("model"):
            critic.add(str(call["model"]))
    return bool(proposer and critic and proposer & critic)


@dataclass
class DebateOutcome:
    item_id: str
    task: str
    final: Dict[str, Any]
    agreed: bool
    rounds: int
    verdict: str
    provenance: Dict[str, Any] = field(default_factory=dict)
    disputed: bool = False
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "task": self.task,
            "final": self.final,
            "agreed": self.agreed,
            "rounds": self.rounds,
            "verdict": self.verdict,
            "provenance": self.provenance,
            "disputed": self.disputed,
            "notes": self.notes,
        }


def _item_prompt(task: str, payload: Dict[str, Any], vocabulary: Sequence[str]) -> str:
    vocab_block = ""
    if vocabulary:
        vocab_block = (
            "\nAllowed taxonomy values (choose exactly one when the task asks for a mapping):\n"
            + "\n".join(f"- {v}" for v in vocabulary[:400])
        )
    return (
        f"Task: {task}\n\n"
        "Item (JSON):\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n"
        f"{vocab_block}\n\n"
        "Reply with JSON only. Include the keys you are asked about and a short "
        '"reason" string.'
    )


def _proposal_schema_hint(task: str) -> str:
    if task == "classify":
        return (
            'Schema: {"concept": string, "chapter": string, "topic": string|null, '
            '"subject": "REAS"|"GK"|"MATH"|"ENG"|"COMPUTER", "confidence": 0..1, "reason": string}'
        )
    if task == "verify":
        # the payload is a *batch* — every item must be answered, keyed by its qid,
        # otherwise the reply cannot be mapped back onto the index records
        return (
            'Schema: {"items": [{"qid": string, "ok": boolean, "concept": string|null, '
            '"chapter": string|null, "topic": string|null, "reason": string}], "reason": string}. '
            "Emit exactly one entry in \"items\" for every item of the batch payload, "
            "echoing each qid verbatim; set \"ok\" to true when the python_result is "
            "correct and otherwise give the corrected concept/chapter/topic."
        )
    if task == "vocab":
        return (
            'Schema: {"kind": "synonym"|"antonym"|"ows"|"idiom"|"spelling"|"homonym"|"none", '
            '"term": string|null, "reason": string}'
        )
    return 'Schema: {"result": any, "reason": string}'


class Debate:
    """Runs the proposer/critic protocol through a :class:`agent.router.Router`."""

    def __init__(self, settings: Settings, router: router_mod.Router, *, log: Optional[Log] = None) -> None:
        self.settings = settings
        self.router = router
        self.log = log or Log("debate")
        self.policy = settings.debate

    # -- helpers ----------------------------------------------------------
    def _role_models(
        self,
        role: str,
        *,
        other: Optional[str] = None,
        first: Optional[str] = None,
        last: Optional[str] = None,
    ) -> List[str]:
        """Candidate models of *role*, with the other role's served model last.

        *other* is the model that answered for the opposite side of the debate: it
        is appended (and moved last) so a debate can still finish when the only
        working model has to speak for both roles — flagged as
        ``same_model_fallback`` instead of failing the step.
        """

        models = list(self.policy.candidates_for(role))
        if other and other not in models:
            models.append(other)
        return ordered_candidates(models, first=first, last=last)

    def _chat(
        self,
        model: str,
        system: str,
        user: str,
        *,
        models: Optional[Sequence[str]] = None,
        role: str = "proposer",
        max_tokens: int = 700,
    ) -> llm.ChatResult:
        """One role call: walk the role's candidate models and provider chains."""

        messages = [
            llm.ChatMessage("system", system),
            llm.ChatMessage("user", user),
        ]
        return self.router.chat(
            model=model,
            messages=messages,
            temperature=0.0,
            max_tokens=max_tokens,
            json_mode=True,
            models=models,
            role=role,
        )

    # -- protocol ---------------------------------------------------------
    def run(
        self,
        *,
        item_id: str,
        task: str,
        payload: Dict[str, Any],
        vocabulary: Sequence[str] = (),
    ) -> DebateOutcome:
        """Propose -> criticise -> (one rebuttal) -> final verdict.

        Each call walks the **candidate models of its role** (the primary model
        first) and, for every candidate, that model's own provider order
        (``debate.model_provider_orders`` first, then the role's order): a bundle
        where ``gpt-5.6-sol`` is only served by one worker must not be steered by
        the proposer's chain, and a model with no working route must not cost the
        debate its chance to run at all.
        """

        proposer_model = self.policy.proposer_model
        critic_model = self.policy.critic_model

        user = _item_prompt(task, payload, vocabulary) + "\n" + _proposal_schema_hint(task)

        proposal_call = self._chat(
            proposer_model,
            SYSTEM_PROPOSER,
            user,
            models=self._role_models("proposer"),
            role="proposer",
        )
        # the model that actually answered — not necessarily the primary one
        proposer_used = proposal_call.model or proposer_model
        proposal = llm.extract_json(proposal_call.text)
        if not isinstance(proposal, dict) or not proposal:
            outcome = DebateOutcome(
                item_id=item_id,
                task=task,
                final={},
                agreed=False,
                rounds=0,
                verdict="unparsed",
                provenance={
                    "proposer": proposal_call.as_dict(),
                    "critic": None,
                    "reason": "proposer reply was not valid JSON",
                    "same_model_fallback": False,
                },
                disputed=True,
            )
            self._record_dispute(outcome)
            return outcome

        critic_user = (
            f"Task: {task}\n\n"
            "Item (JSON):\n"
            f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n\n"
            "Proposal from model A (JSON):\n"
            f"{json.dumps(proposal, ensure_ascii=False, sort_keys=True)}\n\n"
            "Decide whether the proposal is correct. Reply with JSON only: "
            '{"verdict":"agree"|"counter","final":{...},"reason":string}. '
            "If you agree, repeat the proposal in \"final\". If you disagree, put your own "
            "corrected object in \"final\"."
        )
        # Keep the two opinions independent: prefer any critic model other than
        # the one that wrote the proposal, and reach for it only when nothing else
        # can serve (recorded as ``same_model_fallback`` below).
        critic_call = self._chat(
            critic_model,
            SYSTEM_CRITIC,
            critic_user,
            models=self._role_models("critic", other=proposer_used, last=proposer_used),
            role="critic",
        )
        critique = llm.extract_json(critic_call.text)
        if not isinstance(critique, dict):
            critique = {"verdict": VERDICT_COUNTER, "reason": "critic reply was not valid JSON"}

        verdict = str(critique.get("verdict", "")).lower()
        rounds = 1
        final = critique.get("final") if isinstance(critique.get("final"), dict) else proposal

        if verdict == VERDICT_AGREE:
            return self._outcome(
                item_id=item_id,
                task=task,
                final=dict(final or proposal),
                agreed=True,
                rounds=rounds,
                verdict=VERDICT_AGREE,
                provenance={
                    "proposer": proposal_call.as_dict(),
                    "critic": critic_call.as_dict(),
                    "final_author": critic_call.model or critic_model,
                },
            )

        # one rebuttal round
        if self.policy.max_rounds >= 1:
            rebuttal_user = (
                f"Task: {task}\n\n"
                "Item (JSON):\n"
                f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n\n"
                "Your earlier proposal:\n"
                f"{json.dumps(proposal, ensure_ascii=False, sort_keys=True)}\n\n"
                "Reviewer objection:\n"
                f"{json.dumps(critique, ensure_ascii=False, sort_keys=True)}\n\n"
                "Either accept the correction or defend your answer. Reply with JSON only: "
                '{"accept": boolean, "final": {...}, "reason": string}.'
            )
            rebuttal_call = self._chat(
                proposer_used,
                SYSTEM_PROPOSER,
                rebuttal_user,
                models=self._role_models(
                    "proposer",
                    other=critic_call.model,
                    first=proposer_used,
                    last=critic_call.model,
                ),
                role="proposer",
            )
            rebuttal = llm.extract_json(rebuttal_call.text)
            if not isinstance(rebuttal, dict):
                rebuttal = {"accept": True, "final": final}
            rounds = 2
            if bool(rebuttal.get("accept")) and isinstance(rebuttal.get("final"), dict):
                final = rebuttal["final"]
            elif isinstance(rebuttal.get("final"), dict):
                final = rebuttal["final"]

            final_user = (
                f"Task: {task}\n\n"
                "Item (JSON):\n"
                f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n\n"
                "Proposal A:\n"
                f"{json.dumps(proposal, ensure_ascii=False, sort_keys=True)}\n\n"
                "Your earlier verdict:\n"
                f"{json.dumps(critique, ensure_ascii=False, sort_keys=True)}\n\n"
                "A's rebuttal:\n"
                f"{json.dumps(rebuttal, ensure_ascii=False, sort_keys=True)}\n\n"
                "Emit the FINAL verdict. Reply with JSON only: "
                '{"verdict":"agree"|"counter","final":{...},"reason":string}.'
            )
            final_call = self._chat(
                critic_call.model or critic_model,
                SYSTEM_CRITIC,
                final_user,
                models=self._role_models(
                    "critic",
                    other=proposer_used,
                    first=critic_call.model,
                    last=proposer_used,
                ),
                role="critic",
            )
            final_verdict = llm.extract_json(final_call.text)
            if not isinstance(final_verdict, dict):
                final_verdict = {"verdict": VERDICT_COUNTER, "final": final}
            if isinstance(final_verdict.get("final"), dict):
                final = final_verdict["final"]
            agreed = str(final_verdict.get("verdict", "")).lower() == VERDICT_AGREE
            outcome = self._outcome(
                item_id=item_id,
                task=task,
                final=dict(final or {}),
                agreed=agreed,
                rounds=rounds,
                verdict=str(final_verdict.get("verdict", "counter")).lower(),
                provenance={
                    "proposer": proposal_call.as_dict(),
                    "critic": critic_call.as_dict(),
                    "rebuttal": rebuttal_call.as_dict(),
                    "final": final_call.as_dict(),
                    "final_author": final_call.model or critic_model,
                },
            )
            if outcome.disputed:
                self._record_dispute(outcome)
            return outcome

        outcome = self._outcome(
            item_id=item_id,
            task=task,
            final=dict(final or {}),
            agreed=False,
            rounds=rounds,
            verdict=VERDICT_COUNTER,
            provenance={
                "proposer": proposal_call.as_dict(),
                "critic": critic_call.as_dict(),
                "final_author": critic_call.model or critic_model,
            },
        )
        self._record_dispute(outcome)
        return outcome

    def _outcome(
        self,
        *,
        item_id: str,
        task: str,
        final: Dict[str, Any],
        agreed: bool,
        rounds: int,
        verdict: str,
        provenance: Dict[str, Any],
    ) -> DebateOutcome:
        """Build the outcome and flag a debate that one model spoke alone."""

        provenance.setdefault("same_model_fallback", False)
        provenance["same_model_fallback"] = same_model_fallback(provenance)
        notes: List[str] = []
        if provenance["same_model_fallback"]:
            note = (
                "same_model_fallback: true – proposer and critic were both served by "
                f"{provenance.get('final_author') or 'the same model'}; "
                "only one model had a working route"
            )
            notes.append(note)
            self.log.warn(f"{task} {item_id}: {note}")
        return DebateOutcome(
            item_id=item_id,
            task=task,
            final=final,
            agreed=agreed,
            rounds=rounds,
            verdict=verdict,
            provenance=provenance,
            disputed=not agreed,
            notes=notes,
        )

    # -- disputes ---------------------------------------------------------
    def _record_dispute(self, outcome: DebateOutcome) -> None:
        payload = {"ts": now_iso(), **outcome.as_dict(), "status": "unresolved"}
        append_jsonl(paths.DISPUTES_JSONL, [payload])


def load_disputes() -> List[Dict[str, Any]]:
    data = read_json(paths.DISPUTES_JSONL.with_suffix(".json"), default=None)
    if data is not None:
        return data
    if not paths.DISPUTES_JSONL.is_file():
        return []
    out: List[Dict[str, Any]] = []
    with paths.DISPUTES_JSONL.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out
