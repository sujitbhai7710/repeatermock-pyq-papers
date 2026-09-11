# Architecture

## 1. Pipeline

```text
                chapter-and-topic/*.md                 SSC-*/**/*.json
                          │                                   │
                 ┌────────▼────────┐                ┌─────────▼─────────┐
                 │  taxonomy.py    │                │   discover.py     │
                 │  MD -> JSON     │                │  walk + year rule │
                 └────────┬────────┘                └─────────┬─────────┘
                          │ taxonomy.json                     │ PaperRecord[]
                          │ alias_map.json                     │
                          │                          ┌─────────▼─────────┐
                          │                          │    sections.py    │
                          │                          │ Hindi · ordinal   │
                          │                          │ signature · detect│
                          │                          └─────────┬─────────┘
                          │                                    │
                          └──────────────┬─────────────────────┘
                                         ▼
                                 ┌───────────────┐
                                 │  classify.py  │  5-step ladder
                                 └───────┬───────┘
                                         ▼
                                 ┌───────────────┐
                                 │  indexer.py   │  state/index/*.jsonl + manifest
                                 │  coverage     │  (the single source for
                                 └───────┬───────┘   every later phase)
                                         ▼
        ┌──────────────────┬─────────────┴────────────┬───────────────────┐
        ▼                  ▼                          ▼                   ▼
 distribution.py      build_db.py                 vocab.py            mockdata.py
 (phase 0 report)   (phase 1..5 tree)          grammar.py          (pack catalogue)
```

Phase order is fixed: **phase0 → phase1 (ENG) → phase2 (GK) → phase3 (MATH) →
phase4 (REAS) → phase5 (COMPUTER)**.

## 2. Module responsibilities

| Module | Responsibility | Key public API |
|---|---|---|
| `paths.py` | project-root auto-detection, path constants | `find_project_root`, `PROJECT_ROOT`, `STATE_DIR`, … |
| `config.py` | settings + exam/layout table, typed dataclasses | `settings()`, `exams()`, `PaperKind.spans_for(length, year)` |
| `keywords.py` | curated subject keyword table (exact anchors + ordered regexes) | `subject_for_text`, `keyword_table_export` |
| `taxonomy.py` | MD parsers, supplementary merge, alias map, resolver | `build_taxonomy`, `build_alias_map`, `ConceptResolver` |
| `discover.py` | paper walk, year rule, shift/date parsing | `discover`, `load_paper`, `PaperRecord` |
| `sections.py` | ordinal map, `n`-reset sections, section-level + question-level signature validation, layout detection, Hindi | `analyze_paper`, `detect_layout`, `validate`, `validate_sections`, `make_hindi_policy` |
| `classify.py` | concept/tags → canonical concept → chapter/topic, cross-subject + leaf-hygiene guards | `Classifier.classify`, `Classifier.foreign_vocabulary`, `LEAF_HYGIENE_SUBJECTS` |
| `indexer.py` | index building, coverage identity | `build_index`, `coverage`, `write_index`, `read_index` |
| `distribution.py` | distribution, deltas, importance, priorities, markdown | `build_distribution`, `render_distribution_md` |
| `build_db.py` | `database/**` writer (collapsing tree, pruned rebuild), `_meta` documents | `write_question_tree`, `leaf_levels`, `prune_subject_tree`, `write_meta` |
| `vocab.py` | synonym/antonym/OWS/idiom/spelling/homonym buckets | `build_vocabulary_db`, `extract_question`, `build_buckets` |
| `grammar.py` | grammar question → one of 129 rules | `build_grammar_db`, `match_rule`, `detect_type` |
| `mockdata.py` | mock pack catalogue and payloads | `write_mock_catalogue`, `build_packs` |
| `tools/audit_db.py` | structural self-check of `database/` (7 rules, non-zero exit) | `run_audit`, `AuditReport`, `format_report` |
| `llm.py` | OpenAI-compatible chat over `urllib` | `chat_completion`, `extract_json`, `provider_keys` |
| `router.py` | provider order, key rotation, **GLOBAL HALT** | `Router.chat`, `get_router`, `GlobalHalt` |
| `debate.py` | propose → criticise → rebut → final verdict + provenance | `Debate.run` |
| `verify.py` | batched, resumable, idempotent AI re-check | `verify_database`, `apply_corrections` |
| `websearch.py` | Monid TinyFish `/search` and `/fetch` with cache | `TinyFishClient.search/fetch` |
| `checkpoint.py` | work window + atomic checkpoints | `WorkWindow`, `Checkpoint`, `save_checkpoint` |
| `tracking.py` | progress, journal, manifest, `PROGRESS.md` | `record_phase`, `journal`, `record_files` |
| `phases/*` | the six phases (shared shape in `subject_phase.py`) | `run(ctx) -> PhaseResult` |

