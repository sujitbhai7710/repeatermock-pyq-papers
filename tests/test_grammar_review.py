"""The grammar review pass and the confidence floor (LESSONS L28).

The review pass audits every question that already *has* a rule assignment:
the models return ``{confirmed, correct_rule, confidence, why}``, and a verdict
is honoured only above the placement confidence floor (``>= 0.8``, or
``>= 0.75`` when two distinct models agreed — ``same_model_fallback`` false).
Below the floor a question goes to ``_unclassified``: a 0.05-confidence verdict
must never file a question, whether it is a fresh assignment or a review veto.
"""


from __future__ import annotations

# The suite must never write the repository's own ``state/`` (LESSONS.md L26):
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

from agent import grammar
from agent.grammar import (
    AI_CONFIDENCE_FLOOR,
    GrammarQuestion,
    REASON_AI_LOW_CONFIDENCE,
    REASON_AI_REVIEWED,
    RuleMatch,
    confidence_accepted,
    parse_ai_reply,
    review_sample,
)


def question(qid: str, *, rule: int = 0) -> GrammarQuestion:
    """One grammar question; ``rule`` fills a keyword-matcher verdict when > 0."""

    match = (
        RuleMatch(
            rule=rule,
            title=f"Rule {rule} title",
            score=10,
            matched_terms=[],
            hint=False,
            breakdown={"source": "keyword", "strong_score": 3},
        )
        if rule
        else RuleMatch(rule=None, title="", score=0, matched_terms=[], hint=False)
    )
    return GrammarQuestion(
        qid=qid,
        exam="CGL",
        year=2024,
        paper_path="p.json",
        ordinal=1,
        concept_raw="Grammar",
        qtype=None,
        prompt="The lion and the unicorn fought between the crown.",
        options=[],
        subject="ENG",
        concept="Grammar",
        text=qid,
        match=match,
    )


