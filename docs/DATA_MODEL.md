# Data model

Every generated artefact, field by field. All paths are relative to the project
root.

---

## 1. `config/` (authored, not generated)

| File | Contents |
|---|---|
| `settings.json` | work window, checkpoint interval, rate-limit policy, debate policy, verify policy, Hindi policy, signature threshold, year range |
| `exams.json` | per-exam `paper_kinds` with ordered `layout_rules` (`years` / `length` filters), `detect` flag, and a `provenance` block recording where each measured layout came from |
| `supplementary_taxonomy.json` | extra chapters/topics for labels that occur in the corpus but have no home in the four markdown files (English vocabulary/grammar/verbal, `Quantitative Aptitude`, `Logical Reasoning`, `STATES`, `COMPUTER` pseudo-subject, GK edge cases) |

`settings.json` values and their environment overrides:

| Setting | Default | Env override |
|---|---|---|
| `year_range` | `{min:2019, max:2025}` | — |
| `work_window_seconds` | `19800` | `MAX_WORK_SECONDS` |
| `checkpoint_interval_seconds` | `1800` | `CHECKPOINT_INTERVAL_SECONDS` |
| `rate_limit.consecutive_failure_threshold` | `10` | `RATE_LIMIT_THRESHOLD` |
| `debate.max_rounds` | `1` | — |
| `verify.batch_size` | `20` | — |
| `signature.min_agreement` | `0.95` | — |
| `signature.min_section_agreement` | `0.95` | — |
| `hindi.include_solution` | `false` | — |
| `hindi.exclude_punctuation` | `false` | — |

---

## 2. `state/`

### `taxonomy.json`

```jsonc
{
  "version": 1,
  "sources":  { "MATH": "chapter-and-topic/…md", … },
  "counts":   { "MATH": {"chapters": 33, "topics": 27, "concepts": 297}, … },
  "supplementary": { "source": "config/supplementary_taxonomy.json", "chapters_added": {…} },
  "subjects": {
    "MATH": { "kind": "maths", "chapter_label": "chapter", "topic_label": "topic",
      "chapters": [ { "id": "1", "name": "Percentage", "key": "percentage",
                      "topics":    [ { "name": "Mixture", "key": "mixture",
                                       "concepts": [ {"name": "TYPE-I Basic Questions", "key": "…", "tag": "AR-3E"} ] } ],
                      "concepts":  [ {"name": "…", "key": "…", "tag": "SSC-EXTRA"} ] } ] },
    "REAS": { "kind": "reasoning", "chapters": [ … ], "meta_sections": [ … ], "families": { "FAMILY 1 — VERBAL / SYMBOLIC": [ … ] } },
    "GK":   { "kind": "gk",        "chapters": [ … ], "meta_sections": [ … ] },
    "ENG":  { "kind": "english_grammar", "chapters": [ { "id": "1", "name": "Since, For, and From", "topic": "Prepositions of Time", … } ] },
    "COMPUTER": { "kind": "supplementary", "chapters": [ … ] }
  }
}
```

Parsed shapes per file: maths `# N. Chapter` (N ≤ 30) / `## Topic` / `- [TAG] …`;
reasoning `# N. SECTION` (N ≤ 59; 60–77 land in `meta_sections`) / `## N.M …` /
`# N.M …` (the file has H1 typos) plus the five `FAMILY k` blocks; GK `# N. DOMAIN`
(N ≤ 106; 107–115 are rules) / `## N.M` / `### N.M.K`; English `## Rule N: Title`
+ `**Topic:**`.

### `alias_map.json`

```jsonc
{
  "version": 1,
  "stats": { "curated": 560, "taxonomy": 329, "observed": 556, "unmapped": 2,
             "multi_whole": 1, "multi_split": 502, "multi_unresolved": 36 },
  "map": {
    "profit and loss": { "raw": "Profit and Loss", "canonical": "Profit & Loss",
                         "kind": "concept", "subject": "MATH",
                         "chapter": "Profit & Loss", "topic": null, "source": "curated" },
    "unidentified":    { "raw": "Unidentified", "canonical": null, "kind": "unmapped",
                         "subject": null, "chapter": null, "topic": null, "source": "unmapped" }
  }
}
```

