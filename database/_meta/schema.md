# Generated data schema

## `state/`

| File | Contents |
|---|---|
| taxonomy.json | subject -> chapter/family -> topic -> concept parsed from `chapter-and-topic/*.md` |
| alias_map.json | normalised raw concept/tag string -> canonical concept (+chapter/topic/subject/source) |
| subject_keywords.json | the compiled keyword table used for the paper-signature check |
| papers.json | one record per in-scope paper + its layout analysis (canonical, detected, signature) |
| index/<subject>.<n>.jsonl | one record per in-scope non-Hindi question (see below), sharded by subject; no shard exceeds 25 MB because GitHub rejects files > 100 MB |
| index/manifest.json | shard list with per-shard record count, bytes and sha256 + the qid-list sha256 |
| index/ALL.jsonl.gz | optional single-file gzip copy (`PYQ_INDEX_GZIP=1`) |
| questions_index.jsonl | deprecated single-file index; read as a fallback when no manifest exists |
| distribution.json | phase-0 distribution, deltas, importance, priority lists |
| progress.json | per-phase status/counters |
| checkpoint.json | resume point + run policy snapshot |
| journal.jsonl | append-only run journal |
| manifest.json | files produced per phase with sizes and sha256 |
| disputes.jsonl | items the two-model debate could not settle |
| webcache.json | Monid TinyFish search/fetch cache |
| verify_state.json | resumable AI-verification cursor |

## `state/index/` (sharded question index)

| Field | Type | Meaning |
|---|---|---|
| qid | str | source question id |
| paper_path | str | repo-relative path of the source paper JSON |
| test_id | str | source paper id |
| exam | str | CGL / CHSL / CPO / MTS / GD / SELECTION_POST / STENO |
| year | int | year from the deepest folder segment matching 20xx, else from the title |
| shift | int|null | shift parsed from the title |
| ordinal | int | global position in the paper (index + 1) |
| n | int | source position inside its section (restarts at 1 per section) |
| subject | str | REAS / GK / MATH / ENG / COMPUTER |
| subject_source | str | canonical | detected | unvalidated |
| signature_ok | bool | paper passed the 95% positional/keyword check |
| lang | str | always `en` (Hindi questions are excluded from the index) |
| concept_raw | str | source `concept` string verbatim |
| concept | str|null | canonical concept from the alias map / fallback ladder |
| chapter | str|null | taxonomy chapter (null -> unclassified) |
| topic | str|null | taxonomy topic |
| leaf_bucket | str|null | `_other` when the chapter does not declare the concept (the leaf gets an explicit `_other` level); `null` when the chapter declares it |
| class_source | str | alias | chapter | topic | keyword | unmapped |
| has_image | bool | an `[IMAGE: url]` marker exists in prompt or solution; the urls are re-read from the source paper by tools/resolve.py |
| marks_pos / marks_neg | float | source marking scheme |
| status | str | placed | unclassified |

Sharding is deterministic: records keep their order, are grouped by subject and a shard is closed before it would exceed 25 MB, so re-running the phase produces byte-identical shards. `state/index/manifest.json` records each shard's path, record count, size and sha256 plus a sha256 over the sorted `qid` list, which is the index's content identity. `agent.indexer.read_index()` reads shards, legacy file or gzip copy transparently.

## `database/`

| Path | Contents |
|---|---|
| _meta/distribution.md | phase-0 distribution and priority lists |
| _meta/coverage.md | coverage identity, signature validation, flagged papers |
| _meta/schema.md | this file |
| _meta/PROGRESS.md | phase status, counters, artifacts |
| _meta/papers.jsonl | one row per in-scope paper |
| _meta/flagged_papers.jsonl | one row per NEEDS_AI_REVIEW paper |
| <subject>/index.md | subject roll-up |
| <subject>/<chapter>/index.md | chapter roll-up with topic children |
| <subject>/<chapter>/<topic>/index.md | topic roll-up with concept children |
| <subject>/<chapter>/<topic>/<concept>/index.md | concept page: counts by exam/year + question ids |
| <subject>/<chapter>/_other/<concept>/ | placed question whose concept the chapter does not declare |
| <subject>/<chapter>/<topic>/<concept>/questions.jsonl | compact pointer records (see POINTER_FIELDS) |
| <subject>/_unclassified/** | questions whose concept is not mappable |
| english/_analysis/vocabulary/*.md, all.json | synonym/antonym/OWS/idiom/spelling/homonym rankings (derived view) |
| english/_analysis/grammar/*.md, *.json, *.jsonl | grammar questions mapped to the 129 rules (derived view) |
| english/solved-items.json | phase-1 summary: vocabulary + grammar counters and file lists |
| <subject>/analysis.json | per-subject chapter/topic/concept counters |
| mocks/index.json | mock pack catalogue |
| mocks/packs/<kind>/<id>.json | mock pack payloads (`{kind,id,subject,count,exam_filter,year_range,questions}`) |

**Tree levels.** A directory level is never emitted when its name equals its parent's (`Vocabulary > Vocabulary`, `Antonym` under topic `Antonym`, `_unclassified` under `_unclassified` all collapse to a single directory), so no path contains `a/a/`. A collapsed node can hold questions *and* children (`english/grammar`); it still owns exactly one `index.md` and one `questions.jsonl`. The chapter/topic/concept triple stays on every pointer record. `_analysis` and `_meta` are reserved and never question leaves. Records flagged `leaf_bucket=_other` (the chapter does not declare the concept) get an explicit `_other` level between chapter and concept, so a leaf never contradicts the concept's taxonomy parent. `python -m agent.cli audit` re-checks all of this.

## Pointer record (`questions.jsonl`)

```json
"qid", "n", "ordinal", "exam", "year", "shift", "subject", "chapter", "topic", "concept", "concept_raw", "class_source", "class_confidence", "confidence", "leaf_bucket", "has_image", "marks_pos", "marks_neg", "paper_path"
```

Question text, options, solution and image markers are **not** duplicated into the database. `tools/resolve.py <qid>` re-reads the source paper and prints the full record.
