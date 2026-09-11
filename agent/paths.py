"""Path resolution for the SSC PYQ agent.

The project root is auto-detected so the package runs correctly whether it is
invoked from the repository root, from a parent directory, or from CI.
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


PROJECT_ROOT: Path = find_project_root()

CONFIG_DIR: Path = PROJECT_ROOT / "config"
STATE_DIR: Path = PROJECT_ROOT / "state"
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

META_DB_DIR: Path = DATABASE_DIR / "_meta"
#: The English vocabulary/grammar analyses are derived views, not question-tree
#: nodes.  They live under a reserved ``_analysis`` directory so that every
#: question-tree leaf keeps exactly one ``index.md`` and one ``questions.jsonl``
#: (``english/grammar`` is itself a concept leaf of the tree).
ANALYSIS_DB_DIR: Path = DATABASE_DIR / "english" / "_analysis"
VOCAB_DB_DIR: Path = ANALYSIS_DB_DIR / "vocabulary"
GRAMMAR_DB_DIR: Path = ANALYSIS_DB_DIR / "grammar"
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
