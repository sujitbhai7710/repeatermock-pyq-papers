"""Tests for the pure functions: vocab buckets, section map, Hindi detection, aliases."""

from __future__ import annotations

import unittest

from agent import keywords, sections, vocab
from agent.classify import Classifier, MATH_FALLBACK, SubjectIndex
from agent.config import LayoutSpan
from agent.taxonomy import ConceptResolver
from agent.util import norm_key


# ---------------------------------------------------------------------------
# Hindi detection
# ---------------------------------------------------------------------------


class HindiDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = sections.make_hindi_policy(
            type("Cfg", (), {"regex": r"[\u0900-\u097F]", "scope": ("question", "options"), "include_solution": False, "exclude_punctuation": False})()
        )

    def test_devanagari_prompt_is_hindi(self) -> None:
        self.assertTrue(self.policy.is_hindi({"question": "निम्नलिखित में से कौन सा विकल्प सही है?"}))

    def test_devanagari_option_is_hindi(self) -> None:
        self.assertTrue(
            self.policy.is_hindi(
                {
                    "question": "Choose the correct option",
                    "options": [{"label": "1", "text": "सही उत्तर"}, {"label": "2", "text": "wrong"}],
                }
            )
        )

    def test_solution_is_excluded_by_default(self) -> None:
        self.assertFalse(
            self.policy.is_hindi({"question": "ABCD is a trapezium", "solution": "PQ।।SR so the answer is 12"})
        )

    def test_danda_follows_the_configured_policy(self) -> None:
        # The specified range [\u0900-\u097F] includes the danda `।` (U+0964),
        # which this corpus uses as a "parallel" symbol in geometry.  The default
        # policy is spec-literal (danda counts as Devanagari); `exclude_punctuation`
        # documents the narrower alternative.
        question = {"question": "In the figure PT।।SR and QT = PQ"}
        self.assertTrue(self.policy.is_hindi(question))
        strict = sections.make_hindi_policy(
            type(
                "Cfg",
                (),
                {
                    "regex": r"[\u0900-\u097F]",
                    "scope": ("question", "options"),
                    "include_solution": False,
                    "exclude_punctuation": True,
                },
            )()
        )
        self.assertFalse(strict.is_hindi(question))

    def test_hindi_regex_from_keywords_is_still_available(self) -> None:
        self.assertTrue(keywords.subject_for_text("पर्यायवाची शब्द") == "HINDI")


# ---------------------------------------------------------------------------
# section map / layout
# ---------------------------------------------------------------------------


def q(n, concept=None, tags=None, text="", solution="", options=None):
    return {
        "qid": f"q{n}",
        "n": n,
        "concept": concept,
        "tags": tags or [],
        "question": text,
        "solution": solution,
        "options": options or [{"label": "1", "text": "a"}, {"label": "2", "text": "b"}, {"label": "3", "text": "c"}, {"label": "4", "text": "d"}],
    }


class SectionMapTests(unittest.TestCase):
    def test_split_sections_by_n_reset(self) -> None:
        questions = [q(1), q(2), q(3), q(1), q(2), q(1)]
        self.assertEqual([len(s) for s in sections.split_sections(questions)], [3, 2, 1])

    def test_ordinal_is_index_plus_one(self) -> None:
        self.assertEqual(sections.ordinal_of(0), 1)
        self.assertEqual(sections.ordinal_of(24), 25)

    def test_subject_at(self) -> None:
        spans = [LayoutSpan("REAS", 1, 25), LayoutSpan("GK", 26, 50)]
        self.assertEqual(sections.subject_at(spans, 1), "REAS")
        self.assertEqual(sections.subject_at(spans, 25), "REAS")
        self.assertEqual(sections.subject_at(spans, 26), "GK")
        self.assertIsNone(sections.subject_at(spans, 51))

    def test_config_layouts_resolve(self) -> None:
        from agent.config import exams

        table = exams()
        _, cgl_t1 = table.kind_for_path("SSC-CGL/Previous_Year_Paper_Tier_I/2024/x.json")
        self.assertIsNotNone(cgl_t1)
        self.assertEqual(cgl_t1.id, "tier1")
        spans = cgl_t1.spans_for(100, 2024)
        self.assertEqual([s.subject for s in spans], ["REAS", "GK", "MATH", "ENG"])

    def test_mts_layout_is_year_aware(self) -> None:
        from agent.config import exams

        table = exams()
        _, kind = table.kind_for_path("SSC-MTS/Previous_Year_Paper/2024/x.json")
        self.assertIsNotNone(kind)
        spans_2024 = kind.spans_for(90, 2024)
        self.assertEqual([s.subject for s in spans_2024], ["MATH", "REAS", "GK", "ENG"])
        self.assertEqual([(s.start, s.end) for s in spans_2024], [(1, 20), (21, 40), (41, 65), (66, 90)])
        spans_2019 = kind.spans_for(100, 2019)
        self.assertEqual([s.subject for s in spans_2019], ["ENG", "REAS", "MATH", "GK"])

    def test_cgl_tier2_section_sizes(self) -> None:
        from agent.config import exams

        table = exams()
        _, kind = table.kind_for_path(
            "SSC-CGL/Previous_Year_Paper_Tier_II/2022_-_2023/x.json"
        )
        spans = kind.spans_for(150, 2022)
        self.assertEqual(
            [(s.subject, s.start, s.end) for s in spans],
            [("MATH", 1, 30), ("REAS", 31, 60), ("ENG", 61, 105), ("GK", 106, 130), ("COMPUTER", 131, 150)],
        )

    def test_gd_hindi_section_detected(self) -> None:
        from agent.config import exams

        table = exams()
        _, kind = table.kind_for_path("SSC-GD/Previous_Year_Papers/2024/x.json")
        spans = kind.spans_for(100, 2024)
        self.assertEqual([s.subject for s in spans][-1], "HINDI")
        self.assertEqual((spans[-1].start, spans[-1].end), (81, 100))


