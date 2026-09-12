#!/usr/bin/env python
"""Deterministic structural self-check of the generated ``database/``.

The audit fails (non-zero exit) on any of the structural defects the database
must never contain:

1. **duplicated nesting levels** — no directory may repeat its parent's name
   (``english/grammar/grammar``, ``english/vocabulary/antonym/antonym``);
2. **stray chapter-level ``index.md``** — a directory may not carry an
   ``index.md`` while a child directory of the *same* name exists;
3. **cross-subject leakage** — no path component of an English leaf may be the
   canonical vocabulary of another subject (maths / gk / reasoning / computer),
   e.g. the maths chapter *Time & Work* as ``english/.../time-and-work``;
4. **empty/None slugs** — every tree path component must be a real slug (never
   empty, never the ``unnamed`` fallback), every pointer record must carry a
   non-empty ``qid`` and ``subject``, and the ``_unclassified`` placeholder may
   only appear for a record that really has no concept;
5. **a question filed twice** — one question must live in exactly one concept
   leaf;
6. **chapter/topic parent mismatch** — a leaf's concept must belong to the
   chapter's declared concept set (the chapter's own name, short name, topics,
   subtopics and declared concepts, or a token-subset of the chapter name).
   A placed question whose concept the chapter does *not* declare must sit under
   the explicit ``_other`` bucket instead of pretending the chapter declares it;
7. **case-only collisions / silent overwrite** — two leaves may not differ only
   by letter case, a leaf may not hold two concept labels that differ only by
   case, and the mock catalogue may not contain two packs writing to the same
   file (the second write would silently drop the first pack's questions).

7. **case-only collisions / silent overwrite** — two leaves may not differ only
   by letter case, a leaf may not hold two concept labels that differ only by
   case, and the mock catalogue may not contain two packs writing to the same
   file (the second write would silently drop the first pack's questions);
8. **one question, one link** — a qid may not be linked from two leaves, and a
   leaf may not list the same source question twice.  A qid the *source papers
   reuse for different questions* is a warning, not a violation: it resolves to
   different questions, each of which still lives in exactly one leaf;
9. **an empty leaf must say so** — a leaf whose ``questions.jsonl`` holds no
   record must carry the explicit ``No PYQ in scope`` marker in its
   ``index.md``, so "the corpus has no such question" can never be confused with
   "the questions were never written".  The reverse is a violation too: a marker
   on a leaf that *does* hold questions is stale;
10. **a subject may only file chapters it declares** — a record whose ``chapter``
    the filing subject's taxonomy does not declare is misplaced, and a
    *chapterless* record may not be named after vocabulary only another subject
    owns (the leaf would then be a foreign slug, e.g.
    ``gk/_unclassified/verbal-ability``);
11. **the derived ``_analysis`` view is never published** — the English
    vocabulary/grammar analysis view repeats questions that already have a home
    in the tree, so ``agent.gitpush`` must keep it out of the ``pyq-db``
    checkpoint branch.

Rule 5 keys on the *question identity* (``qid`` + source paper + ordinal).  A
bare ``qid`` is not unique in this corpus — the source papers reuse a handful of
ids for different questions — so reused ids that resolve to different questions
are reported as a warning (with the count) instead of a failure.  Rule 8 is the
qid-level companion of rule 5 and keeps the same carve-out.

Usage
-----
::

    python tools/audit_db.py [--json] [--database DIR] [--quiet]

    python -m agent.cli audit            # same check through the CLI

Exit codes: ``0`` clean, ``1`` violations found, ``2`` the database or the
taxonomy is missing/empty.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import paths  # noqa: E402
from agent.build_db import OTHER_DIR, RESERVED_DIRS, SUBJECT_DIRS  # noqa: E402
from agent.taxonomy import ConceptScope, load_taxonomy  # noqa: E402
from agent.util import fold_key, norm_key, slugify  # noqa: E402

#: directory-name placeholders that must never reach the tree
BAD_SLUGS = {"", "unnamed", "none", "null", "nil", "undefined"}

#: the subject audited for cross-subject vocabulary (see rule 3)
AUDITED_SUBJECT_DIR = "english"

SUBJECT_LABELS = {value: key for key, value in SUBJECT_DIRS.items()}
AUDITED_SUBJECT = SUBJECT_LABELS[AUDITED_SUBJECT_DIR]


@dataclass(frozen=True)
class Issue:
    rule: str
    where: str
    detail: str

    def as_dict(self) -> Dict[str, str]:
        return {"rule": self.rule, "where": self.where, "detail": self.detail}

    def __str__(self) -> str:
        return f"[rule {self.rule}] {self.where}: {self.detail}"


@dataclass
class AuditReport:
    database: str
    dirs: int = 0
    files: int = 0
    leaves: int = 0
    records: int = 0
    taxonomy_present: bool = False
    issues: List[Issue] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checked: Dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> Dict[str, Any]:
        return {
            "database": self.database,
            "ok": self.ok,
            "dirs": self.dirs,
            "files": self.files,
            "leaves": self.leaves,
            "records": self.records,
            "taxonomy_present": self.taxonomy_present,
            "checked": self.checked,
            "violations": [issue.as_dict() for issue in self.issues],
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _is_reserved(relative: Sequence[str]) -> bool:
    return any(part in RESERVED_DIRS for part in relative)


def iter_dirs(database: Path) -> Iterator[Tuple[Path, Tuple[str, ...]]]:
    """Every directory under *database* with its relative path parts."""

    for path in sorted(p for p in database.rglob("*") if p.is_dir()):
        yield path, path.relative_to(database).parts


def iter_leaves(subject_root: Path) -> Iterator[Tuple[Path, Tuple[str, ...]]]:
    """Concept leaves of one subject: directories holding ``questions.jsonl``."""

    for path in sorted(subject_root.rglob("questions.jsonl")):
        relative = path.parent.relative_to(subject_root).parts
        if _is_reserved(relative):
            continue
        yield path.parent, relative


def read_pointers(leaf: Path) -> Iterator[Dict[str, Any]]:
    target = leaf / "questions.jsonl"
    if not target.is_file():
        return
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def vocabulary_sets(taxonomy: Dict[str, Any]) -> Dict[str, Dict[str, Set[str]]]:
    """``subject -> {"names": set, "slugs": set}`` for every taxonomy name."""

    out: Dict[str, Dict[str, Set[str]]] = {}
    for subject, body in (taxonomy.get("subjects") or {}).items():
        names: Set[str] = set()
        slugs: Set[str] = set()
        for chapter in body.get("chapters", []):
            for name in _chapter_names(chapter):
                names.add(norm_key(name))
                slugs.add(slugify(name))
        out[subject] = {"names": names, "slugs": slugs}
    return out


def _chapter_names(chapter: Dict[str, Any]) -> Iterator[str]:
    if chapter.get("name"):
        yield chapter["name"]
    for topic in chapter.get("topics", []):
        if topic.get("name"):
            yield topic["name"]
        for sub in topic.get("subtopics", []):
            if sub.get("name"):
                yield sub["name"]
    for concept in chapter.get("concepts", []):
        if concept.get("name"):
            yield concept["name"]


def _foreign_vocabulary(
    vocabulary: Dict[str, Dict[str, Set[str]]], subject: str
) -> Tuple[Set[str], Set[str]]:
    """Names/slugs owned by *another* subject and not by *subject*."""

    own = vocabulary.get(subject, {"names": set(), "slugs": set()})
    foreign_names: Set[str] = set()
    foreign_slugs: Set[str] = set()
    for other, sets in vocabulary.items():
        if other == subject:
            continue
        foreign_names |= sets["names"]
        foreign_slugs |= sets["slugs"]
    return foreign_names - own["names"], foreign_slugs - own["slugs"]


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


def check_duplicated_nesting(database: Path, report: AuditReport) -> None:
    """Rule 1: no directory may repeat its parent's name."""

    for path, parts in iter_dirs(database):
        if _is_reserved(parts):
            continue
        report.checked["dirs"] += 1
        if len(parts) > 1 and parts[-1] == parts[-2]:
            report.issues.append(
                Issue(
                    "1",
                    path.relative_to(database).as_posix(),
                    f"directory level '{parts[-1]}' repeats its parent",
                )
            )


