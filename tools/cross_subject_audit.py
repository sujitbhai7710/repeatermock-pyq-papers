"""Cross-subject placement audit — chapter/topic *ownership*.

The first version of this tool compared the raw ``concept`` string against the
taxonomy and reported every label that also exists in another subject.  That
produced 4,150 "hits" of which nearly all were correct: SSC reuses names across
subjects, so a *Reasoning* question filed under the reasonology chapter
``MATHEMATICAL OPERATIONS`` with the concept ``Ratio & Proportion`` is filed
correctly even though *Ratio & Proportion* is also maths vocabulary.

The check is therefore about **who owns the chapter**, not about who owns the
label:

``definitely wrong``
    the record's chapter is **not declared** by the subject it is filed under,
    while another subject's taxonomy *does* declare that chapter — or the record
    has no chapter at all and its concept is the name of another subject's
    chapter (the leaf is then literally named after foreign vocabulary, e.g.
    ``gk/_unclassified/verbal-ability``).

``ambiguous``
    the chapter is correct for the filing subject, but the concept/raw label is
    also known in another subject's vocabulary.  These are the *shared names*
    (``Ratio & Proportion``, ``Analogy``, ``Computer Fundamentals``) and they are
    **not** wrong: the leaf hangs off a chapter its own subject declares.

``ok``
    neither of the above.

Nothing is moved by default.  ``--fix`` performs the one deterministic repair:
for a *chapterless* record whose concept is another subject's chapter name, the
bogus concept is dropped (it stays in ``concept_raw``), so the record moves from
``<subject>/_unclassified/<foreign-chapter>`` to ``<subject>/_unclassified``
while keeping the subject the paper's layout gave it.  ``--fix`` never changes a
question's subject — the raw text of those questions shows the label, not the
subject, is what is wrong.

Usage::

    python tools/cross_subject_audit.py                 # report
    python tools/cross_subject_audit.py --verbose       # per-group samples
    python tools/cross_subject_audit.py --json
    python tools/cross_subject_audit.py --fix           # apply the leaf hygiene
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent import build_db, paths  # noqa: E402
from agent.util import Log, norm_key  # noqa: E402

#: the taxonomy subject labels, as they appear in the index records
SUBJECTS = ("MATH", "REAS", "GK", "ENG", "COMPUTER")

DIR_TO_SUBJECT = {value: key for key, value in build_db.SUBJECT_DIRS.items()}

#: how many records a group lists in ``--verbose``
SAMPLES = 5
#: groups printed in the report
TOP_GROUPS = 20

VERDICT_WRONG = "definitely wrong"
VERDICT_AMBIGUOUS = "ambiguous"
VERDICT_OK = "ok"


# ---------------------------------------------------------------------------
# taxonomy ownership
# ---------------------------------------------------------------------------


def _chapter_names(chapter: Dict[str, Any]) -> Iterable[str]:
    """Every name a chapter declares (its own, its topics', its concepts')."""

    if chapter.get("name"):
        yield chapter["name"]
    for topic in chapter.get("topics") or []:
        if topic.get("name"):
            yield topic["name"]
        for sub in topic.get("subtopics") or []:
            if sub.get("name"):
                yield sub["name"]
        for concept in topic.get("concepts") or []:
            name = concept if isinstance(concept, str) else (concept or {}).get("name")
            if name:
                yield name
    for concept in chapter.get("concepts") or []:
        name = concept if isinstance(concept, str) else (concept or {}).get("name")
        if name:
            yield name


class Ownership:
    """Who declares which chapter, topic and concept name.

    The *name* map (:meth:`name_owners`) is built from
    :func:`agent.classify._subject_names` — the classifier's own definition of
    "the vocabulary a subject owns" — so this audit and
    :meth:`agent.classify.Classifier.foreign_vocabulary` can never drift apart:
    whatever the classifier refuses to write as a leaf name is exactly what the
    audit reports as ``definitely wrong``.
    """

    def __init__(self, taxonomy: Dict[str, Any]) -> None:
        from agent.classify import _subject_names

        self.declared_chapters: Dict[str, Set[str]] = {}
        self.chapter_owner: Dict[str, Set[str]] = defaultdict(set)
        self.topic_owner: Dict[str, Set[str]] = defaultdict(set)
        self.name_owner: Dict[str, Set[str]] = defaultdict(set)
        #: the *old* tool's map: it also counted topic-level concepts as owned
        #: names, so it selected slightly more records than :attr:`name_owner`.
        #: Kept so ``legacy_hits`` reproduces its 4,150 hits exactly.
        self.legacy_owner: Dict[str, Set[str]] = defaultdict(set)
        for subject, body in (taxonomy.get("subjects") or {}).items():
            chapters = body.get("chapters") or []
            self.declared_chapters[subject] = {
                norm_key(chapter.get("name")) for chapter in chapters if chapter.get("name")
            }
            for chapter in chapters:
                chapter_key = norm_key(chapter.get("name"))
                if chapter_key:
                    self.chapter_owner[chapter_key].add(subject)
                for topic in chapter.get("topics") or []:
                    topic_key = norm_key(topic.get("name"))
                    if topic_key:
                        self.topic_owner[topic_key].add(subject)
                for name in _chapter_names(chapter):
                    key = norm_key(name)
                    if key:
                        self.legacy_owner[key].add(subject)
            for name in _subject_names(body):
                key = norm_key(name)
                if key:
                    self.name_owner[key].add(subject)

    def chapter_owners(self, name: Any) -> Set[str]:
        return self.chapter_owner.get(norm_key(name), set())

    def topic_owners(self, name: Any) -> Set[str]:
        return self.topic_owner.get(norm_key(name), set())

    def name_owners(self, name: Any) -> Set[str]:
        return self.name_owner.get(norm_key(name), set())

    def declares_chapter(self, subject: str, name: Any) -> bool:
        return norm_key(name) in self.declared_chapters.get(subject, set())

    def owns_name(self, subject: str, name: Any) -> bool:
        """Whether *subject* itself declares *name* at any level."""

        return subject in self.name_owner.get(norm_key(name), set())

    def foreign_name(self, subject: str, name: Any) -> Set[str]:
        """The other subjects that declare *name* (empty when it is unknown)."""

        return self.name_owners(name) - {subject}


# ---------------------------------------------------------------------------
# the verdicts
# ---------------------------------------------------------------------------


def _foreign_label(
    record: Dict[str, Any], subject: str, ownership: Ownership
) -> Optional[Tuple[str, str, Set[str]]]:
    """First label of the record that another subject's taxonomy also knows.

    Returns ``(field, label, foreign owners)``.  ``concept``/``concept_raw`` come
    first: they are the labels the classifier used, so a foreign match there is
    the meaningful signal.  The source ``tags`` are checked last — they are noisy
    (a reasoning paper tags a word-pair question ``General Knowledge``), so they
    never turn a record into "wrong", only into "ambiguous".
    """

    for field in ("concept", "concept_raw"):
        value = record.get(field)
        owners = ownership.name_owners(value) if value else set()
        foreign = owners - {subject}
        if foreign:
            return field, str(value), foreign
    for tag in record.get("tags") or []:
        if not isinstance(tag, str):
            continue
        owners = ownership.name_owners(tag)
        foreign = owners - {subject}
        if foreign:
            return "tags", tag, foreign
    return None


def verdict_for(
    record: Dict[str, Any], subject: str, ownership: Ownership
) -> Tuple[str, str]:
    """``(verdict, why)`` for one index record.

    The chapter decides: a leaf is **wrong** only when its chapter is not
    declared by the filing subject (while another subject declares it) or, for a
    chapterless record, when its concept is another subject's chapter name (the
    leaf is then literally named after foreign vocabulary).  A record whose
    chapter *is* the subject's own is at most **ambiguous** — its concept label
    happens to be shared with another subject, which is normal in this corpus.
    """

    chapter = record.get("chapter")
    chapter_key = norm_key(chapter)
    if chapter_key:
        foreign_chapter = ownership.chapter_owners(chapter) - {subject}
        if foreign_chapter and not ownership.declares_chapter(subject, chapter):
            return VERDICT_WRONG, (
                f"chapter {chapter!r} is not declared by {subject} "
                f"(declared by {', '.join(sorted(foreign_chapter))})"
            )
        match = _foreign_label(record, subject, ownership)
        if match is not None:
            field, label, owners = match
            return VERDICT_AMBIGUOUS, (
                f"chapter {chapter!r} is {subject}'s own; {field} {label!r} is also known "
                f"in {', '.join(sorted(owners))} — a shared name, not a misplacement"
            )
        return VERDICT_OK, ""

    # no chapter: the concept is what names the leaf, so it must be vocabulary
    # this subject owns — otherwise the leaf is a foreign slug (the exact rule
    # agent.classify.Classifier._leaf_hygiene enforces)
    concept = record.get("concept")
    if not concept:
        return VERDICT_OK, ""
    foreign = ownership.foreign_name(subject, concept)
    if foreign and not ownership.owns_name(subject, concept):
        return VERDICT_WRONG, (
            f"no chapter; the concept {concept!r} is {', '.join(sorted(foreign))}'s "
            f"{foreign_level(concept, subject, ownership)} vocabulary, not {subject}'s — "
            f"the leaf is named after another subject's vocabulary"
        )
    match = _foreign_label(record, subject, ownership)
    if match is not None:
        field, label, owners = match
        return VERDICT_AMBIGUOUS, (
            f"no chapter; {field} {label!r} is also known in {', '.join(sorted(owners))}"
        )
    return VERDICT_OK, ""


def foreign_level(name: Any, subject: str, ownership: Ownership) -> str:
    """At which detail level the other subject declares *name*."""

    if ownership.chapter_owners(name) - {subject}:
        return "chapter"
    if ownership.topic_owners(name) - {subject}:
        return "topic"
    return "concept"


def is_legacy_hit(record: Dict[str, Any], subject: str, ownership: Ownership) -> bool:
    """The old tool's test: a label owned by exactly one *other* subject.

    Kept so the report can show how the hits it produced (4,150 on this corpus)
    are classified by the chapter-ownership rule — they are almost all
    ``ambiguous``: the chapter is the filing subject's own.
    """

    def unique_foreign(value: Any) -> bool:
        owners = ownership.legacy_owner.get(norm_key(value), set()) if value else set()
        return len(owners) == 1 and subject not in owners

    for field in ("concept", "concept_raw"):
        if unique_foreign(record.get(field)):
            return True
    return any(unique_foreign(tag) for tag in (record.get("tags") or []) if isinstance(tag, str))


def _leaf_label(record: Dict[str, Any]) -> str:
    """The label the record's leaf is named after."""

    return str(record.get("chapter") or record.get("concept") or "")


