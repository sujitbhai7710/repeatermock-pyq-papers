#!/usr/bin/env python3
"""Groq review of a sample of maths / reasoning / GK leaves.

The grammar tree has ``grammar --review``; the other subjects have nothing that
re-checks an existing placement.  This tool samples leaves per subject (the
heaviest leaves plus an evenly spaced stratum), sends their questions to Groq
with the current filing, and applies the confident corrections:

* ``ok: true``  — the filing is right, nothing changes;
* ``ok: false`` at ``confidence >= 0.8`` (single-model adjudication, so the
  strict floor applies) — a correction is appended to
  ``state/corrections.jsonl`` and applied to the index in place (the same
  mechanism ``agent.verify`` uses); a verdict below the floor is recorded but
  never applied (LESSONS L28).

After the run, rebuild the affected subject trees with
``python -m agent.cli phase N --no-ai`` (2=GK, 3=MATH, 4=REAS) and finish with
``python -m agent.cli audit``.

The GROQ_API_KEY is read from the git-ignored ``.env``; it is never printed.
Resumable per batch: re-running skips questions that already have a verdict.

Run from the repository root:  ``python3 tools/cross_subject_review.py --apply``
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import indexer  # noqa: E402
from agent import llm  # noqa: E402
from agent.config import load_settings  # noqa: E402
from agent.phases.subject_phase import _questions_for  # noqa: E402
from agent.util import chunked, now_iso, user_agent  # noqa: E402

STATE_PATH = ROOT / "state" / "cross_subject_review.json"
CORRECTIONS_PATH = ROOT / "state" / "corrections.jsonl"

SUBJECTS = ("MATH", "REAS", "GK")
#: leaves per subject (heaviest + evenly spaced), questions per leaf
LEAVES_PER_SUBJECT = 12
QUESTIONS_PER_LEAF = 15
FLOOR = 0.8

SYSTEM = (
    "You are a meticulous SSC exam database reviewer. A question is filed under a "
    "subject, a chapter and a concept of the SSC taxonomy. Judge whether the filing "
    "is correct. Reply with a single JSON object and nothing else."
)


def _sample_questions(records, questions_by_qid) -> List[Dict[str, Any]]:
    """Deterministic stratified sample across the three subjects' leaves."""

    sample: List[Dict[str, Any]] = []
    for subject in SUBJECTS:
        subset = [r for r in records if r.get("subject") == subject]
        by_chapter: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for record in subset:
            by_chapter[str(record.get("chapter") or "_unclassified")].append(record)
        ranked = sorted(
            by_chapter.items(), key=lambda item: (-len(item[1]), item[0])
        )
        picked: List[str] = []
        half = LEAVES_PER_SUBJECT // 2
        picked += [name for name, _members in ranked[:half]]
        rest = ranked[half:]
        if rest:
            step = len(rest) / float(half)
            for index in range(half):
                name, _members = rest[int(index * step)]
                if name not in picked:
                    picked.append(name)
        for name in picked:
            members = by_chapter[name]
            if len(members) > QUESTIONS_PER_LEAF:
                step = len(members) / float(QUESTIONS_PER_LEAF)
                members = [members[int(i * step)] for i in range(QUESTIONS_PER_LEAF)]
            for record in members:
                qid = str(record.get("qid") or "")
                question = questions_by_qid.get(qid) or {}
                sample.append(
                    {
                        "qid": qid,
                        "subject": record.get("subject"),
                        "chapter": record.get("chapter"),
                        "concept": record.get("concept"),
                        "concept_raw": record.get("concept_raw"),
                        "question": str(question.get("question") or "")[:700],
                        "options": [
                            str(option.get("text") or "")
                            for option in (question.get("options") or [])
                            if isinstance(option, dict)
                        ][:6],
                    }
                )
    return sample


def load_state() -> Dict[str, Any]:
    if STATE_PATH.is_file():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {"verdicts": []}


def _prompt(row: Dict[str, Any]) -> str:
    payload = {
        "qid": row["qid"],
        "filed_subject": row["subject"],
        "filed_chapter": row["chapter"],
        "filed_concept": row["concept"],
        "concept_raw": row["concept_raw"],
        "question": row["question"],
        "options": row["options"],
    }
    return (
        "Task: cross_subject_review\n\n"
        "Item (JSON):\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n\n"
        'Reply with JSON only: {"ok": boolean, "subject": string|null, '
        '"chapter": string|null, "concept": string|null, "confidence": 0..1, '
        '"why": string}. Set "ok" to true when the question genuinely belongs to '
        'the filed subject/chapter/concept; when it does not, give the corrected '
        'subject/chapter/concept (null a field to leave it empty — an empty chapter '
        'means "unclassified"). State a confidence you would defend.'
    )


