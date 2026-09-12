#!/usr/bin/env python
"""Demonstrate that audit rules 8-11 FAIL when deliberately violated.

The demo copies the *real* ``database/`` into a temporary directory, breaks one
rule at a time in the copy, runs the audit through ``agent.cli audit --database``
and prints the verdict.  The real tree is never touched.

Usage::

    python tools/audit_rule_demo.py            # copy + run all four demos
    python tools/audit_rule_demo.py --keep     # keep the temporary copy
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "tools"))

from agent import paths  # noqa: E402
from tools import audit_db  # noqa: E402


def _leaf(database: Path, relative: str) -> Path:
    return database / relative


def _read(database: Path, relative: str) -> List[str]:
    return (database / relative / "questions.jsonl").read_text(encoding="utf-8").splitlines()


def _write(database: Path, relative: str, lines: List[str]) -> None:
    (database / relative / "questions.jsonl").write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8"
    )


def _pick_leaf(database: Path, *, needs_questions: bool = True, exclude: str = "") -> str:
    """A subject leaf relative path (with or without questions)."""

    for subject_dir in sorted(audit_db.SUBJECT_LABELS):
        root = database / subject_dir
        if not root.is_dir():
            continue
        for leaf, relative in audit_db.iter_leaves(root):
            key = f"{subject_dir}/{'/'.join(relative)}"
            if key == exclude:
                continue
            rows = sum(1 for _ in audit_db.read_pointers(leaf))
            if (rows > 0) == needs_questions:
                return key
    raise SystemExit("no suitable leaf found")


# ---------------------------------------------------------------------------
# violations
# ---------------------------------------------------------------------------


def violate_rule8(database: Path) -> str:
    """Link one source question from two leaves."""

    first = _pick_leaf(database)
    rows = _read(database, first)
    victim = json.loads(rows[0])
    second = _pick_leaf(database, exclude=first)
    lines = _read(database, second)
    lines.append(json.dumps(victim, sort_keys=True))
    _write(database, second, lines)
    return f"copied qid {victim.get('qid')} from {first} into {second}"


def violate_rule9(database: Path) -> str:
    """Empty a leaf's questions.jsonl and drop its marker."""

    leaf = _pick_leaf(database)
    index = _leaf(database, leaf) / "index.md"
    text = index.read_text(encoding="utf-8") if index.is_file() else ""
    index.write_text(
        text.replace(audit_db.NO_PYQ_MARKER, "(marker removed)"), encoding="utf-8"
    )
    _write(database, leaf, [])
    return f"emptied {leaf}/questions.jsonl and removed its marker"


def violate_rule10(database: Path) -> str:
    """File a question under a chapter its subject does not declare."""

    source = _pick_leaf(database)
    row = json.loads(_read(database, source)[0])
    row["chapter"] = "Verbal Ability"  # a chapter only ENG declares
    row["subject"] = "GK"
    target = _leaf(database, "gk/_unclassified/verbal-ability")
    target.mkdir(parents=True, exist_ok=True)
    (target / "index.md").write_text("# verbal-ability\n", encoding="utf-8")
    (target / "questions.jsonl").write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    return (
        f"filed qid {row.get('qid')} under gk/_unclassified/verbal-ability "
        f"with chapter 'Verbal Ability'"
    )


def violate_rule11(database: Path) -> str:
    """Stop excluding the derived analysis view from the publish."""

    from agent import gitpush

    gitpush.PUBLISH_EXCLUDE_PATHS = ()
    return "cleared agent.gitpush.PUBLISH_EXCLUDE_PATHS"


VIOLATIONS: Dict[str, Callable[[Path], str]] = {
    "8": violate_rule8,
    "9": violate_rule9,
    "10": violate_rule10,
    "11": violate_rule11,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep the temporary copy")
    parser.add_argument("--only", default=None, help="run one rule (8, 9, 10 or 11)")
    args = parser.parse_args(argv)

    rules = [args.only] if args.only else list(VIOLATIONS)
    tmp = Path(tempfile.mkdtemp(prefix="pyq-audit-demo-"))
    target = tmp / "database"
    print(f"copying {paths.rel(paths.DATABASE_DIR)} -> {target}")
    shutil.copytree(paths.DATABASE_DIR, target)
    baseline = audit_db.run_audit(target)
    print(f"baseline audit of the copy: {len(baseline.issues)} violation(s)\n")

    failed = 0
    for rule in rules:
        broken = tmp / f"database-rule{rule}"
        if broken.exists():
            shutil.rmtree(broken)
        shutil.copytree(paths.DATABASE_DIR, broken)
        detail = VIOLATIONS[rule](broken)
        report = audit_db.run_audit(broken)
        hits = [issue for issue in report.issues if issue.rule == rule]
        print("-" * 78)
        print(f"rule {rule}: {detail}")
        print(f"  -> audit says: VIOLATIONS: {len(report.issues)} / RESULT: {'PASS' if report.ok else 'FAIL'}")
        if hits:
            failed += 1
            for issue in hits[:3]:
                print(f"     {issue}")
        else:
            print("     !! the rule did NOT fire (bad demo)")
        print()
        if not args.keep:
            shutil.rmtree(broken)

    print("=" * 78)
    print(f"{failed}/{len(rules)} demonstration(s) failed the audit as intended")
    print("=" * 78)
    if args.keep:
        print(f"kept: {tmp}")
    else:
        shutil.rmtree(tmp)
    return 0 if failed == len(rules) else 1


if __name__ == "__main__":
    raise SystemExit(main())
