#!/usr/bin/env python3
"""Adjudicate the rule-52 candidates with Groq and store the verdicts.

Rule 52 (Articles with Joined Nouns) teaches that the article placement in
"the X and (the) Y" decides whether the subject is singular or plural.  The
keyword matcher keeps filing such questions under rules 10/15/71, so a first
QA pass (state/rule52_verdicts.json) collected 57 candidates and adjudicated
only 12 of them with ``openai/gpt-oss-120b`` on Groq; the candidate *list* was
never persisted, only the count.

This tool reconstructs the candidate set from the current grammar pool —
grammar-shaped questions with an article-headed joined-noun construction plus
verb-number evidence (number-variant options, or an agreement verb right after
the joined nouns in the prompt) — adjudicates every candidate not yet stored,
and appends the verdicts to ``state/rule52_verdicts.json`` (this time with the
qid list, so the set can never be lost again).

The call is a single-model Groq completion: browser User-Agent (Cloudflare
403/1010 otherwise) and ``max_tokens >= 700`` (the model emits a ``reasoning``
field and truncates before ``content`` at small budgets — LESSONS L29).  The
GROQ_API_KEY is read from the git-ignored ``.env`` (or the environment); it is
never printed.

Run from the repository root:  ``python3 tools/rule52_adjudicate.py``
Resumable and idempotent: re-running only sends the candidates still pending.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import grammar as grammar_mod  # noqa: E402
from agent import indexer  # noqa: E402
from agent import llm  # noqa: E402
from agent.config import load_settings  # noqa: E402
from agent.grammar_rules import load_rule_texts  # noqa: E402
from agent.phases.subject_phase import _questions_for  # noqa: E402
from agent.util import chunked, now_iso, user_agent  # noqa: E402

STATE_PATH = ROOT / "state" / "rule52_verdicts.json"
RULE = 52
BATCH_SIZE = 20

#: article-headed joined nouns: "the famous author and (the) actor", "the
#: percentage ... and the number ..." (a couple of intervening words allowed)
JOINED_RE = re.compile(
    r"\b(?:the|a|an)\s+[a-z]+(?:\s+[a-z]+){0,3}\s+and\s+(?:(?:the|a|an)\s+)?[a-z]+",
    re.I,
)
#: singular/plural number variants inside the options
SINGULAR_RE = re.compile(r"\b(?:is|was|has|wants|does)\b", re.I)
PLURAL_RE = re.compile(r"\b(?:are|were|have|want|do)\b", re.I)
#: agreement verb right after the joined nouns in the prompt (segment-style
#: Error Detection questions keep the verb there, not in the options)
AGREEMENT_RE = re.compile(r"\b(?:is|are|was|were|has|have|am)\b", re.I)

SYSTEM = (
    "You are a meticulous SSC grammar examiner. Rule 52 is: 'When two singular nouns "
    'joined by "and" refer to the same person or form one unit, use the article only '
    "before the first noun and a singular verb. When the article is repeated before "
    "each noun, the nouns refer to separate persons or things and require a plural "
    "verb. The placement of the article determines whether the subject is singular "
    "or plural.' Reply with a single JSON object and nothing else."
)


def rule52_body() -> str:
    texts = {text.number: text for text in load_rule_texts()}
    body = texts.get(RULE)
    return (body.body if body and body.body else "") or "Articles with joined nouns."


def candidates() -> Dict[str, Any]:
    """Reconstructed rule-52 candidate set from the current grammar pool."""

    records = indexer.read_index()
    questions = _questions_for([r for r in records if r.get("subject") == "ENG"])
    pool, _types = grammar_mod.gather_grammar_questions(records, questions)
    out: Dict[str, Any] = {}
    for item in pool:
        text = item.text
        if not JOINED_RE.search(text):
            continue
        options = " | ".join(item.options)
        prompt_joined = JOINED_RE.search(item.prompt)
        if not (
            (SINGULAR_RE.search(options) and PLURAL_RE.search(options))
            or (
                prompt_joined is not None
                and AGREEMENT_RE.search(item.prompt[prompt_joined.end(): prompt_joined.end() + 40])
            )
        ):
            continue
        out[item.qid] = {
            "qid": item.qid,
            "year": item.year,
            "concept": str(item.concept_raw or ""),
            "prompt": grammar_mod.clean_prompt(item.prompt)[:600],
            "options": [grammar_mod.clean_prompt(option)[:160] for option in item.options],
        }
    return out


def load_verdicts() -> Dict[str, Any]:
    if STATE_PATH.is_file():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("verdicts"), list):
                return data
        except json.JSONDecodeError:
            pass
    return {"candidates": 0, "verdicts": []}


def _prompt(row: Dict[str, Any]) -> str:
    payload = {
        "question": row["prompt"],
        "options": row["options"],
        "rule_52_definition": rule52_body(),
    }
    return (
        "Task: rule52\n\n"
        "Item (JSON):\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n\n"
        'Reply with JSON only: {"rule52": boolean, "confidence": 0..1, "why": string}. '
        '"rule52" is true only when the question genuinely tests rule 52 (article '
        "placement deciding the verb number of joined nouns); false when it tests "
        "anything else (a different agreement point, tense, articles elsewhere, "
        "order of words, …). State a confidence you would defend."
    )


def adjudicate(
    rows: List[Dict[str, Any]],
    *,
    base_url: str,
    api_key: str,
    model: str,
    batch_size: int,
    persist=None,
) -> List[Dict[str, Any]]:
    import time

    verdicts: List[Dict[str, Any]] = []
    for batch in chunked(rows, batch_size):
        batch_payload = json.dumps({"items": batch}, ensure_ascii=False)
        messages = [
            llm.ChatMessage("system", SYSTEM),
            llm.ChatMessage(
                "user",
                "Adjudicate every question of the batch. Reply with a single JSON "
                'object: {"items": [{"qid": string, "rule52": boolean, "confidence": '
                '0..1, "why": string}]}, one entry per question, echoing each qid '
                "verbatim.\n\n"
                f"{batch_payload}",
            ),
        ]
        # Groq's free tier meters tokens-per-minute: back off when it says so
        # and re-try a couple of times instead of failing the whole run.
        result = None
        for attempt in range(4):
            try:
                result = llm.chat_completion(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    timeout=120,
                    temperature=0.0,
                    max_tokens=max(700, 300 + 150 * len(batch)),
                    extra={"response_format": {"type": "json_object"}},
                    truncation_retries=2,
                    auth_style=llm.AUTH_BEARER,
                    user_agent_value=user_agent(),
                )
                break
            except llm.LlmRateLimited as exc:
                wait = 30.0
                match = re.search(r"try again in (\d+(?:\.\d+)?)s", str(exc))
                if match:
                    wait = float(match.group(1)) + 2.0
                if attempt >= 3:
                    raise
                print(f"  rate-limited – sleeping {wait:.0f}s (attempt {attempt + 1})", flush=True)
                time.sleep(wait)
        assert result is not None
        parsed = llm.extract_json(result.text)
        items = parsed.get("items") if isinstance(parsed, dict) else None
        if not isinstance(items, list):
            raise RuntimeError(f"unparsable Groq reply: {result.text[:200]!r}")
        for entry in items:
            if not isinstance(entry, dict) or not entry.get("qid"):
                continue
            raw = json.dumps(entry, ensure_ascii=False)
            try:
                confidence = max(0.0, min(1.0, float(entry.get("confidence") or 0.0)))
            except (TypeError, ValueError):
                confidence = 0.0
            verdicts.append(
                {
                    "qid": str(entry["qid"]),
                    "raw": raw,
                    "rule52": bool(entry.get("rule52")),
                    "confidence": confidence,
                    "why": str(entry.get("why") or "")[:300],
                    "model": model,
                    "at": now_iso(),
                }
            )
        print(f"  batch done: {len(verdicts)} verdict(s) so far", flush=True)
        if persist is not None:
            persist()
        # stay under the tokens-per-minute cap between batches
        time.sleep(30.0)
    return verdicts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply-verdicts", action="store_true",
                    help="also write the confident true verdicts into "
                         "state/grammar_ai_state.json as rule-52 review vetoes")
    args = ap.parse_args()

    from agent.cli import _load_env_file

    _load_env_file()
    settings = load_settings()
    groq = settings.provider_by_name("groq")
    if groq is None:
        print("no groq route in config/settings.json", file=sys.stderr)
        return 1
    import os

    keys = llm.provider_keys(llm.resolve_providers(settings)).get("groq") or []
    if not keys:
        print("GROQ_API_KEY missing (put it in .env)", file=sys.stderr)
        return 1

    cands = candidates()
    state = load_verdicts()
    by_qid = {str(v.get("qid")): v for v in state["verdicts"]}
    pending = [row for row in cands.values() if row["qid"] not in by_qid]
    print(f"candidates        : {len(cands)} (pattern-reconstructed, see module docstring)")
    print(f"stored verdicts   : {len(by_qid)}")
    print(f"pending           : {len(pending)}")
    if not pending:
        print("nothing to adjudicate")
        return 0

    def persist() -> None:
        state["verdicts"] = list(by_qid.values())
        state["candidates"] = len(cands)
        state["candidate_qids"] = sorted(cands)
        state["pattern"] = "article-headed joined nouns + verb-number evidence (tools/rule52_adjudicate.py)"
        state["updated_at"] = now_iso()
        STATE_PATH.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")

    new_verdicts = adjudicate(
        list(pending),
        base_url=groq.base_url,
        api_key=keys[0],
        model="openai/gpt-oss-120b",
        batch_size=10,
        persist=persist,
    )
    for verdict in new_verdicts:
        by_qid[verdict["qid"]] = verdict
    persist()
    print(f"saved {len(by_qid)} verdicts -> {STATE_PATH}")

    true_qids = [
        v["qid"]
        for v in by_qid.values()
        if v.get("rule52") and float(v.get("confidence") or 0) >= 0.8
    ]
    print(f"rule52 true (conf >= 0.8): {len(true_qids)}")
    for qid in sorted(true_qids):
        print(f"  {qid}")

    if not args.apply_verdicts:
        print("(--apply-verdicts not given: grammar_ai_state.json untouched)")
        return 0

    # write review vetoes so `python -m agent.cli grammar` re-files them under
    # rule 52: a veto overrides the keyword match and names the correct rule.
    ai_state_path = ROOT / "state" / "grammar_ai_state.json"
    ai_state = {"version": 1, "score_version": grammar_mod.SCORE_VERSION, "questions": {}, "batches": {}}
    if ai_state_path.is_file():
        try:
            ai_state = json.loads(ai_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    ai_state.setdefault("questions", {})
    for qid in sorted(true_qids):
        verdict = by_qid[qid]
        ai_state["questions"][qid] = {
            "qid": qid,
            "rule": RULE,
            "confidence": float(verdict.get("confidence") or 0),
            "reason": str(verdict.get("why") or "")[:400],
            "reason_code": "",
            "keep": False,
            "veto": True,
            "proposer_model": "openai/gpt-oss-120b",
            "proposer_provider": "groq",
            "judge_model": "openai/gpt-oss-120b",
            "judge_provider": "groq",
            "same_model_fallback": True,  # single-model adjudication: strict 0.8 floor
            "verdict": "rule52_adjudication",
            "rounds": 1,
            "batch": "rule52-tool",
            "at": now_iso(),
        }
    ai_state_path.write_text(json.dumps(ai_state, indent=1), encoding="utf-8")
    print(f"wrote {len(true_qids)} rule-52 veto decision(s) -> {ai_state_path}")
    print("now run:  python -m agent.cli grammar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
