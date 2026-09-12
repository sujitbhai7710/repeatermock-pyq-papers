"""Point a test run's generated state at a temporary directory.

Why this module exists (incident L25, see ``LESSONS.md``)::

    python -m unittest discover -s tests          # no -t .
    python -m unittest discover -s tests -t .     # the documented form

Without ``-t .`` unittest makes ``tests/`` the *top-level* directory, imports the
test modules as top-level modules (``test_failover``, not
``tests.test_failover``) and therefore never imports the ``tests`` package: the
redirect that lives in ``tests/__init__.py`` does not run, so the router-outage
tests append their simulated failures to the real, append-only
``state/errors.jsonl`` (840 rows in a single suite run, observed 2026-09-12).

Every test module imports this module **first**, in both discovery modes, so the
redirect can never be skipped:

* ``PYQ_STATE_DIR``      -> ``<tmp>/state``   (every generated artefact)
* ``PYQ_ERRORS_LEDGER``  -> ``<tmp>/state/errors.jsonl``  (the AI failure ledger)

A value set by the caller always wins, and :data:`BASELINE` records what the
repository's real ``state/`` looked like *before* the first test module was
imported, so ``tests/test_zz_state_isolation.py`` (which sorts last) can prove
the suite left it byte-identical.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# the repository's own state directory (located without importing ``agent``,
# which must see the environment variables below *before* it is imported)
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "config" / "settings.json").is_file() and (
            candidate / "chapter-and-topic"
        ).is_dir():
            return candidate
    return here.parent.parent


REPO_ROOT: Path = _repo_root()
#: the production state directory — the one a suite run must never write
REAL_STATE_DIR: Path = REPO_ROOT / "state"
#: the production AI failure ledger, called out explicitly because it is append-only
REAL_ERRORS_LEDGER: Path = REAL_STATE_DIR / "errors.jsonl"

#: names that are never part of the fingerprint (transient / not our output)
_IGNORED_PARTS = ("__pycache__",)


def fingerprint(root: Path) -> Dict[str, Tuple[int, int, str]]:
    """``{relative path: (size, mtime_ns, sha256)}`` for every file under *root*.

    Size + mtime is enough to notice a write and cheap enough to run over a
    100 MB tree; the digest is included because it is the only proof that
    ``state/errors.jsonl`` was not rewritten in place with identical length.
    """

    out: Dict[str, Tuple[int, int, str]] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*")):
        if path.is_dir() or any(part in _IGNORED_PARTS for part in path.parts):
            continue
        try:
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:  # pragma: no cover - a file that vanished mid-walk
            continue
        out[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns, digest)
    return out


#: the real ``state/`` as it was when the suite started (empty if there is none)
BASELINE: Dict[str, Tuple[int, int, str]] = fingerprint(REAL_STATE_DIR)
#: the real ledger's digest at the same instant
BASELINE_LEDGER_SHA256: str = (
    BASELINE.get("errors.jsonl", (0, 0, ""))[2] if REAL_ERRORS_LEDGER.is_file() else ""
)


# ---------------------------------------------------------------------------
# the redirect
# ---------------------------------------------------------------------------

_TMP_DIR: Optional[str] = None


def redirect() -> Optional[Path]:
    """Create the temporary state directory and point the run at it.

    Returns the directory, or ``None`` when the caller already redirected the
    state dir itself (an explicit ``PYQ_STATE_DIR`` wins).
    """

    global _TMP_DIR
    if os.environ.get("PYQ_STATE_DIR", "").strip():
        # the caller chose a tree of their own; only make sure the ledger is
        # inside it rather than in the repository
        os.environ.setdefault("PYQ_ERRORS_LEDGER", str(Path(os.environ["PYQ_STATE_DIR"]) / "errors.jsonl"))
        return None
    if _TMP_DIR is None:
        _TMP_DIR = tempfile.mkdtemp(prefix="pyq-test-state-")
    state = Path(_TMP_DIR) / "state"
    state.mkdir(parents=True, exist_ok=True)
    os.environ["PYQ_STATE_DIR"] = str(state)
    os.environ.setdefault("PYQ_ERRORS_LEDGER", str(state / "errors.jsonl"))
    return state


TEMP_STATE_DIR: Optional[Path] = redirect()