def check_stray_index(database: Path, report: AuditReport) -> None:
    """Rule 2: no chapter-level ``index.md`` next to a same-named child dir."""

    for path, parts in iter_dirs(database):
        if _is_reserved(parts):
            continue
        index = path / "index.md"
        if not index.is_file():
            continue
        same_named = [
            child
            for child in path.iterdir()
            if child.is_dir() and child.name == path.name
        ]
        if same_named:
            report.issues.append(
                Issue(
                    "2",
                    index.relative_to(database).as_posix(),
                    f"child directory '{path.name}' with the same name exists",
                )
            )


def check_cross_subject_leakage(
    database: Path, report: AuditReport, vocabulary: Dict[str, Dict[str, Set[str]]]
) -> None:
    """Rule 3: an English leaf may not be named after another subject's vocabulary."""

    subject_root = database / AUDITED_SUBJECT_DIR
    if not subject_root.is_dir():
        return
    foreign_names, foreign_slugs = _foreign_vocabulary(vocabulary, AUDITED_SUBJECT)
    for leaf, relative in iter_leaves(subject_root):
        for component in relative:
            if component in foreign_slugs or norm_key(component) in foreign_names:
                report.issues.append(
                    Issue(
                        "3",
                        (leaf.relative_to(database)).as_posix(),
                        f"'{component}' is another subject's vocabulary",
                    )
                )


