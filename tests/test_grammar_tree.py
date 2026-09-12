"""The grammar rule leaves own their questions — the tree never files them twice.

``agent.grammar`` partitions the English grammar questions over the 129 rules and
writes one browsable leaf per rule (``database/english/grammar/<NN>-<slug>/``);
``agent.build_db`` writes the concept tree.  A question the grammar pass assigned
must live in exactly one leaf — its rule leaf (audit rule 5) — so the English tree
writer skips the qids the grammar output owns, and a leaf whose questions are all
owned disappears.  Unassigned grammar questions stay where they are.
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

from agent import build_db, grammar
from tools import audit_db


def eng_record(qid, *, ordinal, concept="Grammar", chapter="Grammar"):
    """One English index record, shaped like ``agent.indexer`` emits them."""

    return {
        "subject": "ENG",
        "chapter": chapter,
        "topic": concept,
        "concept": concept,
        "concept_raw": concept,
        "leaf_bucket": None,
        "qid": qid,
        "ordinal": ordinal,
        "paper_path": "p.json",
        "exam": "CGL",
        "year": 2024,
        "has_image": False,
    }


def grammar_taxonomy():
    """One rule chapter plus the supplementary ``Grammar`` chapter."""

    return {
        "subjects": {
            "ENG": {
                "kind": "english_grammar",
                "chapters": [
                    {
                        "id": "17",
                        "name": "Active and Passive Voice",
                        "key": "active and passive voice",
                        "topics": [{"name": "Voice", "key": "voice", "concepts": []}],
                        "concepts": [],
                        "kind": "rule",
                        "topic": "Voice",
                    },
                    {
                        "id": "S1",
                        "name": "Grammar",
                        "key": "grammar",
                        "topics": [],
                        "concepts": [],
                        "kind": "supplementary",
                    },
                ],
            }
        }
    }


def pack_qids(path: Path):
    if not path.is_file():
        return []
    return [json.loads(line)["qid"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class GrammarTreeExclusionTests(unittest.TestCase):
    """Audit rule 5, from the grammar tree's side."""

    def test_the_tree_writer_skips_the_qids_the_grammar_output_owns(self) -> None:
        records = [
            eng_record("q-owned", ordinal=1, concept="Error Detection"),
            eng_record("q-free", ordinal=2),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            summary = build_db.write_question_tree(
                records, database_dir=base, exclude_qids={"q-owned"}
            )
            self.assertEqual(summary["excluded"], 1)
            # the question kept its rule leaf home, the emptied leaf is gone
            self.assertFalse((base / "english" / "grammar" / "error-detection").exists())
            self.assertEqual(pack_qids(base / "english" / "grammar" / "questions.jsonl"), ["q-free"])

    def test_assigned_qids_are_read_from_the_grammar_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "rules.json").write_text(
                json.dumps({"rules": [{"questionIds": ["a", "b"]}, {"questionIds": ["c"]}]}),
                encoding="utf-8",
            )
            self.assertEqual(grammar.assigned_qids(out), {"a", "b", "c"})
            (out / "rules.json").unlink()
            (out / "questions.jsonl").write_text(
                '{"qid": "a", "rule": 17}\n{"qid": "b", "rule": null}\n{"qid": "c"}\n',
                encoding="utf-8",
            )
            self.assertEqual(grammar.assigned_qids(out), {"a"})

    def test_the_grammar_pass_leaves_one_home_per_question_and_the_audit_passes(self) -> None:
        records = [eng_record("q-owned", ordinal=1), eng_record("q-free", ordinal=2)]
        questions = {
            # grammar-shaped (the modal phrasing) but no keyword evidence for any
            # rule, so the stored decision below is what assigns it; the second
            # prompt matches no grammar pattern and no rule claims it
            "q-owned": {"question": "Choose the correct modal verb for the blank.", "options": []},
            "q-free": {"question": "Choose the word most similar in meaning.", "options": []},
        }
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result = grammar.build_grammar_db(
                records,
                questions,
                grammar_taxonomy(),
                # the rule markdown is not needed: the verdict comes from the
                # stored AI decision, which is how a rerun applies it too
                rule_texts=[],
                assignments={"q-owned": {"rule": 17, "confidence": 0.9, "reason": "test"}},
                out_dir=base / "english" / "_analysis" / "grammar",
                rules_dir=base / "english" / "grammar",
                chapter_index=base / "english" / "grammar" / "index.md",
                database_dir=base,
            )
            self.assertEqual(result.counters["tree_excluded"], 1)
            self.assertEqual(pack_qids(base / "english" / "grammar" / "questions.jsonl"), ["q-free"])
            rule_pack = base / "english" / "grammar" / "17-active-and-passive-voice" / "questions.jsonl"
            rows = [json.loads(line) for line in rule_pack.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual([row["qid"] for row in rows], ["q-owned"])
            self.assertEqual(rows[0]["assigned_by"], "ai")  # the stored decision
            # a rule leaf is a question-tree leaf: subject + concept on the record
            self.assertEqual(rows[0]["subject"], "ENG")
            self.assertTrue(rows[0]["concept"])
            # pass the taxonomy explicitly: the audit reads state/taxonomy.json,
            # which a standalone run of this test (no other test has populated
            # the temp state dir) would not have
            report = audit_db.run_audit(base, taxonomy=grammar_taxonomy())
            self.assertTrue(report.ok, [str(issue) for issue in report.issues])
            self.assertEqual([issue for issue in report.issues if issue.rule == "5"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
