"""Verification: AI re-check of every python-extracted item.

Design
------
* Work is driven by the question index (``state/index/<subject>.<n>.jsonl``, the
  python extraction result) and **batched** (``settings.verify.batch_size``,
  default 20) so the number of requests is proportional to items / batch.
* Every batch goes through :class:`agent.debate.Debate` (proposer + critic).
* Resumable and idempotent: ``state/verify_state.json`` stores, per phase, the
  set of already-verified batch fingerprints and the cursor.  Re-running skips
  finished batches, and a correction is a *set* operation on the index record
  (``concept``/``chapter``/``topic``), so applying the same correction twice is a
  no-op.
* Corrections are appended to ``state/corrections.jsonl`` and applied to the
  index in place; ``database/`` is rebuilt from the corrected index.

When no API keys are configured the whole step is skipped with an explicit note
(``skipped_no_keys``), so an offline run still produces a complete database.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import debate as debate_mod
from . import indexer, llm, paths, router as router_mod
from .config import Settings
from .util import Log, append_jsonl, chunked, now_iso, read_json, write_json

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped_no_keys"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_TIME_LIMIT = "time_limit"
STATUS_ERROR = "error"

PHASE_SUBJECTS = {
    "phase1": ["ENG"],
    "phase2": ["GK"],
    "phase3": ["MATH"],
    "phase4": ["REAS"],
    "phase5": ["COMPUTER"],
}


@dataclass
class VerifyResult:
    status: str
    counters: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    corrections_file: Optional[Path] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "counters": self.counters,
            "notes": self.notes,
            "corrections_file": paths.rel(self.corrections_file) if self.corrections_file else None,
            "router": None,
        }


def batch_fingerprint(records: Sequence[Dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for record in records:
        h.update(str(record.get("qid")).encode("utf-8"))
        h.update(b"|")
        h.update(str(record.get("concept")).encode("utf-8"))
        h.update(b"|")
        h.update(str(record.get("chapter")).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:32]


def load_state(path: Optional[Path] = None) -> Dict[str, Any]:
    data = read_json(path or paths.VERIFY_STATE_JSON, default=None)
    if data is None:
        return {"version": 1, "created_at": now_iso(), "phases": {}}
    return data


def save_state(state: Dict[str, Any], path: Optional[Path] = None) -> Path:
    state["updated_at"] = now_iso()
    return write_json(path or paths.VERIFY_STATE_JSON, state)


def _vocabulary_for(taxonomy: Dict[str, Any], subjects: Sequence[str]) -> List[str]:
    out: List[str] = []
    for subject, body in taxonomy.get("subjects", {}).items():
        if subject not in subjects:
            continue
        for chapter in body.get("chapters", []):
            chapter_name = chapter.get("name", "")
            if chapter_name:
                out.append(f"{subject}: {chapter_name}")
            for topic in chapter.get("topics", []):
                topic_name = topic.get("name", "")
                if topic_name:
                    out.append(f"{subject}: {chapter_name} > {topic_name}")
    return out


def _item_payload(record: Dict[str, Any], question: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    payload = {
        "qid": record.get("qid"),
        "subject": record.get("subject"),
        "exam": record.get("exam"),
        "year": record.get("year"),
        "concept_raw": record.get("concept_raw"),
        "tags": record.get("tags"),
        "python_result": {
            "concept": record.get("concept"),
            "chapter": record.get("chapter"),
            "topic": record.get("topic"),
            "class_source": record.get("class_source"),
        },
    }
    if question is not None:
        payload["question"] = str(question.get("question") or "")[:1200]
    return payload


def _parse_verify_reply(reply: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(reply, dict):
        return None
    if "ok" not in reply and "concept" not in reply:
        return None
    return {
        "ok": bool(reply.get("ok", True)),
        "concept": reply.get("concept"),
        "chapter": reply.get("chapter"),
        "topic": reply.get("topic"),
        "reason": str(reply.get("reason") or "")[:400],
    }


def verify_database(
    settings: Settings,
    *,
    phase: Optional[str] = None,
    limit: int = 0,
    batch_size: Optional[int] = None,
    report_only: bool = False,
    log: Optional[Log] = None,
    papers: Optional[Sequence[Any]] = None,
) -> VerifyResult:
    """Re-check python-extracted items; resumable and idempotent."""

    logger = log or Log("verify")
    if not settings.verify.enabled:
        return VerifyResult(STATUS_SKIPPED, notes=["verify disabled in settings"])

    if not llm.keys_available():
        logger.warn("no API keys in the environment – AI verification skipped")
        return VerifyResult(
            STATUS_SKIPPED,
            notes=[
                "no API keys found (OPENAI_KEYS / DEEPSEEK_KEYS / AR_PROXY_TOKEN / JW_PROXY_TOKEN); "
                "python extraction results were kept unchanged"
            ],
        )

    records = indexer.read_index()
    if not records:
        return VerifyResult(STATUS_SKIPPED, notes=["index is empty – run `python -m agent.cli phase0`"])

    phases = [phase] if phase else list(PHASE_SUBJECTS)
    batch = batch_size or settings.verify.batch_size
    state = load_state()
    corrections: List[Dict[str, Any]] = []
    counters: Counter = Counter()
    halted_reason = ""

    router = router_mod.get_router(settings, log=logger)
    session = debate_mod.Debate(settings, router, log=logger)

    for phase_name in phases:
        subjects = PHASE_SUBJECTS.get(phase_name)
        if not subjects:
            continue
        phase_state = state.setdefault("phases", {}).setdefault(phase_name, {"done": [], "cursor": 0})
        done = set(phase_state.get("done", []))
        subset = [r for r in records if r.get("subject") in subjects]
        subset.sort(key=lambda r: (str(r.get("exam")), int(r.get("year") or 0), int(r.get("ordinal") or 0), str(r.get("qid"))))

        if limit:
            subset = subset[:limit]

        batches = list(chunked(subset, batch))
        logger.info(f"{phase_name}: {len(subset)} items in {len(batches)} batches ({len(done)} already verified)")

        for index, group in enumerate(batches):
            if router.halted:
                halted_reason = router.stats.halt_reason
                break
            fingerprint = batch_fingerprint(group)
            if fingerprint in done:
                counters["batches_skipped"] += 1
                continue
            payloads = [
                _item_payload(r, None) for r in group
            ]
            try:
                outcome = session.run(
                    item_id=fingerprint,
                    task="verify",
                    payload={"batch": payloads},
                    vocabulary=_vocabulary_for(
                        read_json(paths.TAXONOMY_JSON, default={"subjects": {}}), subjects
                    )[:200],
                )
            except router_mod.GlobalHalt as exc:
                halted_reason = str(exc)
                break
            except llm.LlmError as exc:
                logger.warn(f"{phase_name}: batch {index} failed: {exc}")
                counters["batches_failed"] += 1
                continue

            counters["batches_verified"] += 1
            phase_state["cursor"] = index + 1
            done.add(fingerprint)
            phase_state["done"] = sorted(done)
            save_state(state)

            final = outcome.final if isinstance(outcome.final, dict) else {}
            results = final.get("items") if isinstance(final.get("items"), list) else None
            if results:
                by_qid = {str(r.get("qid")): r for r in group}
                for item in results:
                    parsed = _parse_verify_reply(item if isinstance(item, dict) else {})
                    if parsed is None:
                        continue
                    qid = str(item.get("qid")) if isinstance(item, dict) else ""
                    target = by_qid.get(qid)
                    if target is None:
                        continue
                    if parsed["ok"]:
                        counters["items_confirmed"] += 1
                        continue
                    counters["items_corrected"] += 1
                    correction = {
                        "ts": now_iso(),
                        "phase": phase_name,
                        "qid": qid,
                        "before": {
                            "concept": target.get("concept"),
                            "chapter": target.get("chapter"),
                            "topic": target.get("topic"),
                        },
                        "after": {
                            "concept": parsed["concept"],
                            "chapter": parsed["chapter"],
                            "topic": parsed["topic"],
                        },
                        "reason": parsed["reason"],
                        "provenance": outcome.provenance,
                        "applied": not report_only,
                    }
                    corrections.append(correction)
            else:
                counters["batches_without_items"] += 1

        if halted_reason:
            break

    corrections_file: Optional[Path] = None
    if corrections:
        append_jsonl(paths.STATE_DIR / "corrections.jsonl", corrections)
        corrections_file = paths.STATE_DIR / "corrections.jsonl"
        if not report_only:
            counters["index_updated"] = apply_corrections(records, corrections)

    save_state(state)
    counters["corrections"] = len(corrections)
    status = STATUS_RATE_LIMITED if halted_reason else STATUS_OK
    notes: List[str] = []
    if halted_reason:
        notes.append(f"halted: {halted_reason}")
    notes.append(f"router: {json.dumps(router.stats.as_dict(), sort_keys=True)}")
    return VerifyResult(status, dict(counters), notes, corrections_file)


def apply_corrections(records: Sequence[Dict[str, Any]], corrections: Sequence[Dict[str, Any]]) -> int:
    """Apply corrections to index records in place; returns the number applied.

    Idempotent: setting the same field values twice has no additional effect, and
    only non-null corrected values overwrite the python result.
    """

    by_qid = {str(r.get("qid")): r for r in records}
    applied = 0
    changed = False
    for correction in corrections:
        record = by_qid.get(str(correction.get("qid")))
        if record is None:
            continue
        after = correction.get("after") or {}
        touched = False
        for field in ("concept", "chapter", "topic"):
            value = after.get(field)
            if value is None:
                continue
            value = str(value).strip()
            if not value:
                continue
            if record.get(field) != value:
                record[field] = value
                touched = True
        if touched:
            record["class_source"] = "ai_verified"
            record["class_confidence"] = "high"
            record["status"] = "placed" if record.get("chapter") else "unclassified"
            applied += 1
            changed = True
    if changed:
        indexer.write_index(records)
    return applied
