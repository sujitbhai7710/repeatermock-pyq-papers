"""Question index.

The index holds one pointer record per **in-scope, non-Hindi** question and is
the single input every later phase reads.  Records are small (no question text)
so the index stays cheap to scan; the full text is resolved from the source
paper via :mod:`agent.tools_resolve` / ``tools/resolve.py`` when needed.

Storage layout (sharded, GitHub-pushable)
-----------------------------------------
``state/index/<subject>.<n>.jsonl``
    the records of one subject, split so that no shard exceeds
    :data:`SHARD_MAX_BYTES`; ``state/index/manifest.json`` lists every shard with
    its record count and sha256.  A single 100 MB index file would be rejected by
    GitHub (> 100 MB per file), so the sharded form is the canonical one and
    :func:`read_index` hides the split from every reader.
``state/questions_index.jsonl``
    the pre-sharding single-file layout; still *read* when no manifest exists
    (compatibility) and deleted by :func:`write_index` once the shards are
    written, so the oversized file cannot come back.
``state/index/ALL.jsonl.gz``
    optional single-file gzip copy (``PYQ_INDEX_GZIP=1`` or
    ``write_index(..., compress=True)``) for consumers that want one file.

Every record carries the full provenance chain:

* ``paper_path`` / ``test_id`` / ``year`` / ``shift`` / ``exam`` / ``paper_kind``
* ``ordinal`` (global position) and ``n`` (source position inside its section)
* ``subject`` + ``subject_source`` (``canonical`` | ``detected`` | ``unvalidated``)
* ``concept_raw`` / ``concept`` / ``chapter`` / ``topic`` + ``class_source``
* ``leaf_bucket`` (``null`` or ``_other`` — the chapter does not declare the concept)
* ``lang``, ``has_image``, ``confidence``, ``marks_pos`` / ``marks_neg``
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from . import paths, sections
from .classify import Classifier, Classification
from .config import ExamTable, Settings
from .discover import PaperRecord
from .util import Log, norm_space, write_json, write_jsonl

IMAGE_RE = re.compile(r"\[IMAGE:\s*([^\]]+)\]")

#: no shard may exceed this many bytes (GitHub rejects files > 100 MB; 25 MB
#: keeps a comfortable margin and keeps a single shard cheap to re-read)
SHARD_MAX_BYTES = 25 * 1024 * 1024

#: stable subject order used for shard naming
SUBJECT_ORDER = ("ENG", "GK", "MATH", "REAS", "COMPUTER")

IMAGE_RE = re.compile(r"\[IMAGE:\s*([^\]]+)\]")

#: terminal status of every in-scope question, used for the coverage identity
STATUS_PLACED = "placed"
STATUS_UNCLASSIFIED = "unclassified"
STATUS_FLAGGED_PAPER = "flagged_paper"
STATUS_SKIPPED_HINDI = "skipped_hindi"


def extract_image_urls(question: dict) -> List[str]:
    """All ``[IMAGE: url]`` markers in the prompt and the solution."""

    found: List[str] = []
    for key in ("question", "solution"):
        for match in IMAGE_RE.finditer(str(question.get(key) or "")):
            url = match.group(1).strip()
            if url and url not in found:
                found.append(url)
    for block in (question.get("solution_images"), question.get("images")):
        if isinstance(block, list):
            for item in block:
                if isinstance(item, dict):
                    url = str(item.get("url") or "").strip()
                    if url and url not in found:
                        found.append(url)
    return found


def strip_images(text: str) -> str:
    return IMAGE_RE.sub(" ", text or "")


@dataclass
class IndexedQuestion:
    status: str
    record: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return self.record


def build_index(
    papers: Sequence[PaperRecord],
    settings: Settings,
    exam_table: ExamTable,
    classifier: Classifier,
    *,
    log: Optional[Log] = None,
    on_paper: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build index records for every in-scope, non-Hindi question.

    Returns ``(records, paper_reports)``; records are sorted deterministically by
    ``(exam, year, shift, paper_path, ordinal)``.
    """

    logger = log or Log("indexer")
    hindi = sections.make_hindi_policy(settings.hindi)
    records: List[Dict[str, Any]] = []
    paper_reports: List[Dict[str, Any]] = []
    totals = {STATUS_PLACED: 0, STATUS_UNCLASSIFIED: 0, STATUS_FLAGGED_PAPER: 0, STATUS_SKIPPED_HINDI: 0}

    for paper in papers:
        exam = exam_table.exams.get(paper.exam)
        kind = None
        if exam is not None:
            _, kind = exam_table.kind_for_path(paper.path)
        if exam is None:
            continue

        analysis = sections.analyze_paper(
            paper.questions, exam, kind, settings.signature, hindi, paper.year
        )
        report = {
            "path": paper.path,
            "test_id": paper.test_id,
            "title": paper.title,
            "exam": paper.exam,
            "year": paper.year,
            "shift": paper.shift,
            "held_on_iso": paper.held_on_iso,
            "question_count": paper.length,
            **analysis.as_dict(),
        }
        paper_reports.append(report)

        flagged_in_paper = 0
        for index, question in enumerate(paper.questions):
            if hindi.is_hindi(question):
                totals[STATUS_SKIPPED_HINDI] += 1
                continue

            subject = analysis.subjects[index] if index < len(analysis.subjects) else None
            if not analysis.placeable or subject is None:
                totals[STATUS_FLAGGED_PAPER] += 1
                flagged_in_paper += 1
                continue

            tags = [norm_space(t) for t in (question.get("tags") or []) if norm_space(t)]
            classification: Classification = classifier.classify(
                question.get("concept"), tags, subject
            )
            status = STATUS_PLACED if classification.placed else STATUS_UNCLASSIFIED
            totals[status] += 1

            images = extract_image_urls(question)
            record = {
                "qid": str(question.get("qid", "")),
                "paper_path": paper.path,
                "test_id": paper.test_id,
                "exam": paper.exam,
                "year": paper.year,
                "shift": paper.shift,
                "held_on_iso": paper.held_on_iso,
                "paper_kind": paper.kind_id,
                "ordinal": sections.ordinal_of(index),
                "n": question.get("n"),
                "qtype": str(question.get("type", "")),
                "subject": subject,
                "subject_source": "detected" if analysis.layout_source.startswith("detected") else (
                    "unvalidated" if analysis.layout_source == "canonical-unverified" else "canonical"
                ),
                "signature_ok": analysis.signature.ok,
                "lang": "en",
                "concept_raw": norm_space(question.get("concept")),
                "tags": tags,
                "concept": classification.concept,
                "chapter": classification.chapter,
                "topic": classification.topic,
                "leaf_bucket": classification.leaf_bucket,
                "class_source": classification.source,
                "class_confidence": classification.confidence,
                "confidence": str(question.get("confidence", "")),
                "marks_pos": question.get("marks_pos"),
                "marks_neg": question.get("marks_neg"),
                # ``has_image`` keeps records self-describing; the urls are
                # re-read from the source paper by tools/resolve.py and
                # tools/mocks.py, which keeps the index about half the size.
                "has_image": bool(images),
                "correct": str(question.get("correct", "")),
                "status": status,
                "source": "python",
            }
            records.append(record)

        report["flagged_questions"] = flagged_in_paper
        if on_paper is not None:
            on_paper(paper)

    records.sort(
        key=lambda r: (r["exam"], r["year"], r["shift"] or 0, r["paper_path"], r["ordinal"])
    )
    logger.info(
        "indexed {p} placed, {u} unclassified, {f} flagged, {h} skipped_hindi".format(
            p=totals[STATUS_PLACED],
            u=totals[STATUS_UNCLASSIFIED],
            f=totals[STATUS_FLAGGED_PAPER],
            h=totals[STATUS_SKIPPED_HINDI],
        )
    )
    return records, paper_reports