Keys are normalised: casefolded, `&`→`and`, `-`/`/`/`.`→space, whitespace
collapsed. `source` ∈ `curated` | `taxonomy` | `observed` | `unmapped`.  Entries
for comma-bearing labels additionally carry `multi` ∈ `whole` | `split` |
`unresolved` (and `multi_component` for a split): the whole string is resolved
first, and only then is the label split on `,` and the first resolvable component
used.  A raw multi-label string is never used as a concept by itself.
`canonical: null` means "do not invent a concept" → the question is
`unclassified`.

### `subject_keywords.json`

The compiled keyword table: `subjects`, `exact_anchors`, `ambiguous_labels`,
`ordered_rules` (each `{subject, pattern}`). Exported so the paper-signature
check can be audited without reading code.

### `index/<subject>.<n>.jsonl` + `index/manifest.json`

One record per in-scope, non-Hindi question (138,634 records in this build),
**sharded by subject** so that no generated file exceeds 25 MB (GitHub rejects
files > 100 MB, and the pre-sharding single file was 100.6 MB).
`index/manifest.json` lists every shard with its record count, byte size and
sha256, plus `qid_sha256` (sha256 over the sorted `qid` list) which is the
index's content identity. `agent.indexer.read_index()` reads the shards, the
legacy `questions_index.jsonl` (read-only fallback) or the optional
`index/ALL.jsonl.gz` (`PYQ_INDEX_GZIP=1`) transparently.

| Field | Type | Meaning |
|---|---|---|
| `qid` | str | source question id |
| `paper_path` | str | repo-relative source paper path |
| `test_id` | str | source paper id |
| `exam` | str | `CGL` / `CHSL` / `CPO` / `MTS` / `GD` / `SELECTION_POST` / `STENO` |
| `year` | int | detected exam year |
| `shift` | int \| null | parsed from the title |
| `held_on_iso` | str | `YYYY-MM-DD` from the title, else `""` |
| `paper_kind` | str | `tier1`, `tier2_paper1`, `paper1`, … |
| `ordinal` | int | global position in the paper (index + 1) |
| `n` | int | source position inside its section |
| `qtype` | str | `mcq` / `mamcq` |
| `subject` | str | `REAS` / `GK` / `MATH` / `ENG` / `COMPUTER` |
| `subject_source` | str | `canonical` \| `detected` \| `unvalidated` |
| `signature_ok` | bool | the paper passed the signature check (section-level agreement by default, question-level when the paper has no declared section) |
| `lang` | str | always `en` (Hindi records are not written) |
| `concept_raw` | str | source `concept` verbatim |
| `tags` | list[str] | source tags |
| `concept` | str \| null | canonical concept |
| `chapter` | str \| null | taxonomy chapter (`null` ⇒ unclassified) |
| `topic` | str \| null | taxonomy topic |
| `class_source` | str | `alias` \| `chapter` \| `topic` \| `keyword` \| `ai_verified` \| `unmapped` |
| `class_confidence` | str | `high` \| `medium` \| `low` |
| `confidence` | str | source `confidence` (`high` / `unidentified`) |
| `marks_pos`, `marks_neg` | float | source marking scheme |
| `has_image` | bool | an `[IMAGE: url]` marker exists in prompt or solution (the urls themselves live in the source paper; `tools/resolve.py` returns them) |
| `correct` | str | correct option label |
| `status` | str | `placed` \| `unclassified` |
| `source` | str | `python` (flipped to `ai_verified` per field by `verify.py`) |

### `papers.json`

One record per in-scope paper:

```jsonc
{ "path": "…", "test_id": "…", "title": "…", "exam": "CGL", "year": 2024, "shift": 1,
  "hindi_count": 0, "section_sizes": [25,25,25,25], "layout_source": "canonical",
  "layout_agreement": 1.0,
  "canonical_spans":  [["REAS",1,25], …],
  "detected": { "spans": [["REAS",1,25], …], "section_subjects": ["REAS",…],
                "section_votes": [{"REAS": 25}, …], "confidence": 1.0, "ok": true, "note": "" },
  "signature": { "ok": true, "basis": "section",
                 "section_agreement": 1.0, "section_comparable": 4, "section_hits": 4,
                 "section_total": 4, "section_mismatches": [],
                 "agreement": 1.0, "comparable": 100, "hits": 100, "total": 100,
                 "positional_known": 100, "keyword_known": 100, "question_ok": true,
                 "needs_ai_review": false, "mismatches": [ … up to 3 samples … ] },
  "flagged": false, "placeable": true }
```

### Other state files

