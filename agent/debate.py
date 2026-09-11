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
    def _chat(
        self,
        model: str,
        system: str,
        user: str,
        provider_order: Optional[Sequence[str]] = None,
        *,
        max_tokens: int = 700,
    ) -> llm.ChatResult:
        messages = [
            llm.ChatMessage("system", system),
            llm.ChatMessage("user", user),
        ]
        return self.router.chat(
            model=model,
            messages=messages,
            provider_order=provider_order,
            temperature=0.0,
            max_tokens=max_tokens,
            json_mode=True,
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

        The route order is resolved **per model** (``debate.model_provider_orders``
        first, then the role's order): a bundle where ``gpt-5.6-sol`` is only
        served by one worker must not be steered by the proposer's chain.
        """

        proposer_model = self.policy.proposer_model
        critic_model = self.policy.critic_model
        proposer_order = self.policy.order_for(proposer_model, role="proposer")
        critic_order = self.policy.order_for(critic_model, role="critic")

        user = _item_prompt(task, payload, vocabulary) + "\n" + _proposal_schema_hint(task)

        proposal_call = self._chat(proposer_model, SYSTEM_PROPOSER, user, proposer_order)
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
        critic_call = self._chat(critic_model, SYSTEM_CRITIC, critic_user, critic_order)
        critique = llm.extract_json(critic_call.text)
        if not isinstance(critique, dict):
            critique = {"verdict": VERDICT_COUNTER, "reason": "critic reply was not valid JSON"}

        verdict = str(critique.get("verdict", "")).lower()
        rounds = 1
        final = critique.get("final") if isinstance(critique.get("final"), dict) else proposal

        if verdict == VERDICT_AGREE:
            return DebateOutcome(
                item_id=item_id,
                task=task,
                final=dict(final or proposal),
                agreed=True,
                rounds=rounds,
                verdict=VERDICT_AGREE,
                provenance={
                    "proposer": proposal_call.as_dict(),
                    "critic": critic_call.as_dict(),
                    "final_author": critic_model,
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
            rebuttal_call = self._chat(proposer_model, SYSTEM_PROPOSER, rebuttal_user, proposer_order)
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
            final_call = self._chat(critic_model, SYSTEM_CRITIC, final_user, critic_order)
            final_verdict = llm.extract_json(final_call.text)
            if not isinstance(final_verdict, dict):
                final_verdict = {"verdict": VERDICT_COUNTER, "final": final}
            if isinstance(final_verdict.get("final"), dict):
                final = final_verdict["final"]
            agreed = str(final_verdict.get("verdict", "")).lower() == VERDICT_AGREE
            outcome = DebateOutcome(
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
                    "final_author": critic_model,
                },
                disputed=not agreed,
            )
            if outcome.disputed:
                self._record_dispute(outcome)
            return outcome

        outcome = DebateOutcome(
            item_id=item_id,
            task=task,
            final=dict(final or {}),
            agreed=False,
            rounds=rounds,
            verdict=VERDICT_COUNTER,
            provenance={
                "proposer": proposal_call.as_dict(),
                "critic": critic_call.as_dict(),
                "final_author": critic_model,
            },
            disputed=True,
        )
        self._record_dispute(outcome)
        return outcome

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
