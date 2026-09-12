"""Regression tests for the database-review fixes.

Covers the five defects that were fixed after the independent database
inspection: duplicated nesting levels, cross-subject leakage in English leaves,
comma-joined multi-label concepts, the section-level signature check, and the
``tools/audit_db.py`` rules themselves.
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

from agent import build_db
from agent.classify import Classifier, SubjectIndex
from agent.config import LayoutSpan, SignaturePolicy
from agent.sections import validate_sections
from agent.taxonomy import build_alias_map
from agent.util import norm_key
from tools import audit_db


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def question(n, concept, tags=None, ordinal=None):
    return {
        "qid": f"q{ordinal or n}",
        "n": n,
        "concept": concept,
        "tags": tags or [],
        "question": "text",
        "options": [{"label": "1", "text": "a"}, {"label": "2", "text": "b"}],
    }


def record(subject, chapter, topic, concept, qid, ordinal=1, paper="p.json"):
    return {
        "subject": subject,
        "chapter": chapter,
        "topic": topic,
        "concept": concept,
        "qid": qid,
        "ordinal": ordinal,
        "paper_path": paper,
        "exam": "CGL",
        "year": 2024,
    }


def small_taxonomy(chapters, subject="ENG"):
    return {
        "subjects": {
            subject: {
                "chapters": [
                    {"id": str(i), "name": name, "key": norm_key(name), "topics": topics, "concepts": []}
                    for i, (name, topics) in enumerate(chapters, start=1)
                ]
            }
        }
    }


# ---------------------------------------------------------------------------
# F1 - tree writer
# ---------------------------------------------------------------------------


class TreeCollapseTests(unittest.TestCase):
    def test_level_equal_to_parent_is_merged(self) -> None:
        self.assertEqual(
            build_db.leaf_levels(record("ENG", "Vocabulary", "Vocabulary", "Vocabulary", "a")),
            (("vocabulary", "chapter"),),
        )
        self.assertEqual(
            build_db.leaf_levels(record("ENG", "Vocabulary", "Antonym", "Antonym", "a")),
            (("vocabulary", "chapter"), ("antonym", "topic")),
        )
        self.assertEqual(
            build_db.leaf_levels(record("ENG", None, None, None, "a")),
            (("_unclassified", "chapter"),),
        )

    def test_tree_has_no_duplicated_nesting_and_one_pair_per_leaf(self) -> None:
        records = [
            record("ENG", "Vocabulary", "Vocabulary", "Vocabulary", "a1"),
            record("ENG", "Vocabulary", "Antonym", "Antonym", "a2"),
            record("ENG", "Vocabulary", "Antonym", "Synonym", "a3"),
            record("ENG", None, None, None, "a4"),
            record("COMPUTER", "Computer Fundamentals", None, "Computer Fundamentals", "a5"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            summary = build_db.write_question_tree(records, database_dir=Path(tmp))
            base = Path(tmp)
            dirs = [p for p in base.rglob("*") if p.is_dir()]
            for directory in dirs:
                if directory.parent != base:
                    self.assertNotEqual(
                        directory.name,
                        directory.parent.name,
                        f"duplicated nesting level in {directory}",
                    )
            # english/vocabulary holds records *and* children -> one index + one jsonl
            vocab = base / "english" / "vocabulary"
            self.assertTrue((vocab / "index.md").is_file())
            self.assertTrue((vocab / "questions.jsonl").is_file())
            self.assertEqual(len(list(vocab.glob("questions.jsonl"))), 1)
            # every leaf concept owns exactly one index.md + one questions.jsonl
            for jsonl in base.rglob("questions.jsonl"):
                self.assertTrue((jsonl.parent / "index.md").is_file())
                self.assertEqual(len(list(jsonl.parent.glob("index.md"))), 1)
            self.assertEqual(summary["collapsed_levels"], 5)
            self.assertEqual(summary["leaves"], 5)

    def test_previous_revision_is_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            stale = base / "english" / "grammar" / "grammar"
            stale.mkdir(parents=True)
            (stale / "index.md").write_text("old", encoding="utf-8")
            (base / "english" / "_analysis").mkdir(parents=True)
            (base / "english" / "_analysis" / "keep.json").write_text("{}", encoding="utf-8")
            build_db.write_question_tree(
                [record("ENG", "Grammar", "Grammar", "Grammar", "a1")], database_dir=base
            )
            self.assertFalse(stale.exists())
            self.assertTrue((base / "english" / "_analysis" / "keep.json").is_file())


# ---------------------------------------------------------------------------
# F2 - cross-subject guard
# ---------------------------------------------------------------------------


class CrossSubjectGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        taxonomy = {
            "subjects": {
                "MATH": {
                    "chapters": [
                        {
                            "id": "1",
                            "name": "Time & Work",
                            "topics": [{"name": "Pipes & Cisterns", "concepts": []}],
                            "concepts": [],
                        }
                    ]
                },
                "ENG": {
                    "chapters": [
                        {"id": "1", "name": "At, On, and In as Prepositions of Time", "topics": [], "concepts": []},
                        {"id": "2", "name": "Verbal Ability", "topics": [{"name": "English", "concepts": []}], "concepts": []},
                    ]
                },
            }
        }
        alias_map = {
            "map": {
                norm_key("Time & Work"): {
                    "canonical": "Time & Work", "subject": "MATH", "chapter": "Time & Work",
                    "topic": None, "source": "curated",
                },
                norm_key("English"): {
                    "canonical": "English", "subject": "ENG", "chapter": "Verbal Ability",
                    "topic": "English", "source": "taxonomy",
                },
            }
        }
        self.classifier = Classifier(taxonomy, alias_map)

    def test_foreign_vocabulary_detection(self) -> None:
        self.assertTrue(self.classifier.foreign_vocabulary("ENG", "Time & Work"))
        self.assertFalse(self.classifier.foreign_vocabulary("MATH", "Time & Work"))
        # a name the subject owns is never foreign, even when another subject has it too
        self.assertFalse(self.classifier.foreign_vocabulary("ENG", "English"))
        self.assertFalse(self.classifier.foreign_vocabulary("ENG", "Some Niche Label"))

    def test_maths_label_is_not_filed_under_an_english_rule(self) -> None:
        result = self.classifier.classify("Time & Work", [], "ENG")
        self.assertIsNone(result.chapter)
        self.assertIsNone(result.concept)
        self.assertFalse(result.placed)

    def test_own_vocabulary_is_kept(self) -> None:
        result = self.classifier.classify("English", [], "ENG")
        self.assertEqual(result.chapter, "Verbal Ability")
        self.assertEqual(result.concept, "English")

    def test_fuzzy_matching_can_be_disabled(self) -> None:
        # "time and work" shares {time, and} with the rule title -> >=60% overlap
        index = SubjectIndex(
            "ENG",
            {"chapters": [{"name": "At, On, and In as Prepositions of Time", "topics": []}]},
        )
        self.assertEqual(index.match_chapter("Time and Work"), "At, On, and In as Prepositions of Time")
        self.assertIsNone(index.match_chapter("Time and Work", allow_fuzzy=False))

    def test_unsluggable_concept_is_dropped(self) -> None:
        result = self.classifier.classify("बिजनेस स्टडीज", [], "ENG")
        self.assertIsNone(result.concept)


# ---------------------------------------------------------------------------
# F3 - multi-label resolution
# ---------------------------------------------------------------------------


class MultiLabelAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.taxonomy = {
            "subjects": {
                "GK": {
                    "chapters": [
                        {
                            "id": "1",
                            "name": "GENERAL SCIENCE — CHEMISTRY",
                            "topics": [{"name": "Carbon and Organic Chemistry", "concepts": []}],
                            "concepts": [
                                {"name": "Acids, Bases & Salts", "key": "acids bases and salts", "tag": ""},
                                {"name": "Alcohols", "key": "alcohols", "tag": ""},
                            ],
                        }
                    ]
                },
                "MATH": {
                    "chapters": [
                        {
                            "id": "1",
                            "name": "Geometry",
                            "topics": [
                                {
                                    "name": "Circle",
                                    "concepts": [{"name": "Circles", "key": "circles", "tag": ""}],
                                }
                            ],
                            "concepts": [],
                        }
                    ]
                },
            }
        }
        self.labels = [
            "Acids, Bases and Salts",          # comma-bearing taxonomy name -> whole
            "Alcohols, Phenols And Ethers",    # whole fails, first component resolves
            "Circles, Chords and Tangents",    # whole fails, first component resolves
            "Foo, Bar",                        # nothing resolves -> no synthetic concept
            "Alcohols",                        # plain label
        ]
        self.alias = build_alias_map(self.taxonomy, self.labels)

    def test_whole_string_wins(self) -> None:
        entry = self.alias["map"][norm_key("Acids, Bases and Salts")]
        self.assertEqual(entry["multi"], "whole")
        self.assertEqual(entry["canonical"], "Acids, Bases & Salts")
        self.assertEqual(entry["chapter"], "GENERAL SCIENCE — CHEMISTRY")

    def test_split_uses_first_resolvable_component(self) -> None:
        entry = self.alias["map"][norm_key("Circles, Chords and Tangents")]
        self.assertEqual(entry["multi"], "split")
        self.assertEqual(entry["multi_component"], "Circles")
        self.assertEqual(entry["canonical"], "Circles")
        self.assertEqual(entry["topic"], "Circle")

    def test_unresolved_multi_label_never_becomes_a_concept(self) -> None:
        entry = self.alias["map"][norm_key("Foo, Bar")]
        self.assertIsNone(entry["canonical"])
        self.assertEqual(entry["multi"], "unresolved")

    def test_counters(self) -> None:
        stats = self.alias["stats"]
        self.assertEqual(stats["multi_whole"], 1)
        self.assertEqual(stats["multi_split"], 2)
        self.assertEqual(stats["multi_unresolved"], 1)

    def test_plain_labels_are_unchanged(self) -> None:
        entry = self.alias["map"][norm_key("Alcohols")]
        self.assertNotIn("multi", entry)
        self.assertEqual(entry["canonical"], "Alcohols")
        self.assertEqual(entry["chapter"], "GENERAL SCIENCE — CHEMISTRY")


# ---------------------------------------------------------------------------
# F4 - section-level signature
# ---------------------------------------------------------------------------


class SectionSignatureTests(unittest.TestCase):
    def _paper(self, concepts, per_section=25):
        return [question((i % per_section) + 1, concept) for i, concept in enumerate(concepts)]

    def test_modal_subject_per_section_is_the_primary_check(self) -> None:
        # one noisy label ("Profit & Loss" in the English section) must not flip it
        concepts = ["Analogy"] * 25 + ["Polity"] * 25 + ["Percentage"] * 25
        concepts += ["Grammar"] * 24 + ["Profit & Loss"]
        spans = [LayoutSpan(s, i * 25 + 1, (i + 1) * 25) for i, s in enumerate(["REAS", "GK", "MATH", "ENG"])]
        agreement, comparable, hits, total, mismatches = validate_sections(
            concepts and self._paper(concepts), spans, list(range(100)), 0.95
        )
        self.assertEqual((comparable, hits, total), (4, 4, 4))
        self.assertEqual(agreement, 1.0)
        self.assertEqual(mismatches, [])

    def test_reordered_sections_fail_the_section_check(self) -> None:
        concepts = ["Grammar"] * 25 + ["Analogy"] * 25 + ["Percentage"] * 25 + ["Polity"] * 25
        spans = [LayoutSpan(s, i * 25 + 1, (i + 1) * 25) for i, s in enumerate(["REAS", "GK", "MATH", "ENG"])]
        agreement, comparable, hits, total, mismatches = validate_sections(
            self._paper(concepts), spans, list(range(100)), 0.95
        )
        self.assertEqual(comparable, 4)
        self.assertEqual(hits, 1)  # only the MATH section keeps its declared subject
        self.assertEqual(agreement, 0.25)
        self.assertFalse(agreement >= 0.95)
        self.assertEqual(len(mismatches), 3)  # capped samples

    def test_without_labels_there_is_no_section_evidence(self) -> None:
        questions = [question(i + 1, "Unidentified") for i in range(50)]
        spans = [LayoutSpan("MATH", 1, 50)]
        agreement, comparable, _hits, total, _mismatches = validate_sections(
            questions, spans, list(range(50)), 0.95
        )
        self.assertIsNone(agreement)
        self.assertEqual(comparable, 0)
        self.assertEqual(total, 1)

    def test_policy_default_exposes_the_threshold(self) -> None:
        policy = SignaturePolicy()
        self.assertEqual(policy.min_agreement, 0.95)
        self.assertEqual(policy.min_section_agreement, 0.95)


# ---------------------------------------------------------------------------
# F5 - audit rules
# ---------------------------------------------------------------------------


def touch(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_leaf(directory: Path, records) -> None:
    touch(directory / "index.md", "# leaf\n")
    (directory / "questions.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n", encoding="utf-8"
    )


TAXONOMY = {
    "subjects": {
        # `Synonym` must be declared by the English chapter that holds it,
        # otherwise audit rule 6 (declared concept scope) rejects the leaf
        "ENG": {
            "chapters": [
                {
                    "id": "1",
                    "name": "Grammar",
                    "topics": [{"name": "Synonym", "concepts": []}],
                    "concepts": [],
                }
            ]
        },
        "MATH": {"chapters": [{"id": "1", "name": "Time & Work", "topics": [], "concepts": []}]},
    }
}


class AuditRuleTests(unittest.TestCase):
    def _report(self, base: Path):
        return audit_db.run_audit(base, taxonomy=TAXONOMY)

    def test_clean_tree_passes_every_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_leaf(
                base / "english" / "grammar" / "synonym",
                [{"qid": "q1", "subject": "ENG", "concept": "Synonym", "chapter": "Grammar",
                  "paper_path": "p.json", "ordinal": 1}],
            )
            write_leaf(
                base / "english" / "_unclassified",
                [{"qid": "q2", "subject": "ENG", "concept": None, "chapter": None,
                  "paper_path": "p.json", "ordinal": 2}],
            )
            report = self._report(base)
            self.assertEqual([str(i) for i in report.issues], [])
            self.assertTrue(report.ok)
            self.assertEqual(report.records, 2)

    def test_duplicated_nesting_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_leaf(base / "english" / "grammar" / "grammar", [{"qid": "q1", "subject": "ENG", "concept": "Grammar"}])
            report = self._report(base)
            self.assertIn("1", {issue.rule for issue in report.issues})

    def test_stray_chapter_index_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_leaf(base / "english" / "grammar" / "phonetics", [{"qid": "q1", "subject": "ENG", "concept": "X"}])
            touch(base / "english" / "grammar" / "index.md", "# chapter level\n")
            touch(base / "english" / "grammar" / "grammar" / "index.md", "# dup\n")
            report = self._report(base)
            rules = {issue.rule for issue in report.issues}
            self.assertIn("2", rules)

    def test_english_foreign_vocabulary_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_leaf(
                base / "english" / "grammar" / "time-and-work",
                [{"qid": "q1", "subject": "ENG", "concept": "Time & Work", "chapter": "Grammar"}],
            )
            report = self._report(base)
            hits = [issue for issue in report.issues if issue.rule == "3"]
            self.assertEqual(len(hits), 1)
            self.assertIn("time-and-work", hits[0].where)

    def test_placeholder_slug_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_leaf(base / "gk" / "_unclassified" / "unnamed", [{"qid": "q1", "subject": "GK", "concept": "x"}])
            report = self._report(base)
            self.assertIn("4", {issue.rule for issue in report.issues})

    def test_missing_record_identity_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_leaf(base / "gk" / "_unclassified", [{"qid": "", "subject": "", "concept": None}])
            report = self._report(base)
            details = [issue.detail for issue in report.issues if issue.rule == "4"]
            self.assertTrue(any("qid" in detail for detail in details))
            self.assertTrue(any("subject" in detail for detail in details))

    def test_question_in_two_leaves_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            row = {"qid": "q1", "subject": "ENG", "concept": "Grammar", "paper_path": "p.json", "ordinal": 7}
            write_leaf(base / "english" / "grammar" / "a", [row])
            write_leaf(base / "english" / "grammar" / "b", [dict(row)])
            report = self._report(base)
            hits = [issue for issue in report.issues if issue.rule == "5"]
            self.assertEqual(len(hits), 1)

    def test_reused_qid_for_different_questions_is_a_warning_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_leaf(
                base / "reasoning" / "a",
                [{"qid": "shared", "subject": "REAS", "concept": "A", "paper_path": "p1.json", "ordinal": 1}],
            )
            write_leaf(
                base / "reasoning" / "b",
                [{"qid": "shared", "subject": "REAS", "concept": "B", "paper_path": "p2.json", "ordinal": 9}],
            )
            report = self._report(base)
            self.assertEqual([issue for issue in report.issues if issue.rule == "5"], [])
            self.assertEqual(len(report.warnings), 1)
            self.assertTrue(report.ok)

    def test_empty_database_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self._report(Path(tmp))
            self.assertFalse(report.ok)
            self.assertEqual(report.issues[0].rule, "0")


if __name__ == "__main__":
    unittest.main()