def taxonomy():
    """Rules 17 and 52 only — enough for the review verdicts below."""

    return {
        "subjects": {
            "ENG": {
                "kind": "english_grammar",
                "chapters": [
                    {
                        "id": "17",
                        "name": "Active and Passive Voice",
                        "key": "active and passive voice",
                        "topics": [],
                        "concepts": [],
                        "kind": "rule",
                        "topic": "Voice",
                    },
                    {
                        "id": "52",
                        "name": "Articles with Joined Nouns",
                        "key": "articles with joined nouns",
                        "topics": [],
                        "concepts": [],
                        "kind": "rule",
                        "topic": "Subject-Verb Agreement",
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


class ConfidenceFloorTests(unittest.TestCase):
    def test_floor_accepts_0_8_and_above(self) -> None:
        self.assertTrue(confidence_accepted(0.8, same_model_fallback=True))
        self.assertTrue(confidence_accepted(0.99, same_model_fallback=True))
        self.assertTrue(confidence_accepted(AI_CONFIDENCE_FLOOR, same_model_fallback=False))

    def test_two_model_agreement_carve_out(self) -> None:
        # 0.75 needs two distinct models; one model speaking for both roles fails
        self.assertTrue(confidence_accepted(0.75, same_model_fallback=False))
        self.assertFalse(confidence_accepted(0.75, same_model_fallback=True))
        self.assertFalse(confidence_accepted(0.749, same_model_fallback=False))

    def test_low_confidence_is_never_accepted(self) -> None:
        for value in (0.05, 0.4, 0.7):
            self.assertFalse(confidence_accepted(value, same_model_fallback=False))
            self.assertFalse(confidence_accepted(value, same_model_fallback=True))
        self.assertFalse(confidence_accepted("not-a-number", same_model_fallback=False))
        self.assertFalse(confidence_accepted(None, same_model_fallback=False))


class ParseReviewReplyTests(unittest.TestCase):
    def test_review_contract_confirmed_and_correct_rule(self) -> None:
        items = [question("q1", rule=17), question("q2", rule=17)]
        final = {
            "items": [
                {"qid": "q1", "confirmed": True, "correct_rule": None, "confidence": 0.95, "why": "voice transform"},
                {"qid": "q2", "confirmed": False, "correct_rule": 52, "confidence": 0.97, "why": "joined nouns"},
            ]
        }
        out = parse_ai_reply(final, items, rules_count=129)
        self.assertTrue(out["q1"]["keep"])
        self.assertFalse(out["q1"]["veto"])
        self.assertFalse(out["q2"]["keep"])
        self.assertTrue(out["q2"]["veto"])
        self.assertEqual(out["q2"]["rule"], 52)
        self.assertEqual(out["q2"]["reason"], "joined nouns")

    def test_legacy_keep_rule_shape_still_parses(self) -> None:
        items = [question("q1", rule=17)]
        final = {"items": [{"qid": "q1", "keep": False, "rule": 52, "confidence": 0.9, "reason": "old shape"}]}
        out = parse_ai_reply(final, items, rules_count=129)
        self.assertTrue(out["q1"]["veto"])
        self.assertEqual(out["q1"]["rule"], 52)

    def test_assignment_rows_are_unchanged_by_the_parser(self) -> None:
        items = [question("q1")]
        final = {"items": [{"qid": "q1", "rule": 17, "confidence": 0.84, "reason": "modal"}]}
        out = parse_ai_reply(final, items, rules_count=129)
        self.assertEqual(out["q1"]["rule"], 17)
        self.assertNotIn("keep", out["q1"])
        self.assertEqual(out["q1"]["reason_code"], "")

    def test_invalid_rule_numbers_are_rejected(self) -> None:
        items = [question("q1")]
        final = {"items": [{"qid": "q1", "rule": 200, "confidence": 0.9, "reason": "x"}]}
        out = parse_ai_reply(final, items, rules_count=129)
        self.assertIsNone(out["q1"]["rule"])
        self.assertEqual(out["q1"]["reason_code"], grammar.REASON_AI_INVALID)


class ReviewSampleTests(unittest.TestCase):
    def test_sample_covers_keyword_and_ai_placed_questions(self) -> None:
        rules = grammar.build_rule_index(taxonomy())
        items = [
            question("q-kw", rule=17),   # keyword matcher
            question("q-ai"),            # stored AI decision below
            question("q-low"),           # stored decision below the floor: not placed
            question("q-none"),          # nothing places it
        ]
        decisions = {
            "q-ai": {"rule": 52, "confidence": 0.9, "same_model_fallback": False},
            "q-low": {"rule": 17, "confidence": 0.05, "same_model_fallback": False},
        }
        sample = review_sample(items, rules, decisions)
        self.assertEqual({item.qid for item in sample}, {"q-kw", "q-ai"})
        # the AI-assigned item carries its effective rule so the review payload
        # can tell the model what it is filed under
        by_qid = {item.qid: item for item in sample}
        self.assertEqual(by_qid["q-ai"].match.rule, 52)
        self.assertEqual(by_qid["q-ai"].match.breakdown["source"], "ai")

    def test_per_rule_cap_is_evenly_spaced_and_deterministic(self) -> None:
        rules = grammar.build_rule_index(taxonomy())
        items = [question(f"q{i}", rule=17) for i in range(10)]
        first = review_sample(items, rules, {}, per_rule=4)
        second = review_sample(items, rules, {}, per_rule=4)
        self.assertEqual([item.qid for item in first], [item.qid for item in second])
        self.assertEqual(len(first), 4)
        self.assertTrue({item.qid for item in first} <= {item.qid for item in items})


class LowConfidenceRejectedTests(unittest.TestCase):
    """Acceptance: a low-confidence verdict lands in ``_unclassified``."""

    def _build(self, base: Path, assignments, *, prompt: str):
        records = [{"subject": "ENG", "qid": qid, "ordinal": 1, "paper_path": "p.json",
                    "exam": "CGL", "year": 2024, "concept": "Grammar", "concept_raw": "Grammar",
                    "chapter": "Grammar", "topic": "Grammar", "has_image": False} for qid in assignments]
        questions = {qid: {"question": prompt, "options": []} for qid in assignments}
        return grammar.build_grammar_db(
            records,
            questions,
            taxonomy(),
            rule_texts=[],
            assignments=assignments,
            out_dir=base / "english" / "_analysis" / "grammar",
            rules_dir=base / "english" / "grammar",
            chapter_index=base / "english" / "grammar" / "index.md",
            database_dir=base,
        )

    def _unassigned(self, base: Path):
        path = base / "english" / "_analysis" / "grammar" / "unassigned.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_low_confidence_assignment_is_rejected_to_unassigned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result = self._build(
                base,
                {"q-low": {"rule": 17, "confidence": 0.05, "reason": "a guess"}},
                prompt="Choose the correct modal verb for the blank.",
            )
            self.assertEqual(result.counters["grammar_questions_matched"], 0)
            rows = self._unassigned(base)
            self.assertEqual([row["qid"] for row in rows], ["q-low"])
            self.assertEqual(rows[0]["reason"], REASON_AI_LOW_CONFIDENCE)

    def test_low_confidence_veto_replacement_is_not_honoured(self) -> None:
        # a review veto with a suggested rule below the floor unclassifies
        # instead of moving into a second guess
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            assignments = {
                "q-kw": {
                    "rule": 17,
                    "confidence": 0.6,
                    "reason": "not sure",
                    "keep": False,
                    "veto": True,
                    "same_model_fallback": False,
                }
            }
            result = self._build(
                base,
                assignments,
                prompt="Change into passive voice: The boy kicked the ball.",
            )
            self.assertEqual(result.counters["grammar_questions_matched"], 0)
            rows = self._unassigned(base)
            self.assertEqual([row["qid"] for row in rows], ["q-kw"])
            self.assertEqual(rows[0]["reason"], REASON_AI_REVIEWED)
            self.assertEqual(rows[0]["reviewed_from"], 17)

    def test_confident_veto_moves_to_the_reviewers_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            assignments = {
                "q-kw": {
                    "rule": 52,
                    "confidence": 0.97,
                    "reason": "repeated article, joined nouns",
                    "keep": False,
                    "veto": True,
                    "same_model_fallback": False,
                }
            }
            result = self._build(
                base,
                assignments,
                prompt="Change into passive voice: The boy kicked the ball.",
            )
            self.assertEqual(result.counters["grammar_questions_matched"], 1)
            pack = base / "english" / "grammar" / "52-articles-with-joined-nouns" / "questions.jsonl"
            rows = [json.loads(line) for line in pack.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["qid"] for row in rows], ["q-kw"])
            self.assertEqual(rows[0]["assigned_by"], "ai_review")
            self.assertEqual(rows[0]["match_breakdown"]["moved_from"], 17)
            # the audit still passes: one home per question, no empty-leaf lies
            from tools import audit_db

            report = audit_db.run_audit(base, taxonomy=taxonomy())
            self.assertTrue(report.ok, [str(issue) for issue in report.issues])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