def iter_index_records() -> Iterable[Tuple[str, Dict[str, Any]]]:
    """``(filing subject, record)`` for every shard of the index."""

    for path in sorted(glob.glob(os.path.join(ROOT, "state", "index", "*.jsonl"))):
        shard = os.path.basename(path).split(".")[0].upper()
        subject = shard if shard in SUBJECTS else shard
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                yield subject, json.loads(line)


def scan() -> Dict[str, Any]:
    """Classify every index record; returns the report payload."""

    with open(os.path.join(ROOT, "state", "taxonomy.json"), encoding="utf-8") as handle:
        taxonomy = json.load(handle)
    ownership = Ownership(taxonomy)

    counts: Counter = Counter()
    legacy: Counter = Counter()
    levels: Counter = Counter()
    groups: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    subject_totals: Counter = Counter()
    total = 0
    for subject, record in iter_index_records():
        total += 1
        subject_totals[subject] += 1
        label = _leaf_label(record)
        verdict, why = verdict_for(record, subject, ownership)
        counts[verdict] += 1
        if verdict == VERDICT_WRONG:
            levels[
                "chapter" if record.get("chapter")
                else foreign_level(record.get("concept"), subject, ownership)
            ] += 1
        if is_legacy_hit(record, subject, ownership):
            legacy[verdict] += 1
        if verdict == VERDICT_OK:
            continue
        owners = ownership.chapter_owners(label) or ownership.name_owners(label)
        key = (verdict, subject, label)
        bucket = groups.get(key)
        if bucket is None:
            bucket = {
                "verdict": verdict,
                "subject": subject,
                "label": label,
                "owner": ", ".join(sorted(owners)),
                "count": 0,
                "why": why,
                "samples": [],
            }
            groups[key] = bucket
        bucket["count"] += 1
        if len(bucket["samples"]) < SAMPLES:
            bucket["samples"].append(
                {
                    "qid": record.get("qid"),
                    "exam": record.get("exam"),
                    "year": record.get("year"),
                    "ordinal": record.get("ordinal"),
                    "chapter": record.get("chapter"),
                    "concept": record.get("concept"),
                    "concept_raw": record.get("concept_raw"),
                    "paper_path": record.get("paper_path"),
                }
            )

    shared = {
        key: sorted(owners)
        for key, owners in ownership.chapter_owner.items()
        if len(owners) > 1
    }
    ordered = sorted(groups.values(), key=lambda item: (-item["count"], item["label"]))
    return {
        "scanned": total,
        "by_subject": dict(subject_totals.most_common()),
        "counts": {
            VERDICT_WRONG: counts[VERDICT_WRONG],
            VERDICT_AMBIGUOUS: counts[VERDICT_AMBIGUOUS],
            VERDICT_OK: counts[VERDICT_OK],
        },
        #: the previous version of this tool reported every label that another
        #: subject owns (the "hits"): show how the new rule classifies them
        "legacy_hits": {
            "total": sum(legacy.values()),
            VERDICT_WRONG: legacy[VERDICT_WRONG],
            VERDICT_AMBIGUOUS: legacy[VERDICT_AMBIGUOUS],
            VERDICT_OK: legacy[VERDICT_OK],
        },
        #: at which taxonomy level the foreign name sits (chapter / topic / concept)
        "wrong_by_level": dict(levels.most_common()),
        "declared_chapters": {
            subject: len(names) for subject, names in sorted(ownership.declared_chapters.items())
        },
        "chapters_shared_between_subjects": shared,
        "groups": ordered,
    }