def adjudicate(
    rows: List[Dict[str, Any]], *, base_url: str, api_key: str, model: str, persist=None
) -> List[Dict[str, Any]]:
    verdicts: List[Dict[str, Any]] = []
    for batch in chunked(rows, 10):
        messages = [
            llm.ChatMessage("system", SYSTEM),
            llm.ChatMessage(
                "user",
                "Judge every question of the batch. Reply with a single JSON object: "
                '{"items": [{"qid": string, "ok": boolean, "subject": string|null, '
                '"chapter": string|null, "concept": string|null, "confidence": 0..1, '
                '"why": string}]}, one entry per question, echoing each qid verbatim.\n\n'
                f"{json.dumps({'items': batch}, ensure_ascii=False)}",
            ),
        ]
        result = None
        batch_verdicts: List[Dict[str, Any]] = []
        json_mode = True
        for attempt in range(5):
            try:
                result = llm.chat_completion(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    timeout=120,
                    temperature=0.0,
                    max_tokens=max(700, 300 + 150 * len(batch)),
                    extra={"response_format": {"type": "json_object"}} if json_mode else None,
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
                if attempt >= 4:
                    raise
                print(f"  rate-limited – sleeping {wait:.0f}s (attempt {attempt + 1})", flush=True)
                time.sleep(wait)
            except llm.LlmError as exc:
                # Groq json mode answers 400 json_validate_failed when the model's
                # reply is not valid JSON: retry once without the json constraint
                if json_mode and (exc.status == 400):
                    json_mode = False
                    print("  json mode rejected the reply – retrying without it", flush=True)
                    continue
                raise
        assert result is not None
        parsed = llm.extract_json(result.text)
        items = parsed.get("items") if isinstance(parsed, dict) else None
        if not isinstance(items, list):
            raise RuntimeError(f"unparsable Groq reply: {result.text[:200]!r}")
        batch_verdicts = []
        for entry in items:
            if not isinstance(entry, dict) or not entry.get("qid"):
                continue
            try:
                confidence = max(0.0, min(1.0, float(entry.get("confidence") or 0.0)))
            except (TypeError, ValueError):
                confidence = 0.0
            batch_verdicts.append(
                {
                    "qid": str(entry["qid"]),
                    "ok": bool(entry.get("ok")),
                    "subject": entry.get("subject"),
                    "chapter": entry.get("chapter"),
                    "concept": entry.get("concept"),
                    "confidence": confidence,
                    "why": str(entry.get("why") or "")[:300],
                    "raw": json.dumps(entry, ensure_ascii=False),
                    "model": model,
                    "at": now_iso(),
                }
            )
        verdicts.extend(batch_verdicts)
        print(f"  batch done: {len(verdicts)} verdict(s) so far", flush=True)
        if persist is not None:
            persist(batch_verdicts)
        time.sleep(30.0)
    return verdicts


def _corrections(rows: List[Dict[str, Any]], verdicts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_qid = {row["qid"]: row for row in rows}
    corrections: List[Dict[str, Any]] = []
    for verdict in verdicts:
        if verdict["ok"] or verdict["confidence"] < FLOOR:
            continue
        row = by_qid.get(verdict["qid"])
        if row is None:
            continue
        after: Dict[str, Any] = {}
        if verdict.get("subject"):
            after["subject"] = str(verdict["subject"])
        after["chapter"] = str(verdict.get("chapter") or "") or ""
        after["concept"] = str(verdict.get("concept") or "") or ""
        corrections.append(
            {
                "ts": now_iso(),
                "phase": "cross_subject_review",
                "qid": verdict["qid"],
                "before": {
                    "subject": row.get("subject"),
                    "concept": row.get("concept"),
                    "chapter": row.get("chapter"),
                    "topic": row.get("topic"),
                },
                "after": after,
                "confidence": verdict["confidence"],
                "reason": verdict["why"],
                "applied": True,
            }
        )
    return corrections


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="append the confident corrections to state/corrections.jsonl "
                         "and apply them to the index")
    ap.add_argument("--max-questions", type=int, default=0,
                    help="cap the sample size (0 = the full stratified sample)")
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

    records = indexer.read_index()
    questions = _questions_for(records)
    sample = _sample_questions(records, questions)
    if args.max_questions:
        sample = sample[: args.max_questions]
    print(f"sample            : {len(sample)} questions across {SUBJECTS}")

    state = load_state()
    by_qid = {str(v.get("qid")): v for v in state["verdicts"]}
    pending = [row for row in sample if row["qid"] not in by_qid]
    print(f"stored verdicts   : {len(by_qid)}")
    print(f"pending           : {len(pending)}")

    if pending:
        def persist(batch_verdicts) -> None:
            for verdict in batch_verdicts:
                by_qid[str(verdict["qid"])] = verdict
            state["verdicts"] = list(by_qid.values())
            state["sample_size"] = len(sample)
            state["updated_at"] = now_iso()
            STATE_PATH.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")

        new_verdicts = adjudicate(
            list(pending),
            base_url=groq.base_url,
            api_key=keys[0],
            model="openai/gpt-oss-120b",
            persist=persist,
        )
        for verdict in new_verdicts:
            by_qid[str(verdict["qid"])] = verdict
        persist([])
    print(f"saved {len(by_qid)} verdicts -> {STATE_PATH}")

    wrong = [v for v in by_qid.values() if not v.get("ok")]
    confident = [v for v in wrong if float(v.get("confidence") or 0) >= FLOOR]
    print(f"ok                : {sum(1 for v in by_qid.values() if v.get('ok'))}")
    print(f"wrong             : {len(wrong)}")
    print(f"wrong & conf>=0.8 : {len(confident)}")
    for verdict in confident:
        print(f"  {verdict['qid']} -> {verdict.get('subject')}/{verdict.get('chapter')} ({verdict['confidence']})")

    if not args.apply:
        print("(--apply not given: no corrections written)")
        return 0

    corrections = _corrections(sample, confident)
    if not corrections:
        print("no corrections to apply")
        return 0
    from agent.util import append_jsonl

    append_jsonl(CORRECTIONS_PATH, corrections)
    from agent import verify as verify_mod

    applied = verify_mod.apply_corrections(records, corrections)
    print(f"appended {len(corrections)} correction(s) -> {CORRECTIONS_PATH}; index updated: {applied}")
    print("now run:  python -m agent.cli phase 2 --no-ai   (and 3/4 for the affected subjects)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
