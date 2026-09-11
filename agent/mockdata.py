"""Mock pack generation.

Emits a catalogue plus one payload per pack::

    database/mocks/index.json
    database/mocks/packs/<kind>/<id>.json

Each pack is ``{kind, id, subject, count, exam_filter, year_range, questions}``
where ``questions`` is a list of ``qid`` values.  Kinds: ``concept``, ``topic``,
``chapter``, ``subject``, ``exam``, ``year``, ``full``.

Selection is deterministic: questions are grouped by ``(exam, year, shift)`` and
drawn round-robin so a pack spreads across the corpus instead of exhausting one
paper, then capped at ``pack_size``.  Re-running produces identical packs.

Pack keys are **case-insensitive** (``fold_key``): two labels that differ only in
letter case are the same group, and where two genuinely different keys still
slugify to the same id the id gets a stable digest suffix instead of silently
overwriting the first pack's questions on disk.
"""

from __future__ import annotations

import hashlib
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import paths
from .util import Log, fold_key, human_int, slugify, write_json

KINDS = ("concept", "topic", "chapter", "subject", "exam", "year", "full")
DEFAULT_PACK_SIZE = 100
#: every non-empty group gets a pack by default, so the catalogue really does
#: index *every* concept/topic/chapter; raise it (``--min-size``) to prune.
DEFAULT_MIN_SIZE = 1


@dataclass
class Pack:
    kind: str
    id: str
    subject: Optional[str]
    count: int
    exam_filter: Optional[str]
    year_range: Optional[List[int]]
    questions: List[str]
    #: the folded grouping key the pack was built from (used for stable ids)
    source_key: Tuple[str, ...] = ()

    def as_dict(self, *, with_questions: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "kind": self.kind,
            "id": self.id,
            "subject": self.subject,
            "count": self.count,
            "exam_filter": self.exam_filter,
            "year_range": self.year_range,
        }
        if with_questions:
            data["questions"] = self.questions
        return data


def _key_for(kind: str, record: Dict[str, Any]) -> Optional[Tuple[str, ...]]:
    """Grouping key for one record, case/diacritic-insensitively folded.

    Folding keeps ``STATIC GK`` and ``Static GK`` (or ``World Geography`` and
    ``WORLD GEOGRAPHY``) in one group instead of two packs whose ids collide on a
    case-insensitive path.
    """

    def fold(value: Any) -> str:
        return fold_key(value)

    if kind == "concept":
        if record.get("concept") is None:
            return None
        return (
            fold(record.get("subject")),
            fold(record.get("chapter")),
            fold(record.get("topic") or ""),
            fold(record["concept"]),
        )
    if kind == "topic":
        if not record.get("topic"):
            return None
        return (fold(record.get("subject")), fold(record.get("chapter")), fold(record["topic"]))
    if kind == "chapter":
        if not record.get("chapter"):
            return None
        return (fold(record.get("subject")), fold(record["chapter"]))
    if kind == "subject":
        return (fold(record.get("subject")),)
    if kind == "exam":
        return (fold(record.get("exam")),)
    if kind == "year":
        return (fold(record.get("year")),)
    if kind == "full":
        return ("all",)
    return None


def _pack_id(kind: str, key: Tuple[str, ...]) -> str:
    if kind == "full":
        return "all"
    if kind == "year":
        return f"year-{key[0]}"
    return slugify("-".join(part for part in key if part), fallback=f"{kind}-pack")


def _disambiguate_ids(packs: Sequence[Pack]) -> int:
    """Give colliding ``(kind, id)`` pairs a stable digest suffix.

    Two different groups can still slugify to the same id (punctuation-only
    differences).  Silently letting the second pack overwrite the first on disk
    would drop questions from the catalogue, so the later pack gets
    ``<id>--<6 hex of the source key>`` — deterministic and collision-free.
    """

    seen: Dict[Tuple[str, str], int] = {}
    renamed = 0
    for pack in packs:
        slot = (pack.kind, pack.id)
        seen[slot] = seen.get(slot, 0) + 1
        if seen[slot] == 1:
            continue
        digest = hashlib.sha256("|".join(pack.source_key).encode("utf-8")).hexdigest()[:6]
        pack.id = f"{pack.id}--{digest}"
        renamed += 1
        seen[(pack.kind, pack.id)] = 1
    return renamed