## 3. Subject assignment (the heart of the system)

### 3.1 Why position needs care

The source `n` field restarts at 1 for every section, so it cannot be used as a
paper position. Two structural facts replace it:

1. **global ordinal** = `index` in `questions[]` + 1 → the *declared* position;
2. **section boundaries** = wherever `n` does not increase → the *actual* shape.

`SCG CGL 2024 T1`: `n = 1..25, 1..25, 1..25, 1..25` → four sections of 25, and
the declared layout `REAS, GK, MATH, ENG` is correct.

`SSC MTS 2024`: `n = 1..38, 3..39, 3..49, 1..36` → four sections of 20/20/25/25;
the declared layout for a 90-question MTS paper is `MATH, REAS, GK, ENG`.

### 3.2 Two signals, one decision

```text
positional = layout_table(exam, kind, year, length)[ordinal]
keyword    = keywords.py(concept | tags)

# primary: modal subject per positional section
section_agreement  = count(modal(section) == layout(section)) / count(sections with a modal)
# secondary: per question, reported (and used as the verdict only when the
# paper has no declared section at all)
agreement = count(positional == keyword) / count(keyword is not None)
```

* ambiguous labels (`Unidentified`, `Miscellaneous`, …) resolve to `None` and are
  **excluded from the denominator** instead of counting as disagreement;
* `section_agreement >= signature.min_section_agreement` (0.95) → `layout_source = canonical`
  (one or two noisy source labels cannot flip a whole section; the per-question
  metric stays in the report as the secondary signal);
* otherwise the paper is flagged and `detect_layout()` runs:
  * sections from `n` resets;
  * one subject per section by keyword majority (ties broken deterministically);
  * adjacent equal labels merged;
  * unlabelled sections inherit the nearest labelled neighbour;
  * accepted only if **every voting section has a decisive majority** (≥3 votes,
    ≥60% winner) **and** coverage ≥ 60% (or the paper has a single section, in
    which case a decisive majority is the best evidence available).
* `comparable == 0` (no usable label anywhere) → the declared layout is kept,
  marked `canonical-unverified`, and the paper stays flagged. There is no
  evidence of mis-assignment, only an absence of evidence.

### 3.3 Coverage identity

```text
placed                     subject assigned AND a chapter resolved
unclassified               subject assigned but no chapter (incl. `Unidentified`)
skipped_hindi              prompt or option contains [\u0900-\u097F]
flagged_papers_questions   paper not placeable (no confident layout)
```

The four buckets are disjoint and exhaustive; `indexer.coverage` asserts
`placed + skipped_hindi + unclassified + flagged == questions`.

## 4. Classification ladder

```text
1 alias     exact hit in alias_map.json      (curated > taxonomy > observed)
2 chapter   the label contains a chapter name of the question's subject
3 topic     the label contains a topic name   (returns the parent chapter)
4 keyword   curated per-subject regex rules   (target must exist in the taxonomy)
5 unmapped  -> status = unclassified
```

An alias may resolve to a **different** subject's chapter (the corpus contains
questions whose label names another subject, e.g. an English question tagged
`Modern India (National Movement )`). In that case the foreign chapter is
rejected and the ladder continues locally, so a question never lands in another
subject's tree.

## 5. AI layer

```text
phases/phaseN.run
  └─ verify.verify_database(phase=...)
       └─ Debate.run(item_id=batch_fingerprint, task="verify", payload={batch})
            ├─ DeepSeek V4 Flash  proposal        (router.chat, proposer_order)
            ├─ GPT-5.6 Sol        agree|counter    (router.chat, critic_order)
            ├─ DeepSeek V4 Flash  one rebuttal     (max_rounds = 1)
            └─ GPT-5.6 Sol        FINAL + provenance
       └─ corrections -> state/corrections.jsonl -> apply_corrections(index)
```

* **resumability**: `state/verify_state.json` keeps the verified batch
  fingerprints per phase; a restart skips them.
* **idempotency**: `apply_corrections` only writes fields whose corrected value
  differs, so replaying the same corrections changes nothing.
* **halt**: `Router` raises `GlobalHalt`; the phase checkpoints and returns
  `status=rate_limited`.

## 6. Determinism

* no randomness anywhere in the pipeline (no `random`, no `uuid` outside run ids);
* every sort has an explicit tie-break (score, then name, then id);
* JSON is written with `sort_keys=True` for records and `indent=2` for
  documents; JSONL records are key-sorted;
* all file writes are atomic (`tempfile.mkstemp` + `os.replace`);
* therefore: same input → byte-identical `state/` and `database/`.
