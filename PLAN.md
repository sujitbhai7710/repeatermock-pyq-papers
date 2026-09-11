# PLAN — SSC PYQ Agent

Status of the build, in the order it was executed, with the evidence behind each
decision. Everything below was verified against the real corpus.

---

## Phase A — data reconnaissance (done)

| Finding | Evidence |
|---|---|
| 1,609 paper JSON files; keys `test_id…questions[]` | `SSC-*/**/*.json` walk |
| Year rule "first `20\d\d` in the deepest matching folder segment, else title" reproduces the target corpus exactly | **1,322 papers / 142,090 questions** in scope, 287 dropped |
| `n` restarts at 1 per section → `n` is *not* a paper position | CGL 2024 T1 `n` = `1..25,1..25,1..25,1..25`; MTS 2024 `n` = `1..38,3..39,3..49,1..36` |
| Section structure is exactly recoverable from `n` resets | section sizes per exam/year are stable (see `docs/DATA_MODEL.md`) |
| Real section orders differ from the brief for CGL T2-I, CHSL, MTS, GD | measured over all 1,322 papers → `config/exams.json` provenance block |
| MTS 2023+ papers have 90 questions with sizes 20/20/25/25 | 165 papers |
| GD 2019/2021 papers have no Hindi section | 89 papers, four 25-question sections |
| Hindi = 3,456 questions (prompt+options) | GD Hindi section = 3,420; the `।।` danda in *solutions* would add 13,157 false positives |
| `options` occurs as a list in every paper (dict form still supported) | 174,890 / 174,890 |
| 1,402 distinct concepts, 935 distinct tags (2019–2025) | label inventory |

## Phase B — core extraction (done)

1. `keywords.py` — curated ordered rule table with explicit conflict anchors.
2. `sections.py` — ordinal map, `n`-reset split, signature validation, detected
   layout with decisive-majority acceptance.
3. `taxonomy.py` — MD parsers (30 maths chapters, 59 reasoning sections + 18 meta
   sections + 5 families, 106 GK domains + 9 rule sections, 129 English rules),
   alias map (curated + taxonomy + observed), supplementary chapters.
4. `classify.py` — 5-step ladder ending in `unmapped`, never inventing a chapter.

## Phase C — database + analyses (done)

`indexer.py` (index + coverage identity) → `distribution.py` (deltas,
importance, priorities) → `build_db.py` (tree + `_meta`) → `vocab.py` (6 ranked
tables) → `grammar.py` (129-rule mapping) → `mockdata.py` (7 pack kinds).

## Phase D — AI layer (done, not exercised: no keys in the build environment)

`llm.py` (urllib client, masked keys, rate-limit detection) → `router.py`
(provider order + GLOBAL HALT) → `debate.py` (propose/counter/one rebuttal/final
+ provenance + `disputes.jsonl`) → `verify.py` (batched, resumable, idempotent)
→ `websearch.py` (Monid TinyFish + `webcache.json`).

## Phase E — operations (done)

`checkpoint.py` / `tracking.py` (atomic state, work window, journal, manifest,
`PROGRESS.md`), `cli.py`, `.github/workflows/pyq-agent.yml`, `tools/`,
`tests/`, docs.

---

## Measured results (this build)

```text
papers in scope                      1,322
questions in scope                 142,090
papers validated (section-level)     1,318   (99.7%)      [revision 2; was 1,293]
papers flagged NEEDS_AI_REVIEW           4   (0.3%)       [revision 2; was 29]
papers placeable                     1,322   (100%)

index records (non-Hindi)           138,634
placed                             130,051                 [revision 2; was 130,048]
unclassified                         8,583                 [revision 2; was 8,586]
skipped_hindi                        3,456
flagged_papers_questions                 0
--------------------------------------------
placed + skipped_hindi + unclassified + flagged_papers_questions
  = 130,051 + 3,456 + 8,583 + 0 = 142,090  == in-scope questions
```

### Why papers were flagged before revision 2, and what happened to them