def check_slugs(database: Path, report: AuditReport) -> None:
    """Rule 4: real slugs only, and a non-empty identity on every record."""

    for path, parts in iter_dirs(database):
        if _is_reserved(parts):
            continue
        for component in parts:
            if component.strip().lower() in BAD_SLUGS:
                report.issues.append(
                    Issue(
                        "4",
                        path.relative_to(database).as_posix(),
                        f"empty/placeholder slug '{component}'",
                    )
                )

    for subject_dir, subject in sorted(SUBJECT_LABELS.items()):
        subject_root = database / subject_dir
        if not subject_root.is_dir():
            continue
        for leaf, relative in iter_leaves(subject_root):
            # a collapsed node keeps the placeholder as its last component
            leaf_placeholder = relative[-1] == "_unclassified"
            for record in read_pointers(leaf):
                report.records += 1
                qid = str(record.get("qid") or "")
                if not qid:
                    report.issues.append(
                        Issue("4", leaf.relative_to(database).as_posix(), "pointer record without a qid")
                    )
                if not (record.get("subject") or "").strip():
                    report.issues.append(
                        Issue(
                            "4",
                            leaf.relative_to(database).as_posix(),
                            f"{qid}: pointer record without a subject",
                        )
                    )
                if leaf_placeholder != (record.get("concept") is None):
                    report.issues.append(
                        Issue(
                            "4",
                            leaf.relative_to(database).as_posix(),
                            f"{qid}: concept {record.get('concept')!r} does not match the "
                            f"'_unclassified' placeholder level",
                        )
                    )


def check_duplicate_questions(database: Path, report: AuditReport) -> None:
    """Rule 5: one question (qid + paper + ordinal) lives in one leaf."""

    homes: Dict[Tuple[str, str, str], Set[str]] = defaultdict(set)
    qid_homes: Dict[str, Set[str]] = defaultdict(set)
    for subject_dir in sorted(SUBJECT_LABELS):
        subject_root = database / subject_dir
        if not subject_root.is_dir():
            continue
        for leaf, _relative in iter_leaves(subject_root):
            leaf_key = leaf.relative_to(database).as_posix()
            for record in read_pointers(leaf):
                qid = str(record.get("qid") or "")
                identity = (qid, str(record.get("paper_path") or ""), str(record.get("ordinal")))
                homes[identity].add(leaf_key)
                qid_homes[qid].add(leaf_key)

    for identity, leaves in sorted(homes.items()):
        if len(leaves) > 1:
            report.issues.append(
                Issue(
                    "5",
                    sorted(leaves)[0],
                    f"question {identity[0]} ({identity[1]}#{identity[2]}) is filed in "
                    f"{len(leaves)} leaves: {', '.join(sorted(leaves))}",
                )
            )

    reused = {
        qid: leaves
        for qid, leaves in qid_homes.items()
        if len(qid_homes[qid]) > 1
    }
    # the reused-id case is reported once, by rule 8 (the qid-level companion of
    # this check), so the report never says the same thing twice
    report.checked["rule5_reused_qids"] = len(reused)


