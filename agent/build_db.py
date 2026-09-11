"""Database writer.

Layout produced under ``database/``::

    database/
      _meta/
        distribution.md      phase-0 distribution + priority lists
        coverage.md          coverage identity, signature validation, flagged papers
        schema.md            record schema for every generated file
        flagged_papers.jsonl one row per NEEDS_AI_REVIEW paper
        papers.jsonl         one row per in-scope paper
      <subject>/
        index.md
        <chapter>/
          index.md
          [<topic>/]
            index.md
            <concept>/
              index.md
              questions.jsonl
        _unclassified/
          index.md
          questions.jsonl
      english/_analysis/{vocabulary,grammar}/...
      mocks/...

The tree is built level by level from the records themselves and **a directory
level is never emitted when its name equals its parent's**: ``Vocabulary >
Vocabulary``, ``Grammar > Grammar`` or ``Computer Fundamentals > Computer
Fundamentals`` collapse into a single directory instead of ``a/a/``.  The full
chapter/topic/concept triple stays on every pointer record, so nothing is lost;
only the redundant path level disappears.  A node that both holds questions and
has children (for example ``english/grammar``) gets one ``index.md`` with the
child table *and* its own question counts, and one ``questions.jsonl``.

``questions.jsonl`` stores compact **pointer** records (no question text) so the
tree stays small; ``tools/resolve.py`` resolves a ``qid`` to the full question,
options, solution and images from the source paper JSON.
"""

from __future__ import annotations

import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import paths
from .util import Log, human_int, pct, slugify, write_jsonl, write_text

SUBJECT_DIRS = {
    "REAS": "reasoning",
    "GK": "gk",
    "MATH": "maths",
    "ENG": "english",
    "COMPUTER": "computer",
}
SUBJECT_DIR_FALLBACK = "_unknown"

UNCLASSIFIED_DIR = "_unclassified"

#: explicit bucket for a placed question whose concept the chapter does **not**
#: declare in the taxonomy (see ``agent.classify.OTHER_BUCKET`` and audit rule 6):
#: the question keeps its chapter, but the concept level is parked under
#: ``<chapter>/_other/<concept>/`` instead of pretending the chapter declares it.
OTHER_DIR = "_other"

#: directories under ``database/`` that are not question-tree nodes
RESERVED_DIRS = ("_meta", "_analysis", "mocks")

POINTER_FIELDS = (
    "qid",
    "n",
    "ordinal",
    "exam",
    "year",
    "shift",
    "subject",
    "chapter",
    "topic",
    "concept",
    "concept_raw",
    "class_source",
    "class_confidence",
    "confidence",
    "leaf_bucket",
    "has_image",
    "marks_pos",
    "marks_neg",
    "paper_path",
)


def subject_dir(subject: Optional[str]) -> str:
    if subject is None:
        return SUBJECT_DIR_FALLBACK
    return SUBJECT_DIRS.get(subject, subject.lower())


def pointer(record: Dict[str, Any]) -> Dict[str, Any]:
    return {field: record.get(field) for field in POINTER_FIELDS}


def _safe(name: str, fallback: str = "unnamed") -> str:
    return slugify(name, fallback=fallback)


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def _year_counts(records: Sequence[Dict[str, Any]]) -> Dict[int, int]:
    counter: Counter = Counter()
    for record in records:
        counter[int(record.get("year") or 0)] += 1
    return dict(sorted(counter.items()))