| Group | Papers | Outcome |
|---|---|---|
| Section-level check passes but the question-level one did not (noisy individual labels) | 25 | after revision 2 these are **validated**: the modal subject of every declared section matches the layout, only the per-question 95% threshold was missed (e.g. a maths concept label sitting in a reasoning slot) |
| Detected layout genuinely **different** from the declared table | 1 | CGL Tier-II “other” *Finance & Accounting (2020)*: detected GK, declared MATH — still flagged |
| No usable label anywhere (`section_comparable = 0`) | 3 | 2× CGL Tier-II AAO/JSO 2023 + 1× MTS 2021: every label is `Unidentified`, so the declared layout is kept and marked `canonical-unverified` |

### `unclassified` composition

```text
8,583 = 8,033 questions whose source label is literally "Unidentified"
      +   550 questions over 200 distinct niche labels (class_source = alias,
        i.e. a canonical concept was resolved but no chapter exists)
```

(Revision 2 moved 4 GK questions from `unclassified` to `placed` — the
comma-joined label *Alcohols, Phenols And Ethers* now resolves through its first
component — and 1 English question from `placed` to `unclassified` — the maths
label *Time & Work* is no longer filed under an English grammar rule.)

They are still stored under
`database/<subject>/_unclassified/<concept>/{index.md,questions.jsonl}` so the
long tail stays browsable.

### Other phase counters

```text
vocabulary items        10,592   synonym 2,083 · antonym 3,256 · OWS 1,529 · idiom 2,678
                                 spelling 907 · homonym 139
grammar questions       10,393   mapped 6,480 (62.4%) · unmapped 3,913 · 98/129 rules hit
mock packs               1,176   34,227 question slots across 7 kinds
database tree            1,001 directories / 3,024 files (was 1,704 / 4,775 before revision 2)
```

The mock-pack drop is intended: ~500 of the old packs were built for the
synthetic comma-joined concepts (`circles-chords-and-tangents`) that revision 2
removed in favour of the resolved components (`Circles`).

---

## Remaining work / known gaps

1. **AI verification has never been executed** — no API keys were present in the
   build environment. The code paths are wired and default to
   `status=skipped_no_keys`; run `python -m agent.cli verify-db` with keys set to
   exercise the debate, corrections and `state/disputes.jsonl`.
2. **4 papers still need review.** They are flagged, their subjects are derived
   from their own section structure (recorded in
   `database/_meta/flagged_papers.jsonl`), and `verify-db` is the intended tool
   to confirm them.
3. **Grammar rule coverage is 62.4%.** 6,480 of 10,393 grammar questions match a
   rule (98 of the 129 rules are hit); the rest are written to `unmapped.jsonl`
   rather than guessed. Improving this needs either more rule keywords or the AI
   verifier.
4. **`websearch.py` is unused by the pipeline.** It is a ready client with a
   cache, intended for the AI verification step to look up obscure facts.
5. **The `../DELIVERY/SSC-PYQ-AGENT-PLAN*.md` files could not be copied** —
   no `DELIVERY/` directory exists anywhere in this workspace. This file and
   `docs/` are the authored substitute.
6. **Repo size.** Generated output measured `state/` ≈ 105 MB (dominated by the
   138,634-record `questions_index.jsonl` ≈ 100 MB — now sharded into
   `state/index/<subject>.<n>.jsonl`, revision 3) and `database/` ≈ 95 MB
   (3,024 files: 840 `questions.jsonl` pointer files ≈ 69 MB, 1,176 mock packs
   ≈ 1.6 MB, the rest `index.md`/`_meta` documents ≈ 24 MB). Revision 2 removed
   1,751 files and 703 directories (the collapsed levels and the synthetic
   multi-label concepts, plus the stale mock packs of the previous revision). That is
   what the CI publishes on the `pyq-db` branch. Two deliberate choices keep it
   down: index records carry only `has_image` (not the image urls, which
   `tools/resolve.py` re-reads) and the database stores pointer records rather
   than question text.
7. ~~**Tree redundancy is intentional.**~~ **Resolved in revision 2** — see
   below: every level whose name equals its parent's is now collapsed, so no path
   contains `a/a/`, and the English analyses moved to `english/_analysis/`.

---