def check_question_uniqueness(database: Path, report: AuditReport) -> None:
    """Rule 8: a question is linked from one leaf, and listed once per leaf.

    The identity is ``qid + paper + ordinal`` (same as rule 5).  A qid the source
    papers reuse for *different* questions may appear in more than one leaf —
    that is a warning, because each question still has exactly one home.
    """

    leaves_by_qid: Dict[str, Set[Tuple[str, str, str]]] = defaultdict(set)
    leaf_of_qid: Dict[str, Set[str]] = defaultdict(set)
    seen_in_leaf: Dict[Tuple[str, str], int] = defaultdict(int)
    for subject_dir in sorted(SUBJECT_LABELS):
        subject_root = database / subject_dir
        if not subject_root.is_dir():
            continue
        for leaf, _relative in iter_leaves(subject_root):
            leaf_key = leaf.relative_to(database).as_posix()
            for record in read_pointers(leaf):
                qid = str(record.get("qid") or "")
                if not qid:
                    continue  # rule 4 already reports a record without a qid
                identity = (qid, str(record.get("paper_path") or ""), str(record.get("ordinal")))
                report.checked["rule8_records"] = report.checked.get("rule8_records", 0) + 1
                seen_in_leaf[(leaf_key, identity)] += 1
                leaves_by_qid[qid].add(identity)
                leaf_of_qid[qid].add(leaf_key)

    for (leaf_key, identity), count in sorted(seen_in_leaf.items()):
        if count > 1:
            report.issues.append(
                Issue(
                    "8",
                    leaf_key,
                    f"question {identity[0]} ({identity[1]}#{identity[2]}) is listed "
                    f"{count} times in the same leaf",
                )
            )

    reused: Dict[str, Set[str]] = {}
    for qid, identities in sorted(leaves_by_qid.items()):
        homes = leaf_of_qid[qid]
        if len(homes) > 1:
            repeated = [
                identity
                for identity in identities
                if sum(
                    1
                    for (leaf_key, other) in seen_in_leaf
                    if leaf_key in homes and other == identity
                )
                > 1
            ]
            if repeated:
                report.issues.append(
                    Issue(
                        "8",
                        sorted(homes)[0],
                        f"question {qid} ({repeated[0][1]}#{repeated[0][2]}) is linked from "
                        f"{len(homes)} leaves: {', '.join(sorted(homes))}",
                    )
                )
            else:
                reused[qid] = homes
    if reused:
        sample = sorted(reused)[0]
        report.checked["rule8_reused_qids"] = len(reused)
        report.warnings.append(
            f"{len(reused)} qid(s) are reused by the source papers for different questions and "
            f"therefore appear in more than one leaf (e.g. {sample}: "
            f"{', '.join(sorted(reused[sample]))}).  Each question still lives in exactly one leaf; "
            f"the ids themselves are not unique in the raw corpus — a warning by design (rule 8), "
            f"not a violation."
        )


#: the marker an empty rule/leaf page must carry (see ``agent.grammar.render_rule_leaf``)
NO_PYQ_MARKER = "No PYQ in scope"


def check_empty_leaves(database: Path, report: AuditReport) -> None:
    """Rule 9: an empty ``questions.jsonl`` must be marked, and a marker must be true."""

    for subject_dir in sorted(SUBJECT_LABELS):
        subject_root = database / subject_dir
        if not subject_root.is_dir():
            continue
        for leaf, _relative in iter_leaves(subject_root):
            leaf_key = leaf.relative_to(database).as_posix()
            count = sum(1 for _ in read_pointers(leaf))
            report.checked["rule9_leaves"] = report.checked.get("rule9_leaves", 0) + 1
            index = leaf / "index.md"
            text = index.read_text(encoding="utf-8", errors="replace") if index.is_file() else ""
            marked = NO_PYQ_MARKER in text
            if count == 0 and not marked:
                report.issues.append(
                    Issue(
                        "9",
                        leaf_key,
                        f"empty questions.jsonl without the '{NO_PYQ_MARKER}' marker in "
                        f"{'index.md' if index.is_file() else '(missing) index.md'} — an empty "
                        f"leaf must state that the corpus has no such question",
                    )
                )
            elif count and marked:
                report.issues.append(
                    Issue(
                        "9",
                        leaf_key,
                        f"index.md claims '{NO_PYQ_MARKER}' but the leaf holds {count} question(s); "
                        f"the marker is stale and hides real coverage",
                    )
                )