class SignatureValidationTests(unittest.TestCase):
    def _questions(self, subjects, per_section=25):
        """Build a paper whose labels match the given desired subjects."""

        label = {
            "REAS": ("Analogy", ["Analogy"]),
            "GK": ("Polity", ["Polity"]),
            "MATH": ("Percentage", ["Percentage"]),
            "ENG": ("Grammar", ["Grammar"]),
            "COMPUTER": ("Operating Systems", ["Operating Systems"]),
        }
        out = []
        index = 0
        for subject in subjects:
            for _ in range(per_section):
                index += 1
                concept, tags = label[subject]
                out.append(q((index - 1) % per_section + 1, concept, tags))
        return out

    def test_agreement_one_when_labels_match_layout(self) -> None:
        questions = self._questions(["REAS", "GK", "MATH", "ENG"])
        spans = [LayoutSpan(s, i * 25 + 1, (i + 1) * 25) for i, s in enumerate(["REAS", "GK", "MATH", "ENG"])]
        positional = sections.spans_to_map(spans, len(questions))
        result = sections.validate(positional, list(range(len(questions))), questions, 0.95)
        self.assertAlmostEqual(result.agreement, 1.0)
        self.assertTrue(result.ok)

    def test_mismatched_layout_is_flagged(self) -> None:
        questions = self._questions(["ENG", "REAS", "MATH", "GK"])
        spans = [LayoutSpan(s, i * 25 + 1, (i + 1) * 25) for i, s in enumerate(["REAS", "GK", "MATH", "ENG"])]
        positional = sections.spans_to_map(spans, len(questions))
        result = sections.validate(positional, list(range(len(questions))), questions, 0.95)
        self.assertLess(result.agreement, 0.95)
        self.assertTrue(result.needs_ai_review)

    def test_ambiguous_labels_are_not_comparable(self) -> None:
        questions = [q(1, "Unidentified", []) for _ in range(10)]
        spans = [LayoutSpan("ENG", 1, 10)]
        positional = sections.spans_to_map(spans, 10)
        result = sections.validate(positional, list(range(10)), questions, 0.95)
        self.assertEqual(result.comparable, 0)
        self.assertEqual(result.agreement, 0.0)

    def test_detect_layout_recovers_real_order(self) -> None:
        questions = self._questions(["MATH", "REAS", "GK", "ENG"], per_section=10)
        detected = sections.detect_layout(questions)
        self.assertTrue(detected.ok)
        self.assertEqual([s.subject for s in detected.spans], ["MATH", "REAS", "GK", "ENG"])
        self.assertEqual([(s.start, s.end) for s in detected.spans], [(1, 10), (11, 20), (21, 30), (31, 40)])

    def test_detect_layout_merges_split_sections(self) -> None:
        questions = self._questions(["MATH", "REAS"], per_section=10)
        # force a stray n reset inside the reasoning block
        for i in range(10, 20):
            questions[i]["n"] = i - 9 if i >= 15 else i - 9
        detected = sections.detect_layout(questions)
        self.assertEqual([s.subject for s in detected.spans], ["MATH", "REAS"])