def _exam_counts(records: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counter: Counter = Counter()
    for record in records:
        counter[str(record.get("exam") or "UNKNOWN")] += 1
    return dict(sorted(counter.items()))


def render_leaf_body(records: Sequence[Dict[str, Any]]) -> str:
    """Counts + question ids for a leaf concept (shared by both page shapes)."""

    lines: List[str] = []
    add = lines.append
    if not records:
        add("_No questions._")
        add("")
        return "\n".join(lines)

    add("## By exam")
    add("")
    add(_table(["Exam", "Questions"], [[k, human_int(v)] for k, v in _exam_counts(records).items()]))
    add("")
    add("## By year")
    add("")
    add(_table(["Year", "Questions"], [[k, human_int(v)] for k, v in _year_counts(records).items()]))
    add("")
    add("## Question ids")
    add("")
    add(
        "Resolve any id with `python tools/resolve.py <qid>`."
    )
    add("")
    add("```text")
    for record in records[:200]:
        add(
            f"{record.get('qid')}  {record.get('exam')} {record.get('year')} "
            f"{record.get('paper_path')} #{record.get('ordinal')}"
        )
    if len(records) > 200:
        add(f"... {len(records) - 200} more (see questions.jsonl)")
    add("```")
    add("")
    return "\n".join(lines)


def render_leaf_index(
    *,
    title: str,
    kind: str,
    subject: str,
    chapter: str,
    topic: Optional[str],
    records: Sequence[Dict[str, Any]],
) -> str:
    lines: List[str] = []
    add = lines.append
    add(f"# {title}")
    add("")
    add(f"- **Level**: {kind}")
    add(f"- **Subject**: {subject}")
    add(f"- **Chapter**: {chapter}")
    if topic:
        add(f"- **Topic**: {topic}")
    add(f"- **Questions**: {human_int(len(records))}")
    add("")
    add(render_leaf_body(records))
    return "\n".join(lines)


def render_branch_index(
    *,
    title: str,
    kind: str,
    subject: str,
    total: int,
    children: Sequence[Tuple[str, str, int]],
) -> str:
    lines: List[str] = []
    add = lines.append
    add(f"# {title}")
    add("")
    add(f"- **Level**: {kind}")
    add(f"- **Subject**: {subject}")
    add(f"- **Questions (this subtree)**: {human_int(total)}")
    add(f"- **Children**: {len(children)}")
    add("")
    if children:
        add(_table(["Child", "Level", "Questions"], [[n, lv, human_int(c)] for n, lv, c in children]))
        add("")
    return "\n".join(lines)


def render_node_index(
    *,
    title: str,
    kind: str,
    subject: str,
    chapter: Optional[str],
    topic: Optional[str],
    own_records: Sequence[Dict[str, Any]],
    children: Sequence[Tuple[str, str, int]],
    total: int,
) -> str:
    """One ``index.md`` for a tree node.

    A node with children renders the child table; a node whose own records end
    at this level (a leaf concept) also renders the counts and question ids.  A
    collapsed node can be both (``english/grammar`` holds 8k concept records
    *and* the grammar topics) and then gets a single combined page.
    """

    if not children:
        # pure leaf: keep the historical concept page shape
        return render_leaf_index(
            title=title,
            kind=kind,
            subject=subject,
            chapter=chapter or title,
            topic=topic,
            records=own_records,
        )

    lines: List[str] = []
    add = lines.append
    add(f"# {title}")
    add("")
    add(f"- **Level**: {kind}")
    add(f"- **Subject**: {subject}")
    if chapter:
        add(f"- **Chapter**: {chapter}")
    if topic:
        add(f"- **Topic**: {topic}")
    add(f"- **Questions (this subtree)**: {human_int(total)}")
    if own_records:
        add(f"- **Questions at this level**: {human_int(len(own_records))}")
    add(f"- **Children**: {len(children)}")
    add("")
    add(_table(["Child", "Level", "Questions"], [[n, lv, human_int(c)] for n, lv, c in children]))
    add("")
    if own_records:
        add(render_leaf_body(own_records))
    return "\n".join(lines)


@dataclass
class TreeNode:
    """One directory of the question tree."""

    name: str
    level: str  # "subject" | "chapter" | "topic" | "concept"
    chapter: Optional[str] = None
    topic: Optional[str] = None
    records: List[Dict[str, Any]] = field(default_factory=list)
    children: Dict[str, "TreeNode"] = field(default_factory=dict)

    def subtree_count(self) -> int:
        return len(self.records) + sum(child.subtree_count() for child in self.children.values())

    def child_summaries(self) -> List[Tuple[str, str, int]]:
        return [
            (child.name, child.level, child.subtree_count())
            for child in sorted(self.children.values(), key=lambda node: node.name)
        ]


def _dir_name(name: Optional[str]) -> str:
    """Directory name for one chapter/topic/concept level.

    A level whose name produces no ASCII slug at all (``slugify`` would return
    its ``unnamed`` fallback) is stored under the ``_unclassified`` placeholder
    instead: the tree never contains a meaningless directory name.
    """

    value = (name or "").strip()
    if not value or value == UNCLASSIFIED_DIR:
        return UNCLASSIFIED_DIR
    return _safe(value, fallback=UNCLASSIFIED_DIR)


def leaf_levels(record: Dict[str, Any]) -> Tuple[Tuple[str, str], ...]:
    """``(slug, level)`` pairs below the subject for one record.

    Consecutive levels with the same slug are merged: a resolved topic that
    equals its chapter (``Vocabulary > Vocabulary``) and a concept that equals
    its parent (``Antonym`` under topic ``Antonym``) each keep a single
    directory.  This is what removes the ``chapter/topic/topic`` duplication.

    A record flagged ``leaf_bucket == "_other"`` (its concept is not declared by
    its chapter — audit rule 6) gets an explicit ``_other`` level between the
    chapter and the concept, so the tree never claims the chapter declares a
    concept it does not.
    """

    levels: List[Tuple[str, str]] = []
    chapter = _dir_name(record.get("chapter"))
    topic = _dir_name(record["topic"]) if record.get("topic") else None
    concept = _dir_name(record.get("concept"))
    for name, level in ((chapter, "chapter"), (topic, "topic"), (concept, "concept")):
        if not name:
            continue
        if levels and levels[-1][0] == name:
            continue
        levels.append((name, level))
    if str(record.get("leaf_bucket") or "") == OTHER_DIR and len(levels) >= 2:
        levels.insert(1, (OTHER_DIR, "other"))
    return tuple(levels)


def build_subject_tree(records: Sequence[Dict[str, Any]], subject: str) -> TreeNode:
    """Nested tree for one subject, built only from the record levels."""

    root = TreeNode(name=subject_dir(subject), level="subject")
    for record in records:
        levels = leaf_levels(record)
        node = root
        for name, level in levels:
            child = node.children.get(name)
            if child is None:
                child = TreeNode(name=name, level=level)
                node.children[name] = child
            node = child
        node.records.append(record)
    return root


def prune_subject_tree(subject_root: Path) -> int:
    """Delete a subject's previous tree revision (reserved dirs are kept).

    The tree is regenerated from the index on every run, so the previous
    revision is removed first: a superseded directory (for example the old
    ``english/grammar/grammar`` duplicate or the pre-``_analysis`` vocabulary
    files) would otherwise survive as a stale shell next to the new layout.
    """

    if not subject_root.is_dir():
        return 0
    removed = 0
    for child in sorted(subject_root.iterdir(), key=lambda item: item.name):
        if child.name in RESERVED_DIRS:
            continue
        if child.is_dir():
            removed += 1 + sum(1 for _ in child.rglob("*"))
            shutil.rmtree(child)
        else:
            child.unlink()
            removed += 1
    return removed


def write_question_tree(
    records: Sequence[Dict[str, Any]],
    *,
    database_dir: Optional[Path] = None,
    log: Optional[Log] = None,
) -> Dict[str, Any]:
    """Write ``database/<subject>/<chapter>[/<topic>]/<concept>/``.

    Levels whose name equals their parent's are collapsed, so the tree contains
    no ``a/a/`` directory; every leaf concept owns exactly one ``index.md`` and
    one ``questions.jsonl``.  The subject subtree is rebuilt from scratch (the
    previous revision is pruned, reserved ``_analysis`` directories are kept).
    """

    base = database_dir or paths.DATABASE_DIR
    logger = log or Log("build_db")

    by_subject: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_subject[record.get("subject") or SUBJECT_DIR_FALLBACK].append(record)

    files_written = 0
    leaves = 0
    collapsed = 0

    def write_node(node: TreeNode, *, subject_label: str, parent_path: Path) -> None:
        nonlocal files_written, leaves
        directory = parent_path / node.name
        children = node.child_summaries() if node.children else []
        assert node.records or children, f"empty node {directory}"
        write_text(
            directory / "index.md",
            render_node_index(
                title=node.name,
                kind=node.level,
                subject=subject_label,
                chapter=node.chapter,
                topic=node.topic,
                own_records=node.records,
                children=children,
                total=node.subtree_count(),
            ),
        )
        files_written += 1
        if node.records:
            # a collapsed node can both hold questions and have children
            # (english/grammar -> 8k question records + the grammar topics);
            # it still owns exactly one questions.jsonl.
            write_jsonl(directory / "questions.jsonl", [pointer(r) for r in node.records])
            files_written += 1
            leaves += 1
        for child in sorted(node.children.values(), key=lambda item: item.name):
            write_node(child, subject_label=subject_label, parent_path=directory)

    for subject, subject_records in sorted(by_subject.items()):
        label = subject if subject != SUBJECT_DIR_FALLBACK else SUBJECT_DIR_FALLBACK
        tree = build_subject_tree(subject_records, subject)
        subject_root = base / tree.name
        pruned = prune_subject_tree(subject_root)
        if pruned:
            logger.info(f"{subject}: pruned {pruned} stale entries from the previous tree revision")
        # remember the record-level chapter/topic on the nodes so the leaf page
        # can show them even when the directory level was collapsed
        for record in subject_records:
            node = tree
            for name, _level in leaf_levels(record):
                node = node.children[name]
                if node.chapter is None and record.get("chapter"):
                    node.chapter = record["chapter"]
                if node.level == "concept" and record.get("topic"):
                    node.topic = record["topic"]

        subject_children = tree.child_summaries()
        write_text(
            subject_root / "index.md",
            render_branch_index(
                title=f"{label} — question bank",
                kind="subject",
                subject=label,
                total=len(subject_records),
                children=subject_children,
            ),
        )
        files_written += 1
        for child in sorted(tree.children.values(), key=lambda item: item.name):
            write_node(child, subject_label=label, parent_path=subject_root)

    # count the levels that were collapsed away (chapter == topic, concept ==
    # parent, unclassified == unclassified) for the phase report
    for record in records:
        emitted = len(leaf_levels(record))
        declared = 2 + (1 if record.get("topic") else 0)
        collapsed += max(0, declared - emitted)

    logger.info(
        f"question tree: {files_written} files across {len(by_subject)} subjects, "
        f"{leaves} concept leaves, {collapsed} duplicate levels collapsed"
    )
    return {
        "files": files_written,
        "subjects": sorted(by_subject),
        "leaves": leaves,
        "collapsed_levels": collapsed,
    }


# ---------------------------------------------------------------------------
# _meta
# ---------------------------------------------------------------------------


def render_coverage_md(
    coverage: Dict[str, Any],
    paper_reports: Sequence[Dict[str, Any]],
    *,
    hindi_variants: Dict[str, int],
    out_of_scope: Sequence[Dict[str, Any]],
) -> str:
    lines: List[str] = []
    add = lines.append
    add("# Coverage & Validation")
    add("")
    add("## Coverage identity")
    add("")
    add("```text")
    add(
        "placed + skipped_hindi + unclassified + flagged_papers_questions "
        f"= {coverage['placed']} + {coverage['skipped_hindi']} + {coverage['unclassified']}"
        f" + {coverage['flagged_papers_questions']} = {coverage['accounted']}"
    )
    add(f"in-scope questions                                        = {coverage['questions']}")
    add(f"balanced = {coverage['balanced']}")
    add("```")
    add("")
    rows = [
        ["placed", human_int(coverage["placed"]), "subject assigned AND concept mapped to the taxonomy"],
        [
            "unclassified",
            human_int(coverage["unclassified"]),
            "subject assigned, concept not mappable (incl. source label `Unidentified`)",
        ],
        [
            "skipped_hindi",
            human_int(coverage["skipped_hindi"]),
            "Devanagari present in the question prompt or one of its options",
        ],
        [
            "flagged_papers_questions",
            human_int(coverage["flagged_papers_questions"]),
            "paper failed signature validation and no confident layout could be derived",
        ],
    ]
    add(_table(["Bucket", "Questions", "Meaning"], rows))
    add("")

    add("## Section-signature validation")
    add("")
    add(
        f"- Papers checked: **{human_int(coverage['papers'])}**\n"
        f"- Validated with section-level agreement >= "
        f"{pct(coverage.get('min_section_agreement', 0.95) * 100, 100):.0f}% "
        f"(primary) or question-level agreement >= "
        f"{pct(coverage.get('min_agreement', 0.95) * 100, 100):.0f}% when the paper has no declared "
        f"section: **{human_int(coverage['papers_validated'])}** "
        f"({pct(coverage['papers_validated'], coverage['papers']):.1f}%)\n"
        f"- Flagged `NEEDS_AI_REVIEW`: **{human_int(coverage['papers_flagged'])}** "
        f"({pct(coverage['papers_flagged'], coverage['papers']):.1f}%)\n"
        f"- Placeable (subject assigned to every non-Hindi question): "
        f"**{human_int(coverage['papers_placeable'])}**"
    )
    add("")
    add(
        "Two independent signals are compared.  **Primary (section level)**: for every positional "
        "section of the declared layout, the modal keyword subject of its questions must equal the "
        "declared subject — one or two noisy source labels cannot flip a whole section.  "
        "**Secondary (question level)**: the per-question agreement, kept for the report and used "
        "as the verdict only when the paper has no declared layout at all."
    )
    add("")
    by_exam: Dict[str, Dict[str, int]] = defaultdict(lambda: {"papers": 0, "ok": 0, "flagged": 0, "questions": 0, "hindi": 0})
    for report in paper_reports:
        bucket = by_exam[report["exam"]]
        bucket["papers"] += 1
        bucket["questions"] += int(report.get("question_count", 0))
        bucket["hindi"] += int(report.get("hindi_count", 0))
        if report.get("signature", {}).get("ok"):
            bucket["ok"] += 1
        if report.get("flagged"):
            bucket["flagged"] += 1
    add(
        _table(
            ["Exam", "Papers", "Validated", "Flagged", "Questions", "Skipped Hindi"],
            [
                [
                    exam,
                    human_int(b["papers"]),
                    human_int(b["ok"]),
                    human_int(b["flagged"]),
                    human_int(b["questions"]),
                    human_int(b["hindi"]),
                ]
                for exam, b in sorted(by_exam.items())
            ],
        )
    )
    add("")

    add("## Flagged papers")
    add("")
    flagged = [r for r in paper_reports if r.get("flagged")]
    if not flagged:
        add("_None._")
    else:
        add(
            _table(
                [
                    "Exam",
                    "Year",
                    "Paper",
                    "Q",
                    "Section agreement",
                    "Sections",
                    "Agreement (question level)",
                    "Comparable",
                    "Layout used",
                    "Layout agreement",
                ],
                [
                    [
                        r["exam"],
                        r["year"],
                        r["title"][:60],
                        r["question_count"],
                        "-"
                        if r["signature"].get("section_agreement") is None
                        else r["signature"]["section_agreement"],
                        f"{r['signature'].get('section_hits', 0)}/{r['signature'].get('section_total', 0)}",
                        r["signature"]["agreement"],
                        r["signature"]["comparable"],
                        r["layout_source"],
                        "-" if r.get("layout_agreement") is None else r["layout_agreement"],
                    ]
                    for r in flagged
                ],
            )
        )
        add("")
        add(
            "A flagged paper is **not** silently mis-assigned: the layout is re-derived from the "
            "paper's own `n` resets plus per-section keyword majorities and the result is recorded "
            "in `state/papers.json` (`detected.spans`). Papers where no confident layout could be "
            "derived contribute to `flagged_papers_questions` and are excluded from the subject "
            "databases."
        )
    add("")

    add("## Hindi detection variants")
    add("")
    add(
        _table(
            ["Detector", "Questions flagged as Hindi"],
            [[name, human_int(count)] for name, count in hindi_variants.items()],
        )
    )
    add("")
    add(
        "The run uses `question + options` with the specified `[\\u0900-\\u097F]` range. "
        "Including the solution field is **not** equivalent: geometry solutions in this corpus use "
        "the Devanagari danda `।।` as a parallel symbol, which the raw range matches."
    )
    add("")

    add("## Out-of-scope papers")
    add("")
    add(f"- Papers dropped by the year filter ({coverage_papers_note(out_of_scope)}): "
        f"**{human_int(len(out_of_scope))}**")
    add("")
    return "\n".join(lines)


def coverage_papers_note(out_of_scope: Sequence[Dict[str, Any]]) -> str:
    return "detected year outside the configured range"


def render_schema_md() -> str:
    lines: List[str] = []
    add = lines.append
    add("# Generated data schema")
    add("")
    add("## `state/`")
    add("")
    add(
        _table(
            ["File", "Contents"],
            [
                ["taxonomy.json", "subject -> chapter/family -> topic -> concept parsed from `chapter-and-topic/*.md`"],
                ["alias_map.json", "normalised raw concept/tag string -> canonical concept (+chapter/topic/subject/source)"],
                ["subject_keywords.json", "the compiled keyword table used for the paper-signature check"],
                ["papers.json", "one record per in-scope paper + its layout analysis (canonical, detected, signature)"],
                [
                    "index/<subject>.<n>.jsonl",
                    "one record per in-scope non-Hindi question (see below), sharded by subject; "
                    "no shard exceeds 25 MB because GitHub rejects files > 100 MB",
                ],
                ["index/manifest.json", "shard list with per-shard record count, bytes and sha256 + the qid-list sha256"],
                ["index/ALL.jsonl.gz", "optional single-file gzip copy (`PYQ_INDEX_GZIP=1`)"],
                ["questions_index.jsonl", "deprecated single-file index; read as a fallback when no manifest exists"],
                ["distribution.json", "phase-0 distribution, deltas, importance, priority lists"],
                ["progress.json", "per-phase status/counters"],
                ["checkpoint.json", "resume point + run policy snapshot"],
                ["journal.jsonl", "append-only run journal"],
                ["manifest.json", "files produced per phase with sizes and sha256"],
                ["disputes.jsonl", "items the two-model debate could not settle"],
                ["webcache.json", "Monid TinyFish search/fetch cache"],
                ["verify_state.json", "resumable AI-verification cursor"],
            ],
        )
    )
    add("")
    add("## `state/index/` (sharded question index)")
    add("")
    add(
        _table(
            ["Field", "Type", "Meaning"],
            [
                ["qid", "str", "source question id"],
                ["paper_path", "str", "repo-relative path of the source paper JSON"],
                ["test_id", "str", "source paper id"],
                ["exam", "str", "CGL / CHSL / CPO / MTS / GD / SELECTION_POST / STENO"],
                ["year", "int", "year from the deepest folder segment matching 20xx, else from the title"],
                ["shift", "int|null", "shift parsed from the title"],
                ["ordinal", "int", "global position in the paper (index + 1)"],
                ["n", "int", "source position inside its section (restarts at 1 per section)"],
                ["subject", "str", "REAS / GK / MATH / ENG / COMPUTER"],
                ["subject_source", "str", "canonical | detected | unvalidated"],
                ["signature_ok", "bool", "paper passed the 95% positional/keyword check"],
                ["lang", "str", "always `en` (Hindi questions are excluded from the index)"],
                ["concept_raw", "str", "source `concept` string verbatim"],
                ["concept", "str|null", "canonical concept from the alias map / fallback ladder"],
                ["chapter", "str|null", "taxonomy chapter (null -> unclassified)"],
                ["topic", "str|null", "taxonomy topic"],
                [
                    "leaf_bucket",
                    "str|null",
                    "`_other` when the chapter does not declare the concept (the leaf gets an "
                    "explicit `_other` level); `null` when the chapter declares it",
                ],
                ["class_source", "str", "alias | chapter | topic | keyword | unmapped"],
                ["has_image", "bool", "an `[IMAGE: url]` marker exists in prompt or solution; the urls are re-read from the source paper by tools/resolve.py"],
                ["marks_pos / marks_neg", "float", "source marking scheme"],
                ["status", "str", "placed | unclassified"],
            ],
        )
    )
    add("")
    add(
        "Sharding is deterministic: records keep their order, are grouped by subject and a shard is "
        "closed before it would exceed 25 MB, so re-running the phase produces byte-identical shards. "
        "`state/index/manifest.json` records each shard's path, record count, size and sha256 plus a "
        "sha256 over the sorted `qid` list, which is the index's content identity. "
        "`agent.indexer.read_index()` reads shards, legacy file or gzip copy transparently."
    )
    add("")
    add("## `database/`")
    add("")
    add(
        _table(
            ["Path", "Contents"],
            [
                ["_meta/distribution.md", "phase-0 distribution and priority lists"],
                ["_meta/coverage.md", "coverage identity, signature validation, flagged papers"],
                ["_meta/schema.md", "this file"],
                ["_meta/PROGRESS.md", "phase status, counters, artifacts"],
                ["_meta/papers.jsonl", "one row per in-scope paper"],
                ["_meta/flagged_papers.jsonl", "one row per NEEDS_AI_REVIEW paper"],
                ["<subject>/index.md", "subject roll-up"],
                ["<subject>/<chapter>/index.md", "chapter roll-up with topic children"],
                ["<subject>/<chapter>/<topic>/index.md", "topic roll-up with concept children"],
                ["<subject>/<chapter>/<topic>/<concept>/index.md", "concept page: counts by exam/year + question ids"],
                ["<subject>/<chapter>/_other/<concept>/", "placed question whose concept the chapter does not declare"],
                ["<subject>/<chapter>/<topic>/<concept>/questions.jsonl", "compact pointer records (see POINTER_FIELDS)"],
                ["<subject>/_unclassified/**", "questions whose concept is not mappable"],
                ["english/_analysis/vocabulary/*.md, all.json", "synonym/antonym/OWS/idiom/spelling/homonym rankings (derived view)"],
                ["english/_analysis/grammar/*.md, *.json, *.jsonl", "grammar questions mapped to the 129 rules (derived view)"],
                ["english/solved-items.json", "phase-1 summary: vocabulary + grammar counters and file lists"],
                ["<subject>/analysis.json", "per-subject chapter/topic/concept counters"],
                ["mocks/index.json", "mock pack catalogue"],
                ["mocks/packs/<kind>/<id>.json", "mock pack payloads (`{kind,id,subject,count,exam_filter,year_range,questions}`)"],
            ],
        )
    )
    add("")
    add(
        "**Tree levels.** A directory level is never emitted when its name equals its parent's "
        "(`Vocabulary > Vocabulary`, `Antonym` under topic `Antonym`, `_unclassified` under "
        "`_unclassified` all collapse to a single directory), so no path contains `a/a/`. A "
        "collapsed node can hold questions *and* children (`english/grammar`); it still owns "
        "exactly one `index.md` and one `questions.jsonl`. The chapter/topic/concept triple stays "
        "on every pointer record. `_analysis` and `_meta` are reserved and never question leaves. "
        "Records flagged `leaf_bucket=_other` (the chapter does not declare the concept) get an "
        "explicit `_other` level between chapter and concept, so a leaf never contradicts the "
        "concept's taxonomy parent. "
        "`python -m agent.cli audit` re-checks all of this."
    )
    add("")
    add("## Pointer record (`questions.jsonl`)")
    add("")
    add("```json")
    add(", ".join(f'"{f}"' for f in POINTER_FIELDS))
    add("```")
    add("")
    add(
        "Question text, options, solution and image markers are **not** duplicated into the "
        "database. `tools/resolve.py <qid>` re-reads the source paper and prints the full record."
    )
    add("")
    return "\n".join(lines)


def write_meta(
    *,
    distribution: Dict[str, Any],
    coverage: Dict[str, Any],
    paper_reports: Sequence[Dict[str, Any]],
    out_of_scope: Sequence[Dict[str, Any]],
    hindi_variants: Dict[str, int],
    database_dir: Optional[Path] = None,
    log: Optional[Log] = None,
) -> Dict[str, Any]:
    base = database_dir or paths.DATABASE_DIR
    meta = base / "_meta"
    write_text(meta / "coverage.md", render_coverage_md(coverage, paper_reports, hindi_variants=hindi_variants, out_of_scope=out_of_scope))
    write_text(meta / "schema.md", render_schema_md())
    write_jsonl(
        meta / "papers.jsonl",
        [
            {
                "path": r["path"],
                "test_id": r["test_id"],
                "title": r["title"],
                "exam": r["exam"],
                "year": r["year"],
                "shift": r["shift"],
                "question_count": r["question_count"],
                "hindi_count": r["hindi_count"],
                "paper_kind": r["paper_kind"],
                "section_sizes": r["section_sizes"],
                "layout_source": r["layout_source"],
                "signature_ok": r["signature"]["ok"],
                "signature_basis": r["signature"].get("basis"),
                "agreement": r["signature"]["agreement"],
                "section_agreement": r["signature"].get("section_agreement"),
                "flagged": r["flagged"],
                "placeable": r["placeable"],
                "flagged_questions": r.get("flagged_questions", 0),
            }
            for r in paper_reports
        ],
    )
    write_jsonl(
        meta / "flagged_papers.jsonl",
        [
            {
                "path": r["path"],
                "title": r["title"],
                "exam": r["exam"],
                "year": r["year"],
                "agreement": r["signature"]["agreement"],
                "comparable": r["signature"]["comparable"],
                "section_agreement": r["signature"].get("section_agreement"),
                "section_hits": r["signature"].get("section_hits"),
                "section_total": r["signature"].get("section_total"),
                "section_mismatches": r["signature"].get("section_mismatches", []),
                "canonical_spans": r["canonical_spans"],
                "detected_spans": None if r["detected"] is None else r["detected"]["spans"],
                "detected_confidence": None if r["detected"] is None else r["detected"]["confidence"],
                "layout_source": r["layout_source"],
                "mismatch_samples": r["signature"].get("mismatches", []),
                "status": "resolved-by-detected-layout" if r["placeable"] else "not-placed",
            }
            for r in paper_reports
            if r["flagged"]
        ],
    )
    return {"coverage": str(meta / "coverage.md"), "schema": str(meta / "schema.md")}