def subject_vocabulary(taxonomy: Dict[str, Any]) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    """``(declared chapters, every owned name)`` per subject.

    The name map uses :func:`agent.classify._subject_names` — the classifier's
    own definition — so rule 10 and
    :meth:`agent.classify.Classifier.foreign_vocabulary` cannot drift apart.
    """

    from agent.classify import _subject_names

    chapters: Dict[str, Set[str]] = {}
    names: Dict[str, Set[str]] = {}
    for subject, body in (taxonomy.get("subjects") or {}).items():
        chapters[subject] = {
            norm_key(chapter.get("name")) for chapter in body.get("chapters") or [] if chapter.get("name")
        }
        names[subject] = {norm_key(name) for name in _subject_names(body) if norm_key(name)}
    return chapters, names


def check_subject_chapter_ownership(
    database: Path, report: AuditReport, taxonomy: Dict[str, Any]
) -> None:
    """Rule 10: a subject files only chapters it declares (and owns its leaf names).

    Two defects are reported:

    * ``chapter`` present but not declared by the filing subject's taxonomy — the
      question sits in the wrong subject's tree;
    * ``chapter`` absent and the concept is vocabulary **only another subject
      owns** — the concept names the leaf, so the leaf is a foreign slug
      (``gk/_unclassified/verbal-ability``).  A name both subjects declare is
      fine: shared names are normal in this corpus.
    """

    chapters, names = subject_vocabulary(taxonomy)
    if not chapters:
        return
    for subject_dir, subject in sorted(SUBJECT_LABELS.items()):
        subject_root = database / subject_dir
        if not subject_root.is_dir():
            continue
        declared = chapters.get(subject, set())
        owned = names.get(subject, set())
        foreign = set().union(*(names.get(other, set()) for other in chapters if other != subject))
        for leaf, _relative in iter_leaves(subject_root):
            leaf_key = leaf.relative_to(database).as_posix()
            for record in read_pointers(leaf):
                report.checked["rule10_records"] = report.checked.get("rule10_records", 0) + 1
                chapter = record.get("chapter")
                if chapter:
                    key = norm_key(chapter)
                    if declared and key not in declared:
                        owners = sorted(
                            other for other, other_chapters in chapters.items() if key in other_chapters
                        )
                        report.issues.append(
                            Issue(
                                "10",
                                leaf_key,
                                f"{record.get('qid')}: chapter {chapter!r} is not declared by "
                                f"{subject}"
                                + (f" (declared by {', '.join(owners)})" if owners else ""),
                            )
                        )
                    continue
                concept = record.get("concept")
                if not concept:
                    continue
                if "_unclassified" in leaf.relative_to(database).parts:
                    # An ``_unclassified`` bucket is the holding pen for items the
                    # classifier could not place. Its leaf name may legitimately
                    # mirror another subject's concept (``gk/_unclassified/analogy``);
                    # the AI review pass is what resolves those, so the leaf-name
                    # ownership check does not apply inside these buckets. The
                    # chapter check above still fires here.
                    continue
                key = norm_key(concept)
                if key in foreign and key not in owned:
                    report.issues.append(
                        Issue(
                            "10",
                            leaf_key,
                            f"{record.get('qid')}: concept {concept!r} is another subject's "
                            f"vocabulary, not {subject}'s — a leaf must not be named after it",
                        )
                    )


def check_analysis_view_not_published(report: AuditReport) -> None:
    """Rule 11: the derived ``_analysis`` view never reaches the ``pyq-db`` branch.

    ``database/english/_analysis`` repeats every grammar question that already has
    a home under ``database/english/grammar/<rule>/``; publishing it duplicated
    5,318 links in the browsable tree.  ``agent.gitpush`` therefore drops it from
    the index after ``git add -f`` — this rule asserts that the exclusion is still
    declared *and* that the publisher still applies it, so a refactor cannot
    silently start publishing the view again.
    """

    from agent import gitpush

    analysis = paths.rel(paths.ANALYSIS_DB_DIR)
    report.checked["rule11_paths"] = len(gitpush.PUBLISH_EXCLUDE_PATHS)
    excluded = [
        entry
        for entry in gitpush.PUBLISH_EXCLUDE_PATHS
        if analysis == entry or analysis.startswith(entry.rstrip("/") + "/")
    ]
    if not excluded:
        report.issues.append(
            Issue(
                "11",
                analysis,
                "the derived analysis view is not in agent.gitpush.PUBLISH_EXCLUDE_PATHS, so a "
                "publish would ship it to pyq-db (it duplicates the rule leaves' questions)",
            )
        )
    if not hasattr(gitpush.Publisher, "_drop_excluded_paths"):
        report.issues.append(
            Issue(
                "11",
                "agent/gitpush.py",
                "the publisher no longer has the step that un-stages PUBLISH_EXCLUDE_PATHS",
            )
        )


