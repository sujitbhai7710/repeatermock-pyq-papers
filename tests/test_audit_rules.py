"""Hard rules 8–11 of ``tools/audit_db.py`` (Issue 4).

Each rule exists so a regression *fails the audit* instead of silently shipping a
bad tree:

* rule 8 — one question is linked from one leaf (a source-reused **qid** is a
  warning, because it resolves to different questions);
* rule 9 — an empty ``questions.jsonl`` must carry the explicit
  ``No PYQ in scope`` marker, and a marker on a leaf that has questions is stale;
* rule 10 — a subject may only file chapters its taxonomy declares, and a
  chapterless leaf must not be named after another subject's vocabulary (the
  fixed cross-subject check, ``tools/cross_subject_audit.py``);
* rule 11 — the derived ``english/_analysis`` view must never be published to
  ``pyq-db``.

Every test builds a throw-away ``database/`` in a temporary directory, so the
suite never touches the real tree.  Each rule is exercised twice: silent when the
tree is correct, ``VIOLATIONS`` when it is deliberately violated.
"""


from __future__ import annotations

# The suite must never write the repository's own ``state/`` (LESSONS.md L25):
# imported before any ``agent`` module so ``PYQ_STATE_DIR``/``PYQ_ERRORS_LEDGER``
# are set first, and importable in both discovery modes (``tests.test_x`` with
# ``-t .``, the top-level ``test_x`` without).
try:  # pragma: no cover - the import name depends on the discovery mode
    from tests import _isolation  # noqa: F401
except ImportError:  # pragma: no cover
    import _isolation  # type: ignore[no-redef]  # noqa: F401


import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from agent import gitpush, paths

import importlib.util
import sys

# ``tools`` is not a package: load the module the same way the CLI does
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import audit_db  # noqa: E402


#: a taxonomy that declares one chapter per subject; ``verbal-ability`` is ENG's
TAXONOMY: Dict[str, Any] = {
    "subjects": {
        "ENG": {
            "chapters": [
                {
                    "name": "Verbal Ability",
                    "topics": [{"name": "Reading Comprehension", "concepts": [{"name": "Cloze Test"}]}],
                    "concepts": [{"name": "Verbal Ability"}],
                }
            ]
        },
        "GK": {"chapters": [{"name": "Static GK", "concepts": [{"name": "Static GK"}]}]},
        "MATH": {"chapters": [{"name": "Algebra", "concepts": [{"name": "Algebra"}]}]},
        "REAS": {"chapters": [{"name": "Analogy", "concepts": [{"name": "Analogy"}]}]},
        "COMPUTER": {"chapters": [{"name": "Computer Fundamentals", "concepts": [{"name": "Hardware"}]}]},
    }
}


def pointer(qid: str, *, subject: str, chapter=None, concept=None, ordinal: int = 1, paper: str = "p.json") -> Dict[str, Any]:
    return {
        "qid": qid,
        "subject": subject,
        "chapter": chapter,
        "topic": None,
        "concept": concept,
        "concept_raw": concept,
        "ordinal": ordinal,
        "paper_path": paper,
        "exam": "CGL",
        "year": 2024,
    }