# ---------------------------------------------------------------------------
# keyword subject map
# ---------------------------------------------------------------------------


class KeywordMapTests(unittest.TestCase):
    def test_core_labels(self) -> None:
        cases = {
            "Analogy": "REAS",
            "Coding-Decoding": "REAS",
            "Non Verbal Reasoning": "REAS",
            "Series": "REAS",
            "Polity": "GK",
            "Indian Geography": "GK",
            "Grammar": "ENG",
            "Vocabulary": "ENG",
            "Percentage": "MATH",
            "Profit & Loss": "MATH",
            "Operating Systems": "COMPUTER",
        }
        for label, expected in cases.items():
            with self.subTest(label=label):
                self.assertEqual(keywords.subject_for_text(label), expected)

    def test_conflicts_resolved(self) -> None:
        # would be MATHS through the bare word "graph"
        self.assertEqual(keywords.subject_for_text("Geography (World Geography)"), "GK")
        # would be ENG through the bare word "verbal"
        self.assertEqual(keywords.subject_for_text("Non Verbal Reasoning"), "REAS")
        # would be REAS through the substring "dice" in "In-dice-s"
        self.assertEqual(keywords.subject_for_text("Surds and Indices"), "MATH")
        # would be REAS through "classification"
        self.assertEqual(
            keywords.subject_for_text("Classification of Elements and Periodicity in Properties"),
            "GK",
        )
        # would be MATH through the bare word "age"
        self.assertEqual(keywords.subject_for_text("Gupta Age"), "GK")
        # would be MATH through "river"
        self.assertEqual(keywords.subject_for_text("Indian Rivers and Water Resources"), "GK")

    def test_ambiguous_labels_yield_none(self) -> None:
        for label in ("Unidentified", "Miscellaneous", ""):
            with self.subTest(label=label):
                self.assertIsNone(keywords.subject_for_text(label))

    def test_multi_label_values(self) -> None:
        self.assertEqual(keywords.subject_for_text("Art and Culture, Famous People"), "GK")
        self.assertEqual(keywords.subject_for_text("Coding-Decoding, Reasoning"), "REAS")


# ---------------------------------------------------------------------------
# vocab buckets
# ---------------------------------------------------------------------------


class VocabExtractionTests(unittest.TestCase):
    def test_asked_term_bold_layout(self) -> None:
        prompt = "**Select the correct synonym of the given word.**\n\nScintillating"
        self.assertEqual(vocab.asked_term(prompt), "Scintillating")

    def test_asked_term_direction_layout(self) -> None:
        prompt = "Direction: Select the most appropriate synonym of the underlined word\nABANDON"
        self.assertEqual(vocab.asked_term(prompt), "ABANDON")

    def test_asked_term_never_returns_instruction(self) -> None:
        prompt = "Select the most appropriate synonym of the underlined word\n"
        self.assertEqual(vocab.asked_term(prompt), "")

    def test_asked_term_idiom(self) -> None:
        prompt = "Select the most appropriate meaning of the given idiom\n\nA snake in the grass"
        self.assertEqual(vocab.asked_term(prompt), "A snake in the grass")

    def test_kind_detection(self) -> None:
        self.assertEqual(vocab.detect_kind("Synonym", ["Vocabulary"], ""), "synonym")
        self.assertEqual(vocab.detect_kind("Antonym", ["Vocabulary"], ""), "antonym")
        self.assertEqual(vocab.detect_kind("OWS", ["Vocabulary"], ""), "ows")
        self.assertEqual(vocab.detect_kind("Idioms", ["Vocabulary"], ""), "idiom")
        self.assertEqual(vocab.detect_kind("Spelling", ["Vocabulary"], ""), "spelling")
        self.assertEqual(vocab.detect_kind(None, [], "Select the correct synonym of the given word."), "synonym")
        self.assertIsNone(vocab.detect_kind("Grammar", ["Grammar"], "Choose the correct option"))

    def test_extract_question(self) -> None:
        question = {
            "qid": "x1",
            "concept": "Synonym",
            "tags": ["Vocabulary"],
            "question": "Select the synonym of the given word.\n\nPATHETIC",
            "options": [
                {"label": "1", "text": "Curious"},
                {"label": "2", "text": "Pitiful"},
                {"label": "3", "text": "Insignificant"},
                {"label": "4", "text": "Dull"},
            ],
            "correct": "2",
        }
        item = vocab.extract_question(question, {"exam": "CGL", "year": 2024, "paper_path": "p.json"})
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.kind, "synonym")
        self.assertEqual(item.term, "PATHETIC")
        self.assertEqual(item.correct_option, "Pitiful")