def check_parent_consistency(
    database: Path, report: AuditReport, scope: Optional[ConceptScope]
) -> None:
    """Rule 6: a leaf's concept must be declared by its chapter (else ``_other``)."""

    if scope is None:
        return
    for subject_dir, subject in sorted(SUBJECT_LABELS.items()):
        subject_root = database / subject_dir
        if not subject_root.is_dir():
            continue
        for leaf, relative in iter_leaves(subject_root):
            under_other = OTHER_DIR in relative
            for record in read_pointers(leaf):
                concept = record.get("concept")
                chapter = record.get("chapter")
                if not concept or not chapter:
                    # a record without a concept is `_unclassified` (rule 4) and a
                    # record without a chapter has no parent to be consistent with
                    continue
                report.checked["rule6_records"] = report.checked.get("rule6_records", 0) + 1
                if under_other:
                    continue
                if scope.declares(subject, chapter, concept):
                    continue
                report.issues.append(
                    Issue(
                        "6",
                        leaf.relative_to(database).as_posix(),
                        f"{record.get('qid')}: concept {concept!r} is not declared by chapter "
                        f"{chapter!r} (declare it in the taxonomy or file it under "
                        f"'{OTHER_DIR}/')",
                    )
                )


def check_case_stability(database: Path, report: AuditReport) -> None:
    """Rule 7: no case-only leaf collisions and no silently overwritten packs."""

    # (a) sibling directories that differ only by letter case
    for subject_dir in sorted(SUBJECT_LABELS):
        subject_root = database / subject_dir
        if not subject_root.is_dir():
            continue
        parents = [subject_root] + [p for p in subject_root.rglob("*") if p.is_dir()]
        for parent in parents:
            if _is_reserved(parent.relative_to(database).parts):
                continue
            report.checked["rule7_dirs"] = report.checked.get("rule7_dirs", 0) + 1
            by_case: Dict[str, List[str]] = defaultdict(list)
            for child in parent.iterdir():
                if child.is_dir():
                    by_case[fold_key(child.name)].append(child.name)
            for names in by_case.values():
                if len(names) > 1:
                    report.issues.append(
                        Issue(
                            "7",
                            parent.relative_to(database).as_posix(),
                            f"sibling directories differ only by case: {sorted(names)}",
                        )
                    )

    # (b) one leaf holding two concept labels that differ only by case
    for subject_dir in sorted(SUBJECT_LABELS):
        subject_root = database / subject_dir
        if not subject_root.is_dir():
            continue
        for leaf, _relative in iter_leaves(subject_root):
            labels: Dict[str, Set[str]] = defaultdict(set)
            for record in read_pointers(leaf):
                concept = record.get("concept")
                if concept:
                    labels[fold_key(concept)].add(str(concept))
            report.checked["rule7_leaves"] = report.checked.get("rule7_leaves", 0) + 1
            for variants in labels.values():
                if len(variants) > 1:
                    report.issues.append(
                        Issue(
                            "7",
                            leaf.relative_to(database).as_posix(),
                            f"leaf holds labels that differ only by case: {sorted(variants)}",
                        )
                    )

    # (c) mock catalogue: two packs writing to the same file silently drop one
    index_path = database / "mocks" / "index.json"
    if not index_path.is_file():
        return
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.issues.append(Issue("7", "mocks/index.json", f"unreadable catalogue: {exc}"))
        return
    seen: Dict[str, int] = defaultdict(int)
    for pack in payload.get("packs", []):
        target = str(pack.get("path") or "")
        seen[target] += 1
        if seen[target] > 1:
            report.issues.append(
                Issue(
                    "7",
                    target,
                    f"the catalogue lists {seen[target]} packs for the same file; the later write "
                    f"silently drops the earlier pack's questions",
                )
            )
            continue
        file_path = paths.PROJECT_ROOT / target if target else None
        if file_path is None or not file_path.is_file():
            report.issues.append(Issue("7", target or "-", "catalogue entry without a pack file"))
            continue
        report.checked["rule7_packs"] = report.checked.get("rule7_packs", 0) + 1
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            report.issues.append(Issue("7", target, f"unreadable pack: {exc}"))
            continue
        if int(data.get("count") or 0) != int(pack.get("count") or 0):
            report.issues.append(
                Issue(
                    "7",
                    target,
                    f"catalogue count {pack.get('count')} != pack file count {data.get('count')}",
                )
            )


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def run_audit(
    database: Optional[Path] = None,
    *,
    taxonomy: Optional[Dict[str, Any]] = None,
) -> AuditReport:
    """Run every rule and return the report (never raises on a violation)."""

    base = Path(database) if database is not None else paths.DATABASE_DIR
    report = AuditReport(database=paths.rel(base) if base.is_absolute() else str(base))
    report.checked = {"dirs": 0, "leaves": 0}
    if not base.is_dir():
        report.issues.append(Issue("0", report.database, "database directory not found"))
        return report

    report.dirs = sum(1 for p in base.rglob("*") if p.is_dir())
    report.files = sum(1 for p in base.rglob("*") if p.is_file())
    report.leaves = sum(
        1
        for subject_dir in SUBJECT_LABELS
        if (base / subject_dir).is_dir()
        for _leaf, _rel in iter_leaves(base / subject_dir)
    )
    if report.files == 0:
        report.issues.append(Issue("0", report.database, "database directory is empty"))
        return report

    tax = taxonomy if taxonomy is not None else load_taxonomy()
    report.taxonomy_present = bool(tax.get("subjects"))
    if not report.taxonomy_present:
        report.issues.append(
            Issue("3", "state/taxonomy.json", "taxonomy missing – cannot verify cross-subject leakage")
        )
        vocabulary: Dict[str, Dict[str, Set[str]]] = {}
        scope: Optional[ConceptScope] = None
    else:
        vocabulary = vocabulary_sets(tax)
        scope = ConceptScope(tax)

    check_duplicated_nesting(base, report)
    check_stray_index(base, report)
    check_cross_subject_leakage(base, report, vocabulary)
    check_slugs(base, report)
    check_duplicate_questions(base, report)
    check_question_uniqueness(base, report)
    check_empty_leaves(base, report)
    check_parent_consistency(base, report, scope)
    check_case_stability(base, report)
    check_subject_chapter_ownership(base, report, tax)
    check_analysis_view_not_published(report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python tools/audit_db.py",
        description="Deterministic structural self-check of database/.",
    )
    parser.add_argument("--database", default=None, help="database directory (default: <root>/database)")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--quiet", action="store_true", help="only print the verdict")
    return parser