def _shard_line(record: Dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"


def _subject_rank(subject: str) -> Tuple[int, str]:
    try:
        return (SUBJECT_ORDER.index(subject), "")
    except ValueError:
        return (len(SUBJECT_ORDER), subject)


def shard_subject_records(
    records: Sequence[Dict[str, Any]], *, max_bytes: int = SHARD_MAX_BYTES
) -> List[Tuple[str, str, List[str]]]:
    """Split one subject's records into ``(subject, shard_name, lines)``.

    Deterministic: the record order is preserved, a shard is closed as soon as
    adding the next record would exceed *max_bytes*, and a single oversized
    record still gets its own shard (never truncated).
    """

    shards: List[Tuple[str, str, List[str]]] = []
    by_subject: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        by_subject.setdefault(str(record.get("subject") or "UNKNOWN"), []).append(record)
    for subject in sorted(by_subject, key=_subject_rank):
        lines: List[str] = []
        size = 0
        number = 1
        for record in by_subject[subject]:
            line = _shard_line(record)
            length = len(line.encode("utf-8"))
            if lines and size + length > max_bytes:
                shards.append((subject, f"{subject.lower()}.{number}.jsonl", lines))
                lines, size, number = [], 0, number + 1
            lines.append(line)
            size += length
        if lines:
            shards.append((subject, f"{subject.lower()}.{number}.jsonl", lines))
    return shards


def qid_digest(records: Iterable[Dict[str, Any]]) -> str:
    """sha256 over the sorted qid list — the index's content identity."""

    qids = sorted(str(record.get("qid", "")) for record in records)
    return hashlib.sha256("\n".join(qids).encode("utf-8")).hexdigest()


def write_index(
    records: Sequence[Dict[str, Any]],
    path: Optional[Path] = None,
    *,
    index_dir: Optional[Path] = None,
    max_bytes: int = SHARD_MAX_BYTES,
    compress: Optional[bool] = None,
    log: Optional[Log] = None,
) -> Path:
    """Write the index as deterministic per-subject shards + manifest.

    Passing *path* keeps the single-file behaviour (used by tests and by
    ``--index-file`` style consumers); the default writes
    ``state/index/<subject>.<n>.jsonl`` and ``state/index/manifest.json`` and
    removes the legacy ``state/questions_index.jsonl``.
    """

    if path is not None:
        return write_jsonl(path, records)

    base = index_dir or paths.INDEX_DIR
    logger = log or Log("indexer")
    paths.ensure_dir(base)
    shards = shard_subject_records(records, max_bytes=max_bytes)

    written: List[Path] = []
    manifest_shards: List[Dict[str, Any]] = []
    keep = set()
    for subject, name, lines in shards:
        target = base / name
        target.write_text("".join(lines), encoding="utf-8", newline="\n")
        blob = target.read_bytes()
        keep.add(target.name)
        manifest_shards.append(
            {
                "path": paths.rel(target),
                "file": name,
                "subject": subject,
                "records": len(lines),
                "bytes": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest(),
            }
        )
        written.append(target)

    # drop shards of a previous revision (a rebuilt index may need fewer shards)
    for stale in sorted(base.glob("*.jsonl")):
        if stale.name not in keep:
            stale.unlink()
            logger.info(f"index: removed stale shard {paths.rel(stale)}")

    subjects: Dict[str, Dict[str, int]] = {}
    for entry in manifest_shards:
        bucket = subjects.setdefault(entry["subject"], {"shards": 0, "records": 0})
        bucket["shards"] += 1
        bucket["records"] += entry["records"]

    use_gzip = _gzip_enabled() if compress is None else compress
    gzip_entry: Optional[Dict[str, Any]] = None
    if use_gzip:
        gzip_entry = write_index_gzip(records, base / paths.INDEX_GZIP_JSONL.name)

    manifest = {
        "version": 1,
        "generated_by": "agent.indexer",
        "format": "jsonl-sharded",
        "max_shard_bytes": max_bytes,
        "legacy_path": paths.rel(paths.QUESTIONS_INDEX_JSONL),
        "records": len(records),
        "bytes": sum(entry["bytes"] for entry in manifest_shards),
        "qid_sha256": qid_digest(records),
        "subjects": dict(sorted(subjects.items())),
        "shards": manifest_shards,
    }
    if gzip_entry is not None:
        manifest["gzip"] = gzip_entry
    write_json(base / paths.INDEX_MANIFEST_JSON.name, manifest)

    # the oversized single-file layout must not survive next to the shards
    legacy = paths.QUESTIONS_INDEX_JSONL
    if legacy.is_file():
        legacy.unlink()
        logger.info(f"index: removed legacy {paths.rel(legacy)} (superseded by state/index/)")

    logger.info(
        f"index: {len(records)} records in {len(manifest_shards)} shard(s) "
        f"across {len(subjects)} subject(s), max shard "
        f"{max((e['bytes'] for e in manifest_shards), default=0) / (1024 * 1024):.1f} MB, "
        f"qid sha256 {manifest['qid_sha256'][:16]}"
    )
    return base / paths.INDEX_MANIFEST_JSON.name


def write_index_gzip(records: Sequence[Dict[str, Any]], target: Path) -> Dict[str, Any]:
    """Optional single-file gzip copy of the whole index."""

    import io

    paths.ensure_dir(target.parent)
    raw = "".join(_shard_line(record) for record in records).encode("utf-8")
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
        handle.write(raw)
    blob = buffer.getvalue()
    target.write_bytes(blob)
    return {
        "path": paths.rel(target),
        "file": target.name,
        "records": len(records),
        "bytes": len(blob),
        "uncompressed_bytes": len(raw),
        "sha256": hashlib.sha256(blob).hexdigest(),
    }


def _gzip_enabled() -> bool:
    return os.environ.get("PYQ_INDEX_GZIP", "").strip().lower() in {"1", "true", "yes", "on"}


def read_index(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Load the index, transparently following the sharded layout.

    ``path`` reads that single JSONL file (legacy/test use).  Otherwise the
    sharded layout is used when ``state/index/manifest.json`` exists, else the
    legacy ``state/questions_index.jsonl``, else the optional gzip copy.
    """

    if path is not None:
        return _read_jsonl_file(Path(path))

    manifest_path = paths.INDEX_MANIFEST_JSON
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        out: List[Dict[str, Any]] = []
        for entry in manifest.get("shards", []):
            target = paths.PROJECT_ROOT / entry["path"]
            out.extend(_read_jsonl_file(target))
        if out:
            return out
    if paths.QUESTIONS_INDEX_JSONL.is_file():
        return _read_jsonl_file(paths.QUESTIONS_INDEX_JSONL)
    if paths.INDEX_GZIP_JSONL.is_file():
        return _read_gzip_file(paths.INDEX_GZIP_JSONL)
    return []


def _read_jsonl_file(target: Path) -> List[Dict[str, Any]]:
    if not target.is_file():
        return []
    out: List[Dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _read_gzip_file(target: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with gzip.open(target, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def index_manifest() -> Optional[Dict[str, Any]]:
    """The shard manifest, or ``None`` when the index is a single file."""

    if not paths.INDEX_MANIFEST_JSON.is_file():
        return None
    return json.loads(paths.INDEX_MANIFEST_JSON.read_text(encoding="utf-8"))


def coverage(records: Sequence[Dict[str, Any]], paper_reports: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts for the coverage line.

    ``placed + skipped_hindi + unclassified + flagged_papers_questions`` must
    equal the number of in-scope questions.  Flagged questions are counted from
    the paper reports rather than the records, because flagged questions are not
    written to the index.
    """

    placed = sum(1 for r in records if r["status"] == STATUS_PLACED)
    unclassified = sum(1 for r in records if r["status"] == STATUS_UNCLASSIFIED)
    skipped_hindi = sum(int(r.get("hindi_count", 0)) for r in paper_reports)
    flagged_questions = sum(int(r.get("flagged_questions", 0)) for r in paper_reports)
    total_papers = len(paper_reports)
    total_questions = sum(int(r.get("question_count", 0)) for r in paper_reports)
    return {
        "papers": total_papers,
        "questions": total_questions,
        "placed": placed,
        "unclassified": unclassified,
        "skipped_hindi": skipped_hindi,
        "flagged_papers_questions": flagged_questions,
        "accounted": placed + unclassified + skipped_hindi + flagged_questions,
        "balanced": placed + unclassified + skipped_hindi + flagged_questions == total_questions,
        "papers_validated": sum(1 for r in paper_reports if r.get("signature", {}).get("ok")),
        "papers_flagged": sum(1 for r in paper_reports if r.get("flagged")),
        "papers_placeable": sum(1 for r in paper_reports if r.get("placeable")),
    }