def _diverse_order(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Round-robin across (exam, year, shift) buckets for even coverage."""

    buckets: "OrderedDict[Tuple[int, int, int], List[Dict[str, Any]]]" = OrderedDict()
    for record in sorted(
        records,
        key=lambda r: (
            str(r.get("exam")),
            int(r.get("year") or 0),
            int(r.get("shift") or 0),
            str(r.get("paper_path")),
            int(r.get("ordinal") or 0),
        ),
    ):
        key = (str(record.get("exam")), int(record.get("year") or 0), int(record.get("shift") or 0))
        buckets.setdefault(key, []).append(record)

    out: List[Dict[str, Any]] = []
    lists = [list(v) for v in buckets.values()]
    while lists:
        remaining: List[List[Dict[str, Any]]] = []
        for bucket in lists:
            if bucket:
                out.append(bucket.pop(0))
            if bucket:
                remaining.append(bucket)
        lists = remaining
    return out


def build_packs(
    records: Sequence[Dict[str, Any]],
    *,
    kinds: Sequence[str] = KINDS,
    pack_size: int = DEFAULT_PACK_SIZE,
    min_size: int = DEFAULT_MIN_SIZE,
    max_packs: int = 0,
) -> List[Pack]:
    """Build every pack for the given kinds."""

    grouped: Dict[str, Dict[Tuple[str, ...], List[Dict[str, Any]]]] = {
        kind: defaultdict(list) for kind in kinds
    }
    for record in records:
        for kind in kinds:
            key = _key_for(kind, record)
            if key is not None:
                grouped[kind][key].append(record)

    packs: List[Pack] = []
    for kind in kinds:
        rows = grouped[kind]
        for key, items in sorted(rows.items()):
            if len(items) < min_size:
                continue
            ordered = _diverse_order(items)
            selected = ordered[:pack_size] if pack_size > 0 else ordered
            years = sorted({int(r.get("year") or 0) for r in selected if r.get("year")})
            subject = str(items[0].get("subject")) if kind in ("concept", "topic", "chapter", "subject") else None
            packs.append(
                Pack(
                    kind=kind,
                    id=_pack_id(kind, key),
                    subject=subject,
                    count=len(selected),
                    exam_filter=str(items[0].get("exam")) if kind == "exam" else None,
                    year_range=[years[0], years[-1]] if years else None,
                    questions=[str(r.get("qid")) for r in selected],
                    source_key=key,
                )
            )

    packs.sort(key=lambda p: (KINDS.index(p.kind), p.id))
    _disambiguate_ids(packs)
    if max_packs > 0:
        packs = packs[:max_packs]
    return packs


def write_mock_catalogue(
    *,
    records: Optional[Sequence[Dict[str, Any]]] = None,
    kinds: Sequence[str] = KINDS,
    pack_size: int = DEFAULT_PACK_SIZE,
    min_size: int = DEFAULT_MIN_SIZE,
    max_packs: int = 0,
    mocks_dir: Optional[Path] = None,
    log: Optional[Log] = None,
) -> Dict[str, Any]:
    logger = log or Log("mocks")
    if records is None:
        from . import indexer

        records = indexer.read_index()
    if not records:
        logger.warn("no index records – run `python -m agent.cli phase0` first")
        return {"status": "empty", "packs": 0, "questions": 0}

    base = mocks_dir or paths.MOCKS_DIR
    packs = build_packs(
        records, kinds=kinds, pack_size=pack_size, min_size=min_size, max_packs=max_packs
    )

    by_kind: Counter = Counter()
    per_kind_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: {"packs": 0, "questions": 0})
    catalogue: List[Dict[str, Any]] = []
    written: set = set()
    paths_seen: Counter = Counter()

    for pack in packs:
        path = base / "packs" / pack.kind / f"{pack.id}.json"
        paths_seen[path.as_posix()] += 1
        write_json(path, pack.as_dict())
        written.add(path.resolve())
        by_kind[pack.kind] += 1
        per_kind_counts[pack.kind]["packs"] += 1
        per_kind_counts[pack.kind]["questions"] += pack.count
        catalogue.append(
            {
                **pack.as_dict(with_questions=False),
                "path": paths.rel(path),
            }
        )

    collisions = {p: n for p, n in paths_seen.items() if n > 1}
    if collisions:
        # unreachable while _disambiguate_ids does its job; a hard signal that a
        # pack would have been silently overwritten is better than a lost pack
        logger.warn(f"mocks: {len(collisions)} pack path collision(s): {sorted(collisions)[:3]}")

    # Drop payloads of a previous revision: a rebuilt catalogue must not leave
    # packs behind for concepts that no longer exist (the same rule the question
    # tree follows, see agent.build_db.prune_subject_tree).
    stale = 0
    packs_root = base / "packs"
    if packs_root.is_dir():
        for path in sorted(packs_root.rglob("*.json")):
            if path.resolve() in written:
                continue
            path.unlink()
            stale += 1
        for directory in sorted(
            (p for p in packs_root.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)
        ):
            if not any(directory.iterdir()):
                directory.rmdir()
    if stale:
        logger.info(f"mocks: pruned {stale} stale pack(s) from the previous catalogue")

    index = {
        "version": 1,
        "generated_by": "agent.mockdata",
        "pack_size": pack_size,
        "min_size": min_size,
        "kinds": list(kinds),
        "totals": {
            "packs": len(packs),
            "questions_in_packs": sum(p.count for p in packs),
        },
        "by_kind": {kind: dict(per_kind_counts[kind]) for kind in kinds if per_kind_counts[kind]},
        "collisions": len(collisions),
        "packs": catalogue,
    }
    write_json(base / "index.json", index)

    summary = {
        "status": "ok",
        "packs": len(packs),
        "questions_in_packs": index["totals"]["questions_in_packs"],
        "collisions": len(collisions),
        "by_kind": index["by_kind"],
        "index": paths.rel(base / "index.json"),
    }
    logger.info(f"mocks: {len(packs)} packs, {index['totals']['questions_in_packs']} question slots")
    return summary


def render_mocks_md(index: Dict[str, Any]) -> str:
    lines: List[str] = []
    add = lines.append
    add("# Mock packs")
    add("")
    add(f"- Total packs: **{human_int(index['totals']['packs'])}**")
    add(f"- Question slots: **{human_int(index['totals']['questions_in_packs'])}**")
    add("")
    add("| Kind | Packs | Questions |")
    add("|---|---:|---:|")
    for kind, data in index["by_kind"].items():
        add(f"| `{kind}` | {human_int(data['packs'])} | {human_int(data['questions'])} |")
    add("")
    add("Consume a pack with:")
    add("")
    add("```bash")
    add("python tools/mocks.py list --kind concept --subject MATH --limit 20")
    add("python tools/mocks.py build --kind concept --id profit-and-loss > mock.json")
    add("```")
    add("")
    return "\n".join(lines)