| File | Contents |
|---|---|
| `distribution.json` | phase-0 distribution: `totals`, `by_exam`, `by_exam_year`, `by_exam_shift`, `by_exam_subject`, `top_chapters/topics/concepts`, `priority{overall,concepts,topics,by_subject}`; each node has `questions`, `recency`, `last_2y`, `prev_2y`, `delta_2y`, `trend`, `importance` |
| `progress.json` | `phases.<name>.{status,started_at,updated_at,finished_at,counters,notes}` |
| `checkpoint.json` | `{run_id, phase, cursor.completed, status, started_at, updated_at, window}` |
| `journal.jsonl` | append-only `{ts, event, …counters}` |
| `manifest.json` | `phases.<name>.files[{path,bytes}]` + per-phase totals |
| `disputes.jsonl` | unresolved debate outcomes with full provenance |
| `verify_state.json` | `phases.<name>.{done:[batch fingerprints], cursor}` |
| `corrections.jsonl` | applied AI corrections: `{qid, before, after, reason, provenance, applied}` |
| `webcache.json` | `entries[sha256(endpoint+payload)] = {ts, endpoint, payload, response}` (max 5,000) |
| `labels.json` | cached raw concept/tag inventory (used by `taxonomy --from-cache`) |

---

## 3. `database/`

### `_meta/`

* **`distribution.md`** — coverage summary, questions by exam / subject / year,
  exam × subject matrix, top chapters and concepts by importance, priority
  chapters per subject, and the importance formula.
* **`coverage.md`** — the coverage identity, the four bucket definitions, the
  per-exam signature-validation table, the flagged-paper table, the Hindi
  detector variants, and the out-of-scope paper count.
* **`schema.md`** — a generated copy of this document's field tables.
* **`PROGRESS.md`** — phase status, counters and artifacts (regenerated
  frequently).
* **`papers.jsonl`** — the paper table without the heavy layout detail.
* **`flagged_papers.jsonl`** — one row per `NEEDS_AI_REVIEW` paper: section
  agreement/hits/total, per-question agreement, comparable count, canonical
  spans, detected spans + confidence, `section_mismatches`, `mismatch_samples`,
  and `status` ∈ `resolved-by-detected-layout` | `not-placed`.

### Question tree

```text
database/<subject>/index.md
database/<subject>/<chapter>/index.md
database/<subject>/<chapter>/[<topic>/]index.md
database/<subject>/<chapter>/[<topic>/]<concept>/index.md
database/<subject>/<chapter>/[<topic>/]<concept>/questions.jsonl
database/<subject>/_unclassified/{index.md,questions.jsonl}
database/english/grammar/<NN>-<slug>/{index.md,questions.jsonl,rule.json}
```

