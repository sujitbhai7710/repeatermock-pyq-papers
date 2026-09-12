"""Path resolution for the SSC PYQ agent.

The project root is auto-detected so the package runs correctly whether it is
invoked from the repository root, from a parent directory, or from CI.

``PYQ_PROJECT_ROOT`` relocates the whole tree and ``PYQ_STATE_DIR`` relocates
just the generated ``state/`` artefacts (:func:`find_state_dir`) — the latter is
how a test or scratch run keeps its writes out of the repository's own state.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# project root
# ---------------------------------------------------------------------------

_ROOT_MARKERS = (
    os.path.join("config", "settings.json"),
    os.path.join("chapter-and-topic"),
)


def find_project_root(start: Optional[Path] = None) -> Path:
    """Return the project root.

    Resolution order:

    1. ``PYQ_PROJECT_ROOT`` environment variable.
    2. The first ancestor of *start* (default: this file) that contains
       ``config/settings.json`` or ``chapter-and-topic/``.
    3. The parent directory of the ``agent`` package.
    """

    env = os.environ.get("PYQ_PROJECT_ROOT")
    if env:
        candidate = Path(env).expanduser()
        if candidate.is_dir():
            return candidate.resolve()

    origin = Path(start) if start is not None else Path(__file__).resolve()
    origin = origin.resolve()
    candidates: Iterable[Path] = (origin, *origin.parents)
    for candidate in candidates:
        if candidate.is_dir() and all((candidate / m).exists() for m in _ROOT_MARKERS):
            return candidate
    for candidate in (origin, *origin.parents):
        if (candidate / "chapter-and-topic").is_dir():
            return candidate

    return Path(__file__).resolve().parent.parent


def find_state_dir(root: Optional[Path] = None) -> Path:
    """Return the directory every generated artefact is written to.

    Resolution order:

    1. ``PYQ_STATE_DIR`` environment variable (relative paths are taken from the
       project root).
    2. ``<project root>/state``.

    The override exists so a scratch/CI/test run can keep **all** generated
    state out of the repository instead of only the error ledger: the test suite
    points it at a temporary directory (see ``tests/_isolation.py``), which is
    what makes a suite run unable to pollute production ``state/``.
    """

    env = os.environ.get("PYQ_STATE_DIR", "").strip()
    if env:
        candidate = Path(env).expanduser()
        if not candidate.is_absolute():
            candidate = (root or PROJECT_ROOT) / candidate
        return candidate
    return (root or PROJECT_ROOT) / "state"


PROJECT_ROOT: Path = find_project_root()

CONFIG_DIR: Path = PROJECT_ROOT / "config"
STATE_DIR: Path = find_state_dir()
DATABASE_DIR: Path = PROJECT_ROOT / "database"
DOCS_DIR: Path = PROJECT_ROOT / "docs"
TOOLS_DIR: Path = PROJECT_ROOT / "tools"
TESTS_DIR: Path = PROJECT_ROOT / "tests"
REPORT_DIR: Path = PROJECT_ROOT / "reports"

TAXONOMY_DIR: Path = PROJECT_ROOT / "chapter-and-topic"
PAPERS_ROOT: Path = PROJECT_ROOT

SETTINGS_FILE: Path = CONFIG_DIR / "settings.json"
EXAMS_FILE: Path = CONFIG_DIR / "exams.json"
SUPPLEMENTARY_TAXONOMY_FILE: Path = CONFIG_DIR / "supplementary_taxonomy.json"

# generated artefacts
TAXONOMY_JSON: Path = STATE_DIR / "taxonomy.json"
ALIAS_MAP_JSON: Path = STATE_DIR / "alias_map.json"
SUBJECT_KEYWORDS_JSON: Path = STATE_DIR / "subject_keywords.json"
#: The question index is **sharded**: ``state/index/<subject>.<n>.jsonl`` plus
#: ``state/index/manifest.json`` (sha256 per shard).  A single 100 MB JSONL file
#: cannot be pushed to GitHub (hard limit: 100 MB per file), which is why the
#: index is split by subject and capped at :data:`~agent.indexer.SHARD_MAX_BYTES`
#: per shard.  ``QUESTIONS_INDEX_JSONL`` is only read as a fallback.
INDEX_DIR: Path = STATE_DIR / "index"
INDEX_MANIFEST_JSON: Path = INDEX_DIR / "manifest.json"
INDEX_GZIP_JSONL: Path = INDEX_DIR / "ALL.jsonl.gz"
QUESTIONS_INDEX_JSONL: Path = STATE_DIR / "questions_index.jsonl"
PAPERS_JSON: Path = STATE_DIR / "papers.json"
DISTRIBUTION_JSON: Path = STATE_DIR / "distribution.json"
CHECKPOINT_JSON: Path = STATE_DIR / "checkpoint.json"
PROGRESS_JSON: Path = STATE_DIR / "progress.json"
JOURNAL_JSONL: Path = STATE_DIR / "journal.jsonl"
MANIFEST_JSON: Path = STATE_DIR / "manifest.json"
DISPUTES_JSONL: Path = STATE_DIR / "disputes.jsonl"
WEBCACHE_JSON: Path = STATE_DIR / "webcache.json"
VERIFY_STATE_JSON: Path = STATE_DIR / "verify_state.json"
#: the append-only AI failure ledger (:mod:`agent.errors`; ``PYQ_ERRORS_LEDGER``
#: overrides it so a test run can redirect the writes of the router itself)
ERRORS_JSONL: Path = STATE_DIR / "errors.jsonl"

META_DB_DIR: Path = DATABASE_DIR / "_meta"
#: The English vocabulary/grammar analyses are derived views, not question-tree
#: nodes.  They live under a reserved ``_analysis`` directory so that every
#: question-tree leaf keeps exactly one ``index.md`` and one ``questions.jsonl``
#: (``english/grammar`` is itself a concept leaf of the tree).
ANALYSIS_DB_DIR: Path = DATABASE_DIR / "english" / "_analysis"
VOCAB_DB_DIR: Path = ANALYSIS_DB_DIR / "vocabulary"
GRAMMAR_DB_DIR: Path = ANALYSIS_DB_DIR / "grammar"
#: The per-rule browsable nodes of the question tree:
#: ``database/english/grammar/<NN>-<slug>/{index.md,questions.jsonl,rule.json}``.
#: A rule leaf is the *one* home of the questions its rule owns — the concept tree
#: is re-filed without them (``agent.grammar.assigned_qids``, audit rule 5) — and
#: its ``rule.json`` marker is what ``agent.grammar._prune_rule_leaves``
#: recognises as a rule leaf when a rule is renamed or removed.
GRAMMAR_RULES_DB_DIR: Path = DATABASE_DIR / "english" / "grammar"
#: the chapter page that carries the rule matrix (written by ``agent.build_db``
#: and extended in place by :mod:`agent.grammar`)
GRAMMAR_CHAPTER_INDEX: Path = GRAMMAR_RULES_DB_DIR / "index.md"
MOCKS_DIR: Path = DATABASE_DIR / "mocks"

TAXONOMY_FILES = {
    "MATH": "SSC_CGL_Maths_Top_Level_Taxonomy_All_Posts_Improved.md",
    "REAS": "SSC_Reasoning_Master_Syllabus.md",
    "GK": "SSC_GK_GS_Master_Syllabus.md",
    "ENG": "english-grammar-rules.md",
}


def ensure_dir(path: Path) -> Path:
    """Create *path* (and parents) if missing and return it."""

    path.mkdir(parents=True, exist_ok=True)
    return path


def rel(path: Path) -> str:
    """Return *path* relative to the project root using forward slashes."""

    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()
