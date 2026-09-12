"""Regression tests for the second review pass (A1-A4, B1).

Covers

* A1 the GK parser no longer lets §39 *Scientists, Inventions & Discoveries*
  swallow the science concepts that §18/§19/§20 declare as chapters;
* A2 case-insensitive label/slug stability — case-only variants resolve to one
  canonical leaf and no pack is silently overwritten;
* A3 vocabulary tables count ``Abandon``/``abandon`` and
  ``To spill the beans``/``spill the beans`` as one entry;
* A4 a leaf's chapter/topic must be consistent with the concept's taxonomy
  parent, otherwise the writer parks it under ``_other`` (audit rule 6);
* B1 the sharded question index (``state/index/<subject>.<n>.jsonl``) with a
  manifest, a compatibility loader and a stable qid digest.
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


import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import build_db, indexer, mockdata, paths, taxonomy, vocab
from agent.classify import OTHER_BUCKET, Classifier, SubjectIndex
from agent.util import fold_key, norm_key
from tools import audit_db


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def record(subject, chapter, topic, concept, qid, *, bucket=None, ordinal=1, paper="p.json"):
    return {
        "subject": subject,
        "chapter": chapter,
        "topic": topic,
        "concept": concept,
        "leaf_bucket": bucket,
        "qid": qid,
        "ordinal": ordinal,
        "paper_path": paper,
        "exam": "CGL",
        "year": 2024,
    }


def small_taxonomy(chapters, subject="GK"):
    return {
        "subjects": {
            subject: {
                "chapters": [
                    {
                        "id": str(i),
                        "name": name,
                        "key": norm_key(name),
                        "topics": [{"name": t, "key": norm_key(t), "concepts": []} for t in topics],
                        "concepts": [{"name": c, "key": norm_key(c)} for c in concepts],
                    }
                    for i, (name, topics, concepts) in enumerate(chapters, start=1)
                ]
            }
        }
    }


# ---------------------------------------------------------------------------
# A1 - GK parser: science concepts belong to the science chapters
# ---------------------------------------------------------------------------


class GkChapterScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        text = (paths.TAXONOMY_DIR / paths.TAXONOMY_FILES["GK"]).read_text(encoding="utf-8")
        self.body = taxonomy.parse_gk(text)
        self.relocations = taxonomy.relocate_cross_chapter_concepts(self.body)
        self.by_name = {chapter["name"]: chapter for chapter in self.body["chapters"]}

    def _concepts(self, chapter_name: str) -> set:
        chapter = self.by_name[chapter_name]
        names = {concept["name"] for concept in chapter.get("concepts", [])}
        for topic in chapter.get("topics", []):
            names |= {concept["name"] for concept in topic.get("concepts", [])}
            for sub in topic.get("subtopics", []):
                names |= {concept["name"] for concept in sub.get("concepts", [])}
        return names

    def test_science_concepts_moved_to_their_own_chapters(self) -> None:
        sections = {
            "GENERAL SCIENCE — PHYSICS": "Physics",
            "GENERAL SCIENCE — CHEMISTRY": "Chemistry",
            "GENERAL SCIENCE — BIOLOGY": "Biology",
        }
        for chapter, concept in sections.items():
            with self.subTest(concept=concept):
                self.assertIn(concept, self._concepts(chapter))
        # ... and are gone from §39 (the old, wrong home)
        inventors = self._concepts("SCIENTISTS, INVENTIONS & DISCOVERIES")
        for concept in ("Physics", "Chemistry", "Biology", "Genetics"):
            with self.subTest(concept=concept):
                self.assertNotIn(concept, inventors)

    def test_genuine_scientist_questions_stay_in_39(self) -> None:
        inventors = self._concepts("SCIENTISTS, INVENTIONS & DISCOVERIES")
        self.assertIn("Telephone", inventors)          # 39.2 Inventions
        self.assertIn("Penicillin", inventors)         # 39.3 Discoveries
        self.assertIn("Scientist", inventors)          # 39.1 Scientists
        self.assertIn("Scientists", inventors)         # 39.1 topic

    def test_relocation_is_recorded_and_never_drops_a_bullet(self) -> None:
        self.assertGreater(self.relocations["moved"], 0)
        for move in self.relocations["moves"]:
            self.assertTrue(move["concept"] and move["from"] and move["to"])
            self.assertNotEqual(move["from"], move["to"])
        # the moved bullets are reported with their target
        physics = [m for m in self.relocations["moves"] if m["concept"] == "Physics"]
        self.assertEqual(physics[0]["to"], "GENERAL SCIENCE — PHYSICS")

    def test_ambiguous_names_are_left_alone(self) -> None:
        body = {
            "chapters": [
                {"id": "1", "name": "A — SHARED", "key": "a shared", "topics": [], "concepts": []},
                {"id": "2", "name": "B — SHARED", "key": "b shared", "topics": [], "concepts": []},
                {
                    "id": "3",
                    "name": "HOLDER",
                    "key": "holder",
                    "topics": [],
                    "concepts": [{"name": "Shared", "key": "shared"}],
                },
            ]
        }
        removed = taxonomy.relocate_cross_chapter_concepts(body)
        self.assertEqual(removed["moved"], 0)
        self.assertIn("Shared", [c["name"] for c in body["chapters"][2]["concepts"]])


class SupplementaryDuplicateTests(unittest.TestCase):
    def test_case_duplicate_chapter_is_merged_into_the_markdown_chapter(self) -> None:
        taxonomy_doc = {
            "subjects": {"GK": {"chapters": [{"id": "27", "name": "STATIC GK", "topics": [], "concepts": []}]}},
            "counts": {"GK": {"chapters": 1, "topics": 0, "concepts": 0}},
            "sources": {},
        }
        merged = taxonomy.merge_supplementary(taxonomy_doc, paths.SUPPLEMENTARY_TAXONOMY_FILE)
        names = [c["name"] for c in merged["subjects"]["GK"]["chapters"]]
        # the supplementary "Static GK" must not create a second, case-different chapter
        self.assertNotIn("Static GK", names)
        self.assertEqual(names.count("STATIC GK"), 1)
        # its topics are merged into the markdown chapter instead
        topics = [t["name"] for t in merged["subjects"]["GK"]["chapters"][0]["topics"]]
        self.assertIn("History", topics)
        duplicates = merged["supplementary"]["duplicates_merged"]
        self.assertTrue(any(d["chapter"] == "Static GK" and d["into"] == "STATIC GK" for d in duplicates))


# ---------------------------------------------------------------------------
# A2 - case-insensitive labels and slugs
# ---------------------------------------------------------------------------


class CaseStabilityTests(unittest.TestCase):
    def test_label_index_keeps_one_spelling_per_case_fold(self) -> None:
        taxonomy_doc = small_taxonomy([("WORLD GEOGRAPHY", ["Geography"], [])])
        labels = taxonomy.build_label_index(taxonomy_doc)
        self.assertEqual(labels[fold_key("World Geography")], "WORLD GEOGRAPHY")
        self.assertEqual(labels[fold_key("world geography")], "WORLD GEOGRAPHY")

    def test_classifier_canonicalises_case_variants(self) -> None:
        taxonomy_doc = small_taxonomy([("MEDIEVAL INDIAN HISTORY", ["History"], [])])
        classifier = Classifier(taxonomy_doc, {"map": {}})
        upper = classifier.classify("MEDIEVAL INDIAN HISTORY", [], "GK")
        mixed = classifier.classify("Medieval Indian History", [], "GK")
        self.assertEqual(upper.concept, mixed.concept)
        self.assertEqual(mixed.concept, "MEDIEVAL INDIAN HISTORY")
        self.assertEqual(
            build_db.leaf_levels(record("GK", upper.chapter, None, upper.concept, "a")),
            build_db.leaf_levels(record("GK", mixed.chapter, None, mixed.concept, "b")),
        )

    def test_case_only_concept_variants_share_one_leaf(self) -> None:
        records = [
            record("GK", "WORLD GEOGRAPHY", "Geography", "WORLD GEOGRAPHY", "q1"),
            record("GK", "WORLD GEOGRAPHY", "Geography", "World Geography", "q2"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            summary = build_db.write_question_tree(records, database_dir=base)
            leaves = list(base.rglob("questions.jsonl"))
            self.assertEqual(len(leaves), 1)
            self.assertEqual(summary["leaves"], 1)
            self.assertEqual(len(leaves[0].read_text(encoding="utf-8").strip().splitlines()), 2)

    def test_mock_pack_ids_do_not_collide_for_case_variants(self) -> None:
        records = [
            record("GK", "STATIC GK", None, "Static GK", "q1", ordinal=1),
            record("GK", "STATIC GK", None, "STATIC GK", "q2", ordinal=2),
        ]
        packs = mockdata.build_packs(records)
        ids = [(p.kind, p.id) for p in packs]
        self.assertEqual(len(ids), len(set(ids)))
        chapters = [p for p in packs if p.kind == "chapter"]
        self.assertEqual(len(chapters), 1)
        self.assertEqual(len(chapters[0].questions), 2)

    def test_identical_pack_ids_get_a_stable_suffix_instead_of_overwriting(self) -> None:
        packs = [
            mockdata.Pack("chapter", "a-b", "GK", 1, None, None, ["q1"], ("gk", "a b")),
            mockdata.Pack("chapter", "a-b", "GK", 1, None, None, ["q2"], ("gk", "a-b")),
        ]
        renamed = mockdata._disambiguate_ids(packs)
        self.assertEqual(renamed, 1)
        self.assertEqual(packs[0].id, "a-b")
        self.assertNotEqual(packs[1].id, "a-b")
        self.assertTrue(packs[1].id.startswith("a-b--"))


# ---------------------------------------------------------------------------
# A3 - vocabulary tables are case/diacritic-insensitive
# ---------------------------------------------------------------------------


class VocabCaseFoldingTests(unittest.TestCase):
    def _item(self, kind, term, options=(), correct=""):
        return vocab.VocabularyQuestion(
            qid=f"{kind}-{term}-{len(options)}",
            kind=kind,
            term=term,
            options=list(options),
            correct_option=correct,
            exam="CGL",
            year=2024,
        )

    def test_entry_key_folds_case_and_strips_idiom_to(self) -> None:
        self.assertEqual(vocab.entry_key("synonym", "Abandon"), "abandon")
        self.assertEqual(vocab.entry_key("synonym", "ABANDON"), "abandon")
        self.assertEqual(vocab.entry_key("idioms", "To spill the beans"), "spill the beans")
        self.assertEqual(vocab.entry_key("idioms", "spill the beans"), "spill the beans")
        # non-idiom tables keep a literal leading "to"
        self.assertNotEqual(vocab.entry_key("ows", "To err"), "err")

    def test_synonym_rows_merge_case_variants(self) -> None:
        items = [
            self._item("synonym", "Abandon", ["Forsake"], "Forsake"),
            self._item("synonym", "abandon", ["Retain"], "Retain"),
            self._item("synonym", "ABANDON", ["Leave"], "Leave"),
            # a question whose *option* is the same entry in another spelling
            self._item("synonym", "Relinquish", ["abandon"], "abandon"),
        ]
        table = vocab.build_buckets(items)["tables"]["synonym"]
        rows = {fold_key(row["word"]): row for row in table}
        # the three spellings are one *entry*: one row, not three
        self.assertEqual(len([row for row in table if fold_key(row["word"]) == "abandon"]), 1)
        row = rows["abandon"]
        self.assertEqual(row["asMain"], 3)
        self.assertEqual(row["asOption"], 1)  # counted once, not once per spelling
        self.assertEqual(row["asOptionCorrect"], 1)
        self.assertEqual(sorted(row["variants"]), ["ABANDON", "Abandon", "abandon"])

    def test_idiom_rows_merge_to_variants(self) -> None:
        items = [
            self._item("idiom", "To spill the beans", ["To reveal a secret"], "To reveal a secret"),
            self._item("idiom", "spill the beans", ["To keep quiet"], "To keep quiet"),
        ]
        table = vocab.build_buckets(items)["tables"]["idioms"]
        mains = [row for row in table if row["asMain"]]
        self.assertEqual(len(mains), 1)
        self.assertEqual(mains[0]["asMain"], 2)
        self.assertEqual(sorted(mains[0]["variants"]), ["To spill the beans", "spill the beans"])

    def test_own_option_rule_uses_the_folded_key(self) -> None:
        # "Forsake" as the option of a question whose term is "forsake" is its own
        # term and must not be counted as a separate option occurrence
        items = [self._item("synonym", "forsake", ["Forsake", "Keep"], "Forsake")]
        table = vocab.build_buckets(items)["tables"]["synonym"]
        rows = {fold_key(r["word"]): r for r in table}
        self.assertEqual(rows["forsake"]["asMain"], 1)
        self.assertEqual(rows["forsake"]["asOption"], 0)


# ---------------------------------------------------------------------------
# A4 - chapter/topic parent consistency
# ---------------------------------------------------------------------------


class ParentConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.taxonomy = {
            "subjects": {
                "MATH": {
                    "chapters": [
                        {
                            "id": "6",
                            "name": "Ratio",
                            "topics": [],
                            "concepts": [{"name": "TYPE-I Basic Questions", "key": "type i basic questions"}],
                        },
                        {
                            "id": "26",
                            "name": "Mensuration 2D & 3D",
                            "topics": [{"name": "2D", "concepts": []}],
                            "concepts": [],
                        },
                        {"id": "14", "name": "Time & Distance", "topics": [], "concepts": []},
                    ]
                },
                "GK": {"chapters": [{"id": "1", "name": "WORLD GEOGRAPHY", "topics": [], "concepts": []}]},
            }
        }
        self.classifier = Classifier(self.taxonomy, {"map": {}})

    def test_word_boundary_matching_fixes_the_ratio_stem_bug(self) -> None:
        index = SubjectIndex("MATH", self.taxonomy["subjects"]["MATH"])
        self.assertIsNone(index.match_chapter("Mensuration"))
        self.assertEqual(index.match_chapter("Speed Time and Distance"), "Time & Distance")
        self.assertEqual(index.match_chapter("Mensuration 2D & 3D"), "Mensuration 2D & 3D")

    def test_concept_declared_by_another_chapter_is_repointed(self) -> None:
        result = self.classifier.classify("Mensuration 2D & 3D", [], "MATH")
        self.assertEqual(result.chapter, "Mensuration 2D & 3D")
        self.assertIsNone(result.leaf_bucket)

    def test_declared_scope_allows_token_subset_of_the_chapter_name(self) -> None:
        scope = taxonomy.ConceptScope(self.taxonomy)
        self.assertTrue(scope.declares("MATH", "Mensuration 2D & 3D", "Mensuration"))
        self.assertFalse(scope.declares("MATH", "Mensuration 2D & 3D", "World Geography"))
        self.assertTrue(scope.declares("MATH", "Ratio", "TYPE-I Basic Questions"))

    def test_undeclared_concept_gets_the_other_bucket(self) -> None:
        result = self.classifier.classify("Speed Time & Distance", [], "MATH")
        self.assertEqual(result.chapter, "Time & Distance")
        self.assertEqual(result.leaf_bucket, OTHER_BUCKET)

    def test_writer_inserts_the_other_level(self) -> None:
        declared = record("MATH", "Time & Distance", None, "Speed Time & Distance", "q1", bucket=OTHER_BUCKET)
        self.assertEqual(
            build_db.leaf_levels(declared),
            (("time-and-distance", "chapter"), ("_other", "other"), ("speed-time-and-distance", "concept")),
        )
        plain = record("MATH", "Time & Distance", None, "Speed Time & Distance", "q1")
        self.assertEqual(len(build_db.leaf_levels(plain)), 2)

    def test_cross_subject_concept_does_not_land_in_a_maths_chapter_leaf(self) -> None:
        # a world-geography label on a maths question: the classifier may not file
        # it under a maths chapter with a GK concept
        result = self.classifier.classify("World Geography", [], "MATH")
        if result.chapter:
            self.assertEqual(result.chapter, "WORLD GEOGRAPHY")  # re-pointed to its owner
        self.assertNotEqual(result.chapter, "Ratio")


class ParentConsistencyAuditTests(unittest.TestCase):
    TAXONOMY = {
        "subjects": {
            "MATH": {"chapters": [{"id": "14", "name": "Time & Distance", "topics": [], "concepts": []}]},
        }
    }

    def _write(self, base: Path, relative, rows) -> None:
        leaf = base / relative
        leaf.mkdir(parents=True, exist_ok=True)
        (leaf / "index.md").write_text("# leaf\n", encoding="utf-8")
        (leaf / "questions.jsonl").write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8"
        )

    def test_rule6_fails_for_an_undeclared_concept_and_passes_under_other(self) -> None:
        rows = [{"qid": "q1", "subject": "MATH", "chapter": "Time & Distance", "concept": "Speed Time & Distance"}]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._write(base, "maths/time-and-distance", rows)
            report = audit_db.run_audit(base, taxonomy=self.TAXONOMY)
            hits = [issue for issue in report.issues if issue.rule == "6"]
            self.assertEqual(len(hits), 1, msg=[str(i) for i in report.issues])
            self.assertFalse(report.ok)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._write(base, "maths/time-and-distance/_other", rows)
            report = audit_db.run_audit(base, taxonomy=self.TAXONOMY)
            self.assertEqual([i for i in report.issues if i.rule == "6"], [])
            self.assertTrue(report.ok, msg=[str(i) for i in report.issues])

    def test_rule6_accepts_a_declared_topic_concept(self) -> None:
        tax = {
            "subjects": {
                "MATH": {
                    "chapters": [
                        {"id": "1", "name": "Speeds", "topics": [{"name": "Average Speed", "concepts": []}], "concepts": []}
                    ]
                }
            }
        }
        rows = [{"qid": "q1", "subject": "MATH", "chapter": "Speeds", "topic": "Average Speed", "concept": "Average Speed"}]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._write(base, "maths/speeds/average-speed", rows)
            report = audit_db.run_audit(base, taxonomy=tax)
            self.assertTrue(report.ok, msg=[str(i) for i in report.issues])


class CaseStabilityAuditTests(unittest.TestCase):
    TAXONOMY = {"subjects": {"GK": {"chapters": [{"id": "1", "name": "STATIC GK", "topics": [], "concepts": []}]}}}

    def test_rule7_reports_two_labels_in_one_leaf(self) -> None:
        leaf = {"qid": "q1", "subject": "GK", "chapter": "STATIC GK", "concept": "Static GK"}
        other = {"qid": "q2", "subject": "GK", "chapter": "STATIC GK", "concept": "STATIC GK"}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / "gk" / "static-gk"
            target.mkdir(parents=True)
            (target / "index.md").write_text("# leaf\n", encoding="utf-8")
            (target / "questions.jsonl").write_text(
                json.dumps(leaf) + "\n" + json.dumps(other) + "\n", encoding="utf-8"
            )
            report = audit_db.run_audit(base, taxonomy=self.TAXONOMY)
            hits = [issue for issue in report.issues if issue.rule == "7"]
            self.assertEqual(len(hits), 1)
            self.assertIn("differ only by case", hits[0].detail)

    def test_rule7_reports_a_duplicated_mock_pack_path(self) -> None:
        pack = {"kind": "chapter", "id": "gk-x", "count": 1, "path": "database/mocks/packs/chapter/gk-x.json"}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "database"
            (base / "mocks").mkdir(parents=True)
            (base / "mocks" / "index.json").write_text(
                json.dumps({"totals": {"packs": 2}, "packs": [dict(pack), dict(pack)]}), encoding="utf-8"
            )
            report = audit_db.run_audit(base, taxonomy=self.TAXONOMY)
            hits = [issue for issue in report.issues if issue.rule == "7"]
            self.assertTrue(any("silently drops" in issue.detail for issue in hits), msg=[str(i) for i in hits])


# ---------------------------------------------------------------------------
# B1 - sharded index
# ---------------------------------------------------------------------------


def index_record(qid, subject, **extra):
    base = {
        "qid": qid,
        "subject": subject,
        "exam": "CGL",
        "year": 2024,
        "ordinal": 1,
        "paper_path": "p.json",
        "concept": "X",
        "chapter": "C",
        "status": "placed",
    }
    base.update(extra)
    return base


class ShardedIndexTests(unittest.TestCase):
    def test_shards_are_split_by_subject_and_size(self) -> None:
        records = [index_record(f"gk{i}", "GK", pad="x" * 200) for i in range(50)]
        records += [index_record(f"eng{i}", "ENG") for i in range(10)]
        shards = indexer.shard_subject_records(records, max_bytes=4096)
        names = [name for _subject, name, _lines in shards]
        self.assertEqual(names[0], "eng.1.jsonl")
        self.assertTrue(all(name.startswith("gk.") for name in names[1:]))
        self.assertGreater(len(names), 2)
        for subject, _name, lines in shards:
            size = sum(len(line.encode()) for line in lines)
            self.assertLessEqual(size, 4096 + 400)
            self.assertTrue(all(json.loads(line)["subject"] == subject for line in lines))

    def test_round_trip_and_manifest(self) -> None:
        records = [index_record(f"q{i}", "GK" if i % 2 else "MATH") for i in range(20)]
        with tempfile.TemporaryDirectory() as tmp:
            index_dir = Path(tmp) / "index"
            manifest_path = indexer.write_index(records, index_dir=index_dir)
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            self.assertEqual(manifest["records"], 20)
            self.assertEqual(manifest["format"], "jsonl-sharded")
            self.assertEqual(manifest["qid_sha256"], indexer.qid_digest(records))
            self.assertEqual(
                manifest["qid_sha256"],
                hashlib.sha256("\n".join(sorted(r["qid"] for r in records)).encode()).hexdigest(),
            )
            for entry in manifest["shards"]:
                blob = (index_dir / entry["file"]).read_bytes()
                self.assertEqual(len(blob), entry["bytes"])
                self.assertEqual(hashlib.sha256(blob).hexdigest(), entry["sha256"])
                self.assertLess(entry["bytes"], indexer.SHARD_MAX_BYTES)
            with mock.patch.object(paths, "INDEX_DIR", index_dir), mock.patch.object(
                paths, "INDEX_MANIFEST_JSON", index_dir / "manifest.json"
            ), mock.patch.object(paths, "QUESTIONS_INDEX_JSONL", Path(tmp) / "legacy.jsonl"):
                loaded = indexer.read_index()
            self.assertEqual(sorted(r["qid"] for r in loaded), sorted(r["qid"] for r in records))

    def test_reader_falls_back_to_the_legacy_single_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "questions_index.jsonl"
            legacy.write_text(json.dumps(index_record("q1", "GK")) + "\n", encoding="utf-8")
            self.assertEqual([r["qid"] for r in indexer.read_index(legacy)], ["q1"])

    def test_writer_removes_the_legacy_file_it_supersedes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            legacy = state / "questions_index.jsonl"
            legacy.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(paths, "QUESTIONS_INDEX_JSONL", legacy):
                indexer.write_index([index_record("q1", "GK")], index_dir=state / "index")
            self.assertFalse(legacy.is_file())

    def test_writer_drops_stale_shards_of_a_previous_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index_dir = Path(tmp) / "index"
            index_dir.mkdir(parents=True)
            (index_dir / "gk.9.jsonl").write_text("{}\n", encoding="utf-8")
            indexer.write_index([index_record("q1", "GK")], index_dir=index_dir)
            self.assertFalse((index_dir / "gk.9.jsonl").is_file())

    def test_qid_digest_is_order_independent(self) -> None:
        first = [index_record("b", "GK"), index_record("a", "GK")]
        second = list(reversed(first))
        self.assertEqual(indexer.qid_digest(first), indexer.qid_digest(second))

    def test_manifest_present_for_the_real_repository_index(self) -> None:
        """The committed index must be sharded (no 100 MB single file).

        Read from the *repository's* state dir: the suite redirects its own
        ``state/`` to a temporary directory (``tests/_isolation.py``), so
        ``paths.INDEX_MANIFEST_JSON`` would otherwise always be missing here.
        """
        manifest = indexer.index_manifest(_isolation.REAL_STATE_DIR / "index" / "manifest.json")
        if manifest is None:
            self.skipTest("index not generated in this checkout")
        self.assertLessEqual(len(manifest["shards"]), 64)
        for entry in manifest["shards"]:
            self.assertLess(entry["bytes"], indexer.SHARD_MAX_BYTES)
        self.assertFalse((_isolation.REAL_STATE_DIR / "questions_index.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