* `<subject>` ∈ `maths`, `reasoning`, `gk`, `english`, `computer`;
* directories are slugs of the taxonomy names (`profit-loss`, `indian-geography`);
* **a level is never emitted when its slug equals its parent's** — `Vocabulary >
  Vocabulary`, concept `Antonym` under topic `Antonym` and `_unclassified` under
  `_unclassified` each collapse into a single directory, so no path contains
  `a/a/`;
* a collapsed node can hold questions *and* children (`english/grammar`); it then
  gets one combined `index.md` (child table + own counts) and one
  `questions.jsonl`, so **every leaf concept owns exactly one of each**;
* `_meta`, `mocks` and `english/_analysis` are reserved and are never leaves;
* a **rule leaf** (`english/grammar/<NN>-<slug>/`, written by `agent.grammar`) is
  the *one* home of the questions its rule owns: the English concept tree is
  re-filed with those qids skipped, so a question never sits in two leaves (audit
  rule 5) and a leaf whose questions are all rule-owned disappears (the
  unassigned grammar questions stay where they are). The audit reads a rule leaf
  as a leaf like any other, so its pointer records carry the match fields *and*
  `subject`/`concept`;
* a `concept` page lists counts by exam and year and up to 200 `qid`s;
* `questions.jsonl` uses the **pointer schema**
  `qid, n, ordinal, exam, year, shift, subject, chapter, topic, concept,
  concept_raw, class_source, class_confidence, confidence, leaf_bucket,
  has_image, marks_pos, marks_neg, paper_path` (`leaf_bucket` is `null` or
  `_other`, see audit rule 6).

The subject subtree is **rebuilt** from the index on every run: the previous
revision is pruned first, so a superseded directory cannot survive as a stale
shell. `python -m agent.cli audit` (or `python tools/audit_db.py`) re-checks the
seven structural invariants (duplicated nesting, stray chapter `index.md`,
English cross-subject vocabulary, placeholder slugs, a question filed twice,
a leaf concept the chapter does not declare — those live under the explicit
`_other` level — and case-only leaf/pack collisions) and exits non-zero on any
violation.

### English analyses

The vocabulary/grammar analyses are derived views, not question-tree nodes, and
live under the reserved `english/_analysis/` directory: `english/grammar` is
itself a concept leaf of the tree (one `index.md` + one `questions.jsonl`), so
the analysis tables cannot sit next to it. The per-rule *leaves* are tree nodes
and live under `english/grammar/<NN>-<slug>/` (see the rule-leaf bullet above).

| File | Contents |
|---|---|
| `english/_analysis/vocabulary/all.json` | `{question_counts, distinct_entries, tables, repeats, items}`; `tables.<kind>[]` = `{rank, word, asMain, asOption, asOptionCorrect, total, importance, exams, years}` |
| `english/_analysis/vocabulary/synonyms.md`, `antonyms.md`, `one-word-substitution.md`, `idioms.md`, `spelling.md`, `homonyms.md` | the same tables, ranked most-important → least |
| `english/_analysis/grammar/rules.json` | the 129 rules with their title/topic keyword sets and per-rule question counts + exam breakdown |
| `english/_analysis/grammar/questions.jsonl` | matched grammar questions: `{qid, subject, exam, year, ordinal, concept, concept_raw, question_type, rule, rule_title, score, matched_terms, type_hint, match_breakdown, ai_*}` |
| `english/_analysis/grammar/unassigned.jsonl` | grammar questions no rule claimed: the same identity fields with `rule: null`, `score: 0`, `reason`/`detail`, the AI verdict (`ai_rule`) and the top candidates |
| `english/_analysis/grammar/rules.md` | rules ranked by question volume, plus rules with no questions |
| `english/solved-items.json` | phase-1 summary: vocabulary + grammar counters and file lists |
| `maths/analysis.json`, `reasoning/analysis.json`, `gk/analysis.json`, `computer/analysis.json` | per-subject chapter/topic/concept counters |

### `mocks/`

```jsonc
// index.json
{ "version": 1, "pack_size": 100, "min_size": 1, "kinds": ["concept", …],
  "totals": { "packs": 1176, "questions_in_packs": 34227 },
  "by_kind": { "concept": { "packs": 836, "questions": 16508 }, … },
  "packs": [ { "kind": "concept", "id": "math-profit-and-loss-profit-and-loss",
               "subject": "MATH", "count": 100, "exam_filter": null,
               "year_range": [2019, 2025], "path": "database/mocks/packs/concept/….json" } ] }

// packs/<kind>/<id>.json
{ "kind": "concept", "id": "…", "subject": "MATH", "count": 100,
  "exam_filter": null, "year_range": [2019, 2025], "questions": ["qid", …] }
```

---

## 4. Derived quantities

### Importance score

```text
recency  = Σ W(year)                 W = {0:1.00, 1:0.75, 2:0.50}, else 0.30   (gap = 2025 - year)
delta_2y = count(2024,2025) - count(2022,2023)
trend    = clamp(delta_2y / max(1, count(2022,2023)), -1, 1)

importance = 100 * ( 0.50 * recency/max_recency
                   + 0.35 * frequency/max_frequency
                   + 0.15 * (trend + 1)/2 )
```

### Vocab bucket metrics

```text
asMain          the entry was asked about
asOption        the entry appeared among another question's options
asOptionCorrect the entry was that question's answer
importance      = 3 * asMain + asOption            (ties broken alphabetically)
```

### Grammar match score

```text
score = 3 * |prompt_terms ∩ title_terms|
      + 2 * |prompt_terms ∩ topic_terms|
      + 4  if a curated type hint matches the rule title
prompt_terms = content words of concept + tags + prompt + options (stopwords removed)
```

---

## 5. Source data (read-only)

| Field | Note |
|---|---|
| `n` | position **within the section**, restarts at 1 — never used as a paper position |
| `options` | list of `{label,text}` in every paper in this corpus; the dict form `{"1": {…}}` is supported by `sections.iter_options` |
| `[IMAGE: url]` | preserved verbatim in `question`/`solution`; urls are also collected into `images` |
| `confidence` | `high` (166,839) or `unidentified` (8,051) |
| `tags` | absent on 8,207 questions → treated as `[]` |
| `solution` | absent on 124 questions → treated as `""` |