## Revision 2 — post-review fixes

An independent inspection of `database/` found five structural defects. All were
fixed and re-verified; the evidence is in `README.md` §2.3/2.5 and in the command
logs.

| # | Defect | Fix |
|---|---|---|
| F1 | 167 directories repeated their parent's name (`english/grammar/grammar`, `computer/computer-fundamentals/computer-fundamentals`, …) | `build_db.leaf_levels` merges consecutive equal levels; the writer emits one `index.md` + one `questions.jsonl` per leaf concept, prunes the subject subtree before rewriting it, and the English analyses moved to the reserved `english/_analysis/` so `english/grammar` can be a leaf |
| F2 | English grammar rules carried other subjects' vocabulary (`english/at-on-and-in-as-prepositions-of-time/time-and-work` — the maths chapter *Time & Work*) | cross-subject labels may only match a local chapter by exact/containment (never the token-overlap fallback), and in English a concept owned only by another subject is dropped: 4 leaves removed, 1 question moved to `unclassified` |
| F3 | 536 comma-joined concepts became synthetic slugs (`circles-chords-and-tangents`) | `build_alias_map` resolves the whole string first, then the first resolvable comma component, and never concatenates: of the 536 labels **1 resolves whole / 502 through a component / 33 to nothing**; 1,207 records changed chapter/topic/concept as a result |
| F4 | 29 papers flagged `NEEDS_AI_REVIEW` while 25 of them had a detected layout identical to the declared one | the **section-level agreement** (modal keyword subject per declared section) is now the primary verdict, the per-question metric stays secondary: **1,318 validated / 4 flagged** |
| F5 | no structural gate | `tools/audit_db.py` + `python -m agent.cli audit` enforce five rules (duplicated nesting, stray chapter `index.md`, English cross-subject vocabulary, empty/placeholder slugs, a question filed in two leaves) and exit non-zero on any violation; wired into CI |

Verified after the fixes: `taxonomy` → `phase0` → `run --fresh --no-ai` → `audit`
all exit 0, the coverage identity balances at 142,090, `audit` reports
`VIOLATIONS: 0`, `database/` holds 1,001 directories (0 with duplicated nesting,
0 English leaves with foreign vocabulary), the 66 unit tests pass, and a full
rerun reproduces every file byte-for-byte except the timestamped
`database/_meta/PROGRESS.md`.


---

## Revision 3 — second quality + CI-readiness pass

Four open database defects (A1–A4) and three CI blockers (B1–B3) from the second
independent audit. All fixed and re-verified on a fresh full run.