class VocabBucketTests(unittest.TestCase):
    def _item(self, kind, term, options, correct=""):
        return vocab.VocabularyQuestion(
            qid=f"{kind}-{term}",
            kind=kind,
            term=term,
            options=list(options),
            correct_option=correct,
            exam="CGL",
            year=2024,
        )

    def test_four_buckets_are_independent(self) -> None:
        items = [
            self._item("synonym", "Abandon", ["Forsake", "Keep"], "Forsake"),
            self._item("synonym", "Forsake", ["Abandon", "Retain"], "Abandon"),
            self._item("antonym", "Abandon", ["Retain", "Forsake"], "Retain"),
        ]
        buckets = vocab.build_buckets(items)["tables"]
        syn = {row["word"]: row for row in buckets["synonym"]}
        ant = {row["word"]: row for row in buckets["antonym"]}
        self.assertEqual(syn["Abandon"]["asMain"], 1)
        self.assertEqual(syn["Abandon"]["asOption"], 1)
        self.assertEqual(syn["Abandon"]["asOptionCorrect"], 1)
        self.assertEqual(ant["Abandon"]["asMain"], 1)
        self.assertEqual(ant["Abandon"]["asOption"], 0)

    def test_own_options_are_never_counted_for_the_main_term(self) -> None:
        buckets = vocab.build_buckets([self._item("synonym", "Abandon", ["Forsake", "Retain"], "Forsake")])["tables"]
        syn = {row["word"]: row for row in buckets["synonym"]}
        self.assertEqual(syn["Abandon"]["asMain"], 1)
        self.assertEqual(syn["Abandon"]["asOption"], 0)
        self.assertEqual(syn["Forsake"]["asOption"], 1)

    def test_duplicate_options_count_once(self) -> None:
        buckets = vocab.build_buckets([self._item("ows", "A period of ten years", ["Decade", "Decade"], "Decade")])["tables"]
        ows = {row["word"]: row for row in buckets["ows"]}
        self.assertEqual(ows["Decade"]["asOption"], 1)

    def test_importance_and_rank_order(self) -> None:
        items = [
            self._item("synonym", "Alpha", ["Beta"], ""),
            self._item("synonym", "Alpha", ["Gamma"], ""),
            self._item("synonym", "Delta", ["Alpha"], ""),
        ]
        table = vocab.build_buckets(items)["tables"]["synonym"]
        self.assertEqual(table[0]["word"], "Alpha")
        self.assertEqual(table[0]["asMain"], 2)
        self.assertEqual(table[0]["importance"], 6 + 1)
        ranks = [row["rank"] for row in table]
        self.assertEqual(ranks, sorted(ranks))

    def test_spelling_prompts_do_not_leak_instructions(self) -> None:
        question = {
            "qid": "s1",
            "concept": "Spelling",
            "tags": ["Vocabulary"],
            "question": "Select the misspelt word",
            "options": [
                {"label": "1", "text": "Bureaucracy"},
                {"label": "2", "text": "Consciencious"},
                {"label": "3", "text": "Accommodate"},
                {"label": "4", "text": "Priviledge"},
            ],
            "correct": "1",
        }
        item = vocab.extract_question(question, {"exam": "CGL", "year": 2024, "paper_path": "p.json"})
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.term, "Bureaucracy")

    def test_boilerplate_options_are_dropped(self) -> None:
        question = {
            "qid": "o1",
            "concept": "OWS",
            "tags": ["Vocabulary"],
            "question": "Select one word for the following group of words.\n\nA period of ten years",
            "options": [
                {"label": "1", "text": "Century"},
                {"label": "2", "text": "Fortnight"},
                {"label": "3", "text": "No substitution required"},
                {"label": "4", "text": "Decade"},
            ],
            "correct": "4",
        }
        item = vocab.extract_question(question, {"exam": "CGL", "year": 2024, "paper_path": "p.json"})
        assert item is not None
        self.assertNotIn("No substitution required", item.options)
        self.assertEqual(item.term, "A period of ten years")


# ---------------------------------------------------------------------------
# alias mapping
# ---------------------------------------------------------------------------