class AuditFixture(unittest.TestCase):
    """Builds a minimal, valid database tree that the audit must pass."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "database"
        (self.root / "reasoning" / "_unclassified").mkdir(parents=True)
        (self.root / "english" / "verbal-ability").mkdir(parents=True)
        self.write(
            "reasoning/_unclassified",
            [pointer("q-reas", subject="REAS")],
            index="# _unclassified\n\nQuestions whose concept is not mappable.\n",
        )
        self.write(
            "english/verbal-ability",
            [pointer("q-eng", subject="ENG", chapter="Verbal Ability", concept="Verbal Ability")],
            index="# verbal-ability\n",
        )

    # -- helpers -----------------------------------------------------------
    def leaf(self, relative: str) -> Path:
        return self.root / relative

    def write(self, relative: str, records: List[Dict[str, Any]], *, index: str = "# leaf\n") -> None:
        directory = self.leaf(relative)
        directory.mkdir(parents=True, exist_ok=True)
        body = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
        (directory / "questions.jsonl").write_text(body, encoding="utf-8")
        if index is not None:
            (directory / "index.md").write_text(index, encoding="utf-8")

    def rules(self) -> List[str]:
        report = audit_db.run_audit(self.root, taxonomy=TAXONOMY)
        return [issue.rule for issue in report.issues]

    def failures(self) -> List[str]:
        report = audit_db.run_audit(self.root, taxonomy=TAXONOMY)
        return [str(issue) for issue in report.issues]

    def assertClean(self) -> None:
        report = audit_db.run_audit(self.root, taxonomy=TAXONOMY)
        self.assertTrue(report.ok, f"the fixture must be clean, got: {[str(i) for i in report.issues]}")


# ---------------------------------------------------------------------------
# rule 8 — one question, one link
# ---------------------------------------------------------------------------


class RuleEightTests(AuditFixture):
    def test_a_clean_tree_has_no_rule8_violation(self) -> None:
        self.assertClean()
        self.assertNotIn("8", self.rules())

    def test_the_same_question_in_two_leaves_fails(self) -> None:
        """The identical source question in two leaves must be a violation."""

        self.write(
            "english/verbal-ability",
            [
                pointer("q-eng", subject="ENG", chapter="Verbal Ability", concept="Verbal Ability"),
                pointer("q-dup", subject="REAS", ordinal=7),
            ],
        )
        self.write("reasoning/analogy", [pointer("q-dup", subject="REAS", ordinal=7)])
        issues = self.failures()
        self.assertIn("8", self.rules(), f"rule 8 must fire, got {issues}")
        self.assertTrue(any("linked from" in issue for issue in issues), issues)

    def test_the_same_question_twice_in_one_leaf_fails(self) -> None:
        """A leaf that lists one source question twice must be a violation."""

        self.write(
            "reasoning/_unclassified",
            [pointer("q-twice", subject="REAS"), pointer("q-twice", subject="REAS")],
            index="# _unclassified\n",
        )
        self.assertIn("8", self.rules())
        self.assertTrue(any("listed 2 times in the same leaf" in issue for issue in self.failures()))

    def test_a_source_reused_qid_is_a_warning_not_a_violation(self) -> None:
        """Two *different* questions sharing one source qid is the documented carve-out."""

        self.write(
            "gk/_unclassified",
            [pointer("q-shared", subject="GK", ordinal=3)],
            index="# _unclassified\n",
        )
        self.write(
            "reasoning/_unclassified",
            [pointer("q-shared", subject="REAS", ordinal=42)],
            index="# _unclassified\n",
        )
        report = audit_db.run_audit(self.root, taxonomy=TAXONOMY)
        self.assertNotIn("8", [issue.rule for issue in report.issues])
        self.assertEqual(len(report.warnings), 1, f"exactly one warning expected: {report.warnings}")
        self.assertIn("reused by the source papers for different questions", report.warnings[0])
        self.assertIn("a warning by design (rule 8), not a violation", report.warnings[0])
        self.assertTrue(report.ok, [str(issue) for issue in report.issues])


# ---------------------------------------------------------------------------
# rule 9 — an empty leaf must say so
# ---------------------------------------------------------------------------


class RuleNineTests(AuditFixture):
    def test_an_empty_leaf_without_the_marker_fails(self) -> None:
        self.write("english/grammar", [], index="# grammar\n")

        self.assertIn("9", self.rules())
        self.assertTrue(any("empty questions.jsonl without" in issue for issue in self.failures()))

    def test_an_empty_leaf_with_the_marker_passes(self) -> None:
        self.write(
            "english/grammar",
            [],
            index="# Rule 1\n\n**No PYQ in scope 2019–2025** — the corpus has no such question.\n",
        )
        self.assertClean()
        self.assertNotIn("9", self.rules())

    def test_a_stale_marker_on_a_non_empty_leaf_fails(self) -> None:
        """The marker may not hide real questions."""

        self.write(
            "english/grammar",
            [pointer("q-g", subject="ENG", chapter="Verbal Ability", concept="Verbal Ability")],
            index="# Rule 1\n\n**No PYQ in scope 2019–2025**\n",
        )
        self.assertIn("9", self.rules())
        self.assertTrue(any("the marker is stale" in issue for issue in self.failures()))

    def test_an_empty_leaf_without_an_index_page_fails(self) -> None:
        self.write("english/grammar", [], index=None)
        (self.leaf("english/grammar") / "index.md").unlink(missing_ok=True)
        self.assertIn("9", self.rules())


# ---------------------------------------------------------------------------
# rule 10 — a subject files only chapters it declares
# ---------------------------------------------------------------------------


class RuleTenTests(AuditFixture):
    def test_a_chapter_the_subject_does_not_declare_fails(self) -> None:
        """The Issue-1 defect: a GK leaf holding an English chapter."""

        self.write(
            "gk/verbal-ability",
            [pointer("q-wrong", subject="GK", chapter="Verbal Ability", concept="Verbal Ability")],
        )
        self.assertIn("10", self.rules())
        self.assertTrue(any("is not declared by GK" in issue for issue in self.failures()))

    def test_a_chapterless_leaf_named_after_another_subject_fails(self) -> None:
        """A real leaf named after another subject's vocabulary is a violation."""

        self.write("gk/verbal-ability", [pointer("q-foreign", subject="GK", concept="Verbal Ability")])
        self.assertIn("10", self.rules())
        self.assertTrue(any("is another subject's vocabulary" in issue for issue in self.failures()))

    def test_an_unclassified_bucket_is_exempt_from_the_leaf_name_check(self) -> None:
        """``gk/_unclassified/analogy`` is the holding pen: the AI review resolves it."""

        self.write(
            "gk/_unclassified/analogy",
            [pointer("q-pending", subject="GK", concept="ANALOGY")],
            index="# Analogy\n",
        )
        self.assertNotIn("10", self.rules())

    def test_a_shared_name_is_not_a_violation(self) -> None:
        """A name both subjects declare is normal (Reasoning owns a chapter named alike)."""

        taxonomy = json.loads(json.dumps(TAXONOMY))
        taxonomy["subjects"]["MATH"]["chapters"].append(
            {"name": "Number Series", "concepts": [{"name": "Number Series"}]}
        )
        taxonomy["subjects"]["REAS"]["chapters"].append(
            {"name": "Number Series", "concepts": [{"name": "Number Series"}]}
        )
        self.write(
            "reasoning/_unclassified",
            [pointer("q-shared", subject="REAS", concept="Number Series")],
            index="# _unclassified\n",
        )
        report = audit_db.run_audit(self.root, taxonomy=taxonomy)
        self.assertNotIn("10", [issue.rule for issue in report.issues])

    def test_a_declared_chapter_with_a_shared_concept_passes(self) -> None:
        """The 'Ratio & Proportion' false positive: correct chapter, shared label."""

        taxonomy = json.loads(json.dumps(TAXONOMY))
        taxonomy["subjects"]["REAS"]["chapters"].append(
            {"name": "Mathematical Operations", "concepts": [{"name": "Ratio & Proportion"}]}
        )
        taxonomy["subjects"]["MATH"]["chapters"].append(
            {"name": "Ratio", "concepts": [{"name": "Ratio & Proportion"}]}
        )
        self.write(
            "reasoning/mathematical-operations",
            [
                pointer(
                    "q-ratio",
                    subject="REAS",
                    chapter="Mathematical Operations",
                    concept="Ratio & Proportion",
                )
            ],
        )
        report = audit_db.run_audit(self.root, taxonomy=taxonomy)
        self.assertNotIn("10", [issue.rule for issue in report.issues])