def format_report(payload: Dict[str, Any], *, top: int = TOP_GROUPS, verbose: bool = False) -> str:
    lines: List[str] = []
    add = lines.append
    counts = payload["counts"]
    add("=" * 78)
    add("CROSS-SUBJECT PLACEMENT AUDIT")
    add("=" * 78)
    add(f"scanned            : {payload['scanned']} index records")
    add(
        "declared chapters  : "
        + ", ".join(f"{subject} {count}" for subject, count in payload["declared_chapters"].items())
    )
    shared = payload["chapters_shared_between_subjects"]
    add(
        "shared chapters    : "
        + (", ".join(sorted(shared)) if shared else "none (chapter ownership is unambiguous)")
    )
    add("")
    add(f"definitely wrong   : {counts[VERDICT_WRONG]}")
    add(f"ambiguous          : {counts[VERDICT_AMBIGUOUS]}")
    add(f"ok                 : {counts[VERDICT_OK]}")
    by_level = payload.get("wrong_by_level") or {}
    if by_level:
        add(
            "  wrong by level   : "
            + ", ".join(f"{level} {count}" for level, count in by_level.items())
            + "  (the level of the other subject's taxonomy the label comes from)"
        )
    add("")
    legacy = payload.get("legacy_hits") or {}
    if legacy:
        total_legacy = legacy.get("total", 0) or 1
        wrong_legacy = legacy.get(VERDICT_WRONG, 0)
        add(
            "the old label-ownership rule — every label owned by exactly one other subject, "
            f"re-implemented here — selects {legacy.get('total', 0)} records."
        )
        add(
            f"  only {wrong_legacy} of them ({100.0 * wrong_legacy / total_legacy:.1f}%) are definitely "
            "wrong here: the rest hang off a chapter their own subject declares"
        )
        add("")
    for verdict, title in (
        (
            VERDICT_WRONG,
            "definitely wrong — the leaf sits under / is named after another subject's vocabulary",
        ),
        (
            VERDICT_AMBIGUOUS,
            "ambiguous — chapter is correct for the filing subject, the label is shared",
        ),
    ):
        rows = [group for group in payload["groups"] if group["verdict"] == verdict]
        add("-" * 78)
        add(f"{title}: {counts[verdict]}")
        add("-" * 78)
        if not rows:
            add("  (none)")
            add("")
            continue
        for group in rows[:top]:
            add(
                f"  {group['count']:6d}  {group['subject']:9s} -> "
                f"{group['owner'] or '?':9s}  {group['label']!r}"
            )
            if verbose:
                add(f"          {group['why']}")
                for sample in group["samples"]:
                    add(
                        f"          {sample['qid']}  {sample['exam']} {sample['year']} "
                        f"#{sample['ordinal']}  concept={sample['concept']!r} "
                        f"raw={sample['concept_raw']!r}"
                    )
        if len(rows) > top:
            add(f"  ... {len(rows) - top} more group(s)")
        add("")
    add("=" * 78)
    if counts[VERDICT_WRONG]:
        add("RESULT: FAIL — see the 'definitely wrong' groups above")
    else:
        add("RESULT: PASS — every leaf hangs off a chapter its own subject declares")
    add("=" * 78)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# --fix: drop the foreign concept label of a chapterless record