class AliasMappingTests(unittest.TestCase):
    def test_norm_key_equivalences(self) -> None:
        self.assertEqual(norm_key("Profit and Loss"), norm_key("Profit & Loss"))
        self.assertEqual(norm_key("Coding-Decoding"), norm_key("Coding Decoding"))
        self.assertEqual(norm_key("  Time   and  Work "), norm_key("Time & Work"))

    def test_curated_aliases(self) -> None:
        from agent.taxonomy import CURATED_ALIASES

        curated = {norm_key(k): v for k, v in CURATED_ALIASES.items()}
        self.assertEqual(curated[norm_key("Profit and Loss")], "Profit & Loss")
        self.assertEqual(curated[norm_key("Coding Decoding")], "Coding-Decoding")
        self.assertEqual(curated[norm_key("Time and Work")], "Time & Work")

    def test_resolver_uses_alias_map(self) -> None:
        alias_map = {
            "map": {
                norm_key("Profit and Loss"): {"canonical": "Profit & Loss", "subject": "MATH", "chapter": "Profit & Loss", "topic": None, "source": "curated"},
                norm_key("Unidentified"): {"canonical": None, "subject": None, "chapter": None, "topic": None, "source": "unmapped"},
            }
        }
        resolver = ConceptResolver(alias_map)
        resolved = resolver.resolve("Profit and Loss", [])
        self.assertEqual(resolved.canonical, "Profit & Loss")
        self.assertEqual(resolved.chapter, "Profit & Loss")
        self.assertIsNone(resolver.resolve("Unidentified", []).canonical)
        # tag fallback when the concept does not resolve
        self.assertEqual(resolver.resolve("Unidentified", ["Profit and Loss"]).canonical, "Profit & Loss")

    def test_classifier_fallback_ladder(self) -> None:
        from agent.config import exams

        table = exams()
        taxonomy = {"subjects": {}}
        for code, exam in table.exams.items():
            taxonomy["subjects"][code] = {"chapters": []}
        classifier = Classifier(taxonomy, {"map": {}})
        # An unresolvable label keeps its raw text as the canonical concept but
        # must NOT be placed: placement requires a chapter, and the ladder never
        # invents one.
        result = classifier.classify("Some Niche Label", [], "MATH")
        self.assertFalse(result.placed)
        self.assertIsNone(result.chapter)
        self.assertEqual(result.concept, "Some Niche Label")

    def test_classifier_never_borrows_another_subjects_chapter(self) -> None:
        classifier = Classifier(
            {
                "subjects": {
                    "GK": {"chapters": [{"name": "MODERN INDIAN HISTORY", "topics": []}]},
                    "ENG": {"chapters": [{"name": "Vocabulary", "topics": [{"name": "Synonym"}]}]},
                }
            },
            {
                "map": {
                    norm_key("Modern India (National Movement )"): {
                        "canonical": "Modern Indian History",
                        "subject": "GK",
                        "chapter": "MODERN INDIAN HISTORY",
                        "topic": None,
                        "source": "curated",
                    },
                    norm_key("Synonym"): {
                        "canonical": "Synonym",
                        "subject": "ENG",
                        "chapter": "Vocabulary",
                        "topic": "Synonym",
                        "source": "taxonomy",
                    },
                }
            },
        )
        # an English question whose label names a GK chapter must not land in GK
        result = classifier.classify("Modern India (National Movement )", [], "ENG")
        self.assertNotEqual(result.chapter, "MODERN INDIAN HISTORY")
        # the same label on a GK question keeps the GK chapter
        self.assertEqual(
            classifier.classify("Modern India (National Movement )", [], "GK").chapter,
            "MODERN INDIAN HISTORY",
        )

    def test_subject_index_topic_returns_parent_chapter(self) -> None:
        index = SubjectIndex("ENG", {"chapters": [{"name": "Vocabulary", "topics": [{"name": "Synonym"}]}]})
        self.assertEqual(index.match_chapter("Synonym"), "Vocabulary")
        self.assertEqual(index.match_chapter("Vocabulary"), "Vocabulary")
        self.assertIsNone(index.match_chapter("Quantum Chromodynamics"))

    def test_maths_fallbacks_target_real_chapters(self) -> None:
        names = {chapter for _, chapter in MATH_FALLBACK}
        self.assertIn("Profit & Loss", names)
        self.assertIn("Percentage", names)


if __name__ == "__main__":
    unittest.main()