# ---------------------------------------------------------------------------
# rule 11 — the derived view is never published
# ---------------------------------------------------------------------------


class RuleElevenTests(AuditFixture):
    def test_the_shipped_exclusion_covers_the_analysis_view(self) -> None:
        self.assertClean()
        self.assertNotIn("11", self.rules())

    def test_dropping_the_exclusion_fails(self) -> None:
        """If ``gitpush`` stops excluding the view, the audit must fail."""

        with mock.patch.object(gitpush, "PUBLISH_EXCLUDE_PATHS", ()):
            self.assertIn("11", self.rules())
            self.assertTrue(
                any("not in agent.gitpush.PUBLISH_EXCLUDE_PATHS" in issue for issue in self.failures())
            )

    def test_removing_the_unstaging_step_fails(self) -> None:
        with mock.patch.object(gitpush.Publisher, "_drop_excluded_paths", None, create=True):
            delattr(gitpush.Publisher, "_drop_excluded_paths")
            try:
                self.assertIn("11", self.rules())
                self.assertTrue(
                    any("no longer has the step" in issue for issue in self.failures())
                )
            finally:
                gitpush.Publisher._drop_excluded_paths = lambda self, result: None  # type: ignore[attr-defined]

    def test_the_analysis_directory_is_a_reserved_derived_view(self) -> None:
        """The rule protects the *reserved* analysis directory, not a random path."""

        self.assertEqual(paths.rel(paths.ANALYSIS_DB_DIR), "database/english/_analysis")
        self.assertIn("_analysis", paths.ANALYSIS_DB_DIR.parts)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