| # | Defect | Fix | Evidence |
|---|---|---|---|
| A1 | §39 *Scientists, Inventions & Discoveries* owned the science concepts (`Physics`, `Chemistry`, `Biology`, `Genetics`), so **3,201** GK questions were filed there instead of under the PHYSICS / CHEMISTRY / BIOLOGY chapters | `taxonomy.relocate_cross_chapter_concepts`: a concept bullet that uniquely names another chapter (full name, short name after the em dash, or topic/subtopic) is attached to the owning chapter; the move is recorded in `taxonomy.json` under `relocations` (GK 65 moves, REAS 7) | §39 `3201 → 32`; PHYSICS `181 → 754`, CHEMISTRY `115 → 1161`, BIOLOGY `182 → 1697`; genuine scientist/invention/discovery questions (Telephone, Penicillin, Scientist, Scientists) stay in §39 |
| A2 | two labels differing only in letter case produced two leaves/packs that slugified identically; the second write silently overwrote the first (3 colliding mock pack ids, **112 question slots lost**), plus a supplementary `Static GK` chapter duplicated the markdown `STATIC GK` | `ConceptScope`/`build_label_index` canonicalise labels case- and diacritic-insensitively (taxonomy spelling first), the supplementary merge folds a case-duplicate chapter into the markdown one, mock pack keys are folded and colliding ids get a stable digest suffix | 3 pack collisions → **0** (1,170 packs = 1,170 files on disk); the two case-only leaves merged; `Static GK` + `STATIC GK` are one chapter (2,685 questions) |
| A3 | vocabulary tables counted `Abandon`/`abandon`/`ABANDON` and `To spill the beans`/`spill the beans` as separate rows | `vocab.entry_key` folds case + diacritics + whitespace and strips a leading `to ` for idioms; the first spelling stays as the display form, all spellings are listed in `variants` | synonym `6733 → 5415`, antonym `8877 → 7070`, ows `5637 → 5395`, idioms `12089 → 11530` (spelling `3496 → 3359`, homonyms `497 → 482`); `sum(variants)` reproduces the old row counts exactly, so no entry was lost, and `sum(asMain) == questions of that kind` |
| A4 | a leaf's chapter could contradict the concept's taxonomy parent (`Mensuration` under the maths chapter *Ratio* because ``mensu·ration`` contains ``ratio``) | word-boundary matching for chapter/topic containment and for the keyword ladder; the classifier re-points an ambiguous-free concept to its owner chapter; undeclared concepts are written under the explicit `_other` level (new audit rule 6); new audit rule 7 for case collisions/overwrites | new-rule violations before `rule 6: 391 leaves / 15,944 records`, `rule 7: 2 leaves + 3 pack collisions` → after `0 / 0`; 12,800 records in 378 `_other` leaves; 31 records that were placed by a substring coincidence moved to `unclassified` |
| B1 | `state/questions_index.jsonl` was **100.6 MB** — GitHub rejects files > 100 MB, so the checkpoint push could not work | `indexer.write_index` shards by subject into `state/index/<subject>.<n>.jsonl` (≤ 25 MB) + `manifest.json` (per-shard count/bytes/sha256 + `qid_sha256`), `read_index()` follows shards/legacy/gzip transparently, the legacy file is deleted on write, optional `state/index/ALL.jsonl.gz` (`PYQ_INDEX_GZIP=1`) | 8 shards, largest **26,214,277 B (25.0 MB)**, `sha256(sorted qids)` **identical before and after** (`852558b4…f921f6b1`), 138,634 records; `Get-ChildItem -Recurse -File \| where Length -gt 40MB` is empty |
| B2 | `state/` + `database/` were versioned on `main` | `main` is code only (`/state/`, `/database/` ignored), the workflow publishes the tree to `pyq-db` with `git add -f state database`, `.gitattributes` marks both as generated/vendored | `git status --porcelain` lists no generated and no raw-data file |
| B3 | the workflow could not run/needed keys | browser `User-Agent` on every worker call (`llm.py`, `websearch.py`, `PYQ_USER_AGENT` override), secrets from `secrets.*`, `status=skipped_no_keys` dry run, `PROGRESS.md` in the job summary, artifact upload, `.tmp-*.part` cleanup, idempotent `pyq-db` push | workflow YAML + every `run:` block syntax-checked; the commit step simulated against a local bare remote (branch created on run 1, "nothing to publish" on run 2, fast-forward push on run 3, no `.part` committed) and the dry run simulated (`keys=false`, exit 0, `status=skipped_no_keys`) |

**Numbers that changed.** The coverage identity still balances at 142,090 but the
placed/unclassified split moved by the A4 substring fix:
`placed 130,051 → 130,020`, `unclassified 8,583 → 8,614`,
`skipped_hindi 3,456 → 3,456`, `flagged_papers_questions 0 → 0`
(31 records whose only "evidence" was a substring coincidence inside a word —
*Partnership Accounts*→ART & CULTURE via ``p·art·nership``, *Operations
Research*→Ratio via ``ope·ratio·n``, *Passage Based Questions*→AGE-BASED
REASONING via ``p·ass·age`` — are now honestly unclassified instead of
confidently wrong).

**Verification run.** `taxonomy` → `run --fresh --no-ai` → `audit` all exit 0;
`audit` reports `VIOLATIONS: 0` with `rules 1-7` and 138,634 pointer records in
1,080 directories / 3,101 files; the 97 unit tests pass; a second full run
changes only the five run-bookkeeping files (`PROGRESS.md`, `checkpoint.json`,
`journal.jsonl`, `manifest.json`, `progress.json`) — every index shard and every
database file is byte-identical.