# ---------------------------------------------------------------------------


#: the phase that writes each subject's tree (used to refresh ``analysis.json``)
SUBJECT_PHASE = {"ENG": "phase1", "GK": "phase2", "MATH": "phase3", "REAS": "phase4", "COMPUTER": "phase5"}


def plan_fix(ownership: Ownership) -> Dict[str, Any]:
    """The deterministic repair plan for the *chapterless* wrong records."""

    repairs: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    for subject, record in iter_index_records():
        label = _leaf_label(record)
        verdict, why = verdict_for(record, subject, ownership)
        if verdict != VERDICT_WRONG:
            continue
        entry = {
            "subject": subject,
            "qid": record.get("qid"),
            "label": label,
            "concept": record.get("concept"),
            "why": why,
            "leaf": record.get("_leaf"),
        }
        if record.get("chapter"):
            # a real chapter that another subject owns: not a label problem, so
            # it needs a human/AI decision instead of a mechanical cleanup
            blocked.append(entry)
        else:
            repairs.append(entry)
    return {"repairs": repairs, "blocked": blocked}


def apply_fix(*, log: Log, dry_run: bool = False) -> Dict[str, Any]:
    """Clear the foreign concept of the chapterless wrong records and rebuild.

    Returns a payload with the moved records (``from``/``to`` leaf) so the caller
    can print exactly what changed.  Nothing happens when there is nothing to fix
    (idempotent).
    """

    from agent import indexer
    from agent.phases import subject_phase

    ownership = Ownership(json.load(open(os.path.join(ROOT, "state", "taxonomy.json"), encoding="utf-8")))
    plan = plan_fix(ownership)
    repairs = plan["repairs"]
    result: Dict[str, Any] = {"moved": [], "blocked": plan["blocked"], "subjects": [], "dry_run": dry_run}
    if not repairs:
        return result

    records = indexer.read_index()
    targets = {str(entry["qid"]): entry for entry in repairs}
    by_subject: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        entry = targets.get(str(record.get("qid")))
        if entry is None:
            continue
        if str(record.get("subject") or "") != entry["subject"]:
            continue
        source = "<subject>/_unclassified/{slug}".format(
            slug=build_db.slugify(record.get("concept"), fallback="unclassified")
        )
        if record.get("chapter"):
            continue
        record["concept"] = None
        if ownership.chapter_owners(record.get("topic")) - {entry["subject"]}:
            record["topic"] = None
        by_subject[entry["subject"]].append(record)
        result["moved"].append(
            {
                "qid": record.get("qid"),
                "subject": entry["subject"],
                "from": source.replace("<subject>", build_db.subject_dir(entry["subject"])),
                "to": f"{build_db.subject_dir(entry['subject'])}/_unclassified",
                "label": entry["label"],
                "concept_raw": record.get("concept_raw"),
                "exam": record.get("exam"),
                "year": record.get("year"),
                "ordinal": record.get("ordinal"),
            }
        )

    result["subjects"] = sorted(by_subject)
    if dry_run:
        return result

    if "ENG" in by_subject:
        raise SystemExit(
            "refusing to rebuild the English tree from here: its grammar rule leaves are "
            "written by the grammar pass, so re-run `python -m agent.cli grammar` instead"
        )

    indexer.write_index(records, log=log)
    log.info(f"index rewritten ({len(result['moved'])} record(s) re-filed)")

    subsets: Dict[str, List[Dict[str, Any]]] = {
        subject: [r for r in records if str(r.get("subject") or "") == subject]
        for subject in by_subject
    }
    for subject, subset in sorted(subsets.items()):
        build_db.write_question_tree(subset, log=log)
        subject_phase._write_subject_analysis(
            SUBJECT_PHASE.get(subject, "phase"), subject, subset, log
        )
        log.info(f"{subject}: tree + analysis.json rebuilt from the corrected index")
    return result


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verbose", action="store_true", help="per-group samples")
    ap.add_argument("--top", type=int, default=TOP_GROUPS, help="groups to print")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    ap.add_argument(
        "--fix",
        action="store_true",
        help="apply the leaf hygiene for the chapterless wrong records (never changes a subject)",
    )
    ap.add_argument("--dry-run", action="store_true", help="show what --fix would move")
    args = ap.parse_args(argv)

    log = Log("cross_subject")
    payload = scan()
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_report(payload, top=max(1, args.top), verbose=args.verbose))

    if args.fix or args.dry_run:
        fixed = apply_fix(log=log, dry_run=args.dry_run)
        print()
        print("-" * 78)
        print("LEAF HYGIENE" + (" (dry run)" if args.dry_run else ""))
        print("-" * 78)
        if not fixed["moved"]:
            print("  nothing to move — every leaf is already named after its own subject")
        else:
            by_pair: Counter = Counter()
            for move in fixed["moved"]:
                by_pair[(move["from"], move["to"])] += 1
            for (source, target), count in sorted(by_pair.items(), key=lambda item: -item[1]):
                print(f"  {count:6d}  {source}  ->  {target}")
            print(f"  {len(fixed['moved']):6d} record(s) in {', '.join(fixed['subjects'])}")
            if args.dry_run:
                print("  (dry run: nothing written)")
        if fixed["blocked"]:
            print("")
            print(f"  {len(fixed['blocked'])} record(s) need a decision (chapter owned by another subject):")
            for entry in fixed["blocked"][:10]:
                print(f"    {entry['subject']:9s} {entry['qid']}  {entry['why']}")
            return 1
    if not args.json and payload["counts"][VERDICT_WRONG]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