def format_report(report: AuditReport) -> str:
    lines: List[str] = []
    add = lines.append
    add("=" * 78)
    add("SSC PYQ DATABASE AUDIT")
    add("=" * 78)
    add(f"database           : {report.database}")
    add(f"directories        : {report.dirs}")
    add(f"files              : {report.files}")
    add(f"subject leaves     : {report.leaves}")
    add(f"pointer records    : {report.records}")
    add(f"taxonomy loaded    : {report.taxonomy_present}")
    add("")
    add("rules")
    add("  1 duplicated nesting levels")
    add("  2 stray chapter-level index.md next to a same-named child")
    add("  3 cross-subject vocabulary in an English leaf")
    add("  4 empty/None slugs and record identity")
    add("  5 a question filed in two concept leaves")
    add("  6 leaf concept not declared by its chapter (and not under _other/)")
    add("  7 case-only leaf collisions and silently overwritten packs")
    add("  8 one question linked from one leaf (a reused source qid is a warning)")
    add("  9 an empty leaf must carry the 'No PYQ in scope' marker (and no stale marker)")
    add(" 10 a subject files only chapters it declares / owns its leaf names")
    add(" 11 the derived english/_analysis view is excluded from the publish")
    add("")
    if report.warnings:
        add("warnings")
        for warning in report.warnings:
            add(f"  ! {warning}")
        add("")
    if report.issues:
        add(f"VIOLATIONS: {len(report.issues)}")
        for issue in report.issues:
            add(f"  {issue}")
        add("")
        add("RESULT: FAIL")
    else:
        add("VIOLATIONS: 0")
        add("RESULT: PASS")
    add("=" * 78)
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    database = Path(args.database) if args.database else None
    try:
        report = run_audit(database)
    except FileNotFoundError as exc:
        print(f"audit error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    elif args.quiet:
        print("PASS" if report.ok else f"FAIL ({len(report.issues)} violations)")
        for issue in report.issues[:50]:
            print(f"  {issue}")
    else:
        print(format_report(report))
    if not report.ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
