# SSC PYQ Agent — previous-year-question database builder

Turns the raw SSC previous-year question papers in this repository into a
classified, validated, mock-generator-ready database.

* **Input**: 1,609 paper JSON files (`SSC-*/**/*.json`) plus four canonical
  taxonomy markdown files in `chapter-and-topic/`.
* **In scope**: exam year **2019–2025** → **1,322 papers / 142,090 questions**.
* **Output**: `state/` (machine state, checkpoints, index) and `database/`
  (browsable question tree, distribution, English vocabulary/grammar analyses,
  mock packs).
* **Runtime**: Python standard library only — no third-party packages are needed
  to run the pipeline, the tests or the tools.

---

## 1. What it does

| Step | Command | Result |
|---|---|---|
| Parse taxonomies | `python -m agent.cli taxonomy` | `state/taxonomy.json`, `state/alias_map.json` |
| Phase 0 — distribution | `python -m agent.cli phase0` | `state/index/<subject>.<n>.jsonl` + `state/index/manifest.json`, `state/distribution.json`, `database/_meta/*` |
| Phases 1–5 — subjects | `python -m agent.cli run` | subject trees, vocabulary + grammar analyses, mock packs |
| Audit | `python -m agent.cli audit` | structural self-check of `database/` (non-zero exit on any defect) |
| Verify | `python -m agent.cli verify-db` | AI re-check of every python-extracted item |
| Stats | `python -m agent.cli stats` | per-exam / per-subject counts, top concepts |
| Mocks | `python -m agent.cli mocks` | `database/mocks/index.json` + packs |

The pipeline is **deterministic** (identical input → byte-identical output) and
**idempotent** (re-running a phase rewrites the same files; the only file that
carries a timestamp is `database/_meta/PROGRESS.md`). `python -m agent.cli audit`
is the structural gate: it must exit 0 before the database is published.

---

## 2. Data scope and the rules that decide it

### 2.1 Year filter

The year of a paper is the **first `20\d\d` match in the deepest folder segment
that contains one**; if no folder matches, the first `20\d\d` in `title` is used.

```
SSC-CGL/Previous_Year_Paper_Tier_I/2019_-_2020/…          -> 2019  (in scope)
SSC-GD/Previous_Year_Papers/2022-23/…                     -> title (2022)
SSC-CGL/Previous_Year_Paper_Tier_II/English_2016_-_2022/… -> 2016  (out of scope)
```

This rule reproduces the expected corpus exactly: **1,322 papers / 142,090
questions** in 2019–2025; 287 papers are dropped (2016–2018 and 2026).

### 2.2 Positions vs. section structure

The source `n` field **restarts at 1 for every section**, so it is *not* a paper
position. Section identity comes from the **order of the `questions[]` array**
(global ordinal = `index + 1`), and sections are detected structurally at the
points where `n` does not increase.

```
SSC CGL 2024 Tier-I, n sequence: 1..25, 1..25, 1..25, 1..25
→ sections (25,25,25,25) = REAS, GK, MATH, ENG
```

Section sizes and orders were measured across all 1,322 papers. Where the
task-specified table disagreed with the corpus, the **corpus value is used and
the difference is documented** (`config/exams.json` → `provenance`, and
`database/_meta/coverage.md`):

| Exam | Specified | Measured |
|---|---|---|
| CGL Tier-II Paper-I | 30/30/30/30/30 | **30/30/45/25/20** = MATH, REAS, ENG, GK, COMPUTER |
| CHSL Tier-I | ENG, REAS, MATH, GK | same for 2019-21 / 2024-25; **REAS, GK, MATH, ENG** for the 2022-23 and 2023 papers |
| MTS | ENG, REAS, MATH, GK | 2019/2021 = ENG,REAS,MATH,GK; 2022 = REAS,MATH,ENG,GK; **2023+ (90 q) = MATH,REAS,GK,ENG (20/20/25/25)** |
| GD | REAS,GK,MATH,ENG,HINDI | 2019/2021 have four 25-question sections and **no Hindi section** |

### 2.3 Signature validation (never silently mis-assign)

Every paper gets two independent subject signals:

1. **positional** – the layout table applied to the global ordinal;
2. **keyword** – `agent/keywords.py` applied to `concept`/`tags`.

The verdict is taken at **section level first** (`validate_sections`): for every
positional section the *modal* keyword subject of its questions is compared with
the declared subject. A section is the unit the layout table describes, so one or
two noisy source labels inside a section cannot flip the verdict.

```text
section_agreement = (sections whose modal subject == declared subject) / (sections with a usable modal)
question_agreement = (positional == keyword) / comparable      # secondary, reported for every paper
```

* `section_agreement >= signature.min_section_agreement` (default 0.95, i.e. every
  section must agree) → the paper is **validated**, `subject_source=canonical`.
* otherwise → the paper is flagged **`NEEDS_AI_REVIEW`** and its layout is
  **re-derived** from its own `n` resets plus per-section keyword majorities. The
  detected layout is stored in `state/papers.json` and
  `database/_meta/flagged_papers.jsonl`; questions placed this way carry
  `subject_source=detected`. Nothing is changed silently.
* when the paper has no declared section at all (CGL Tier-II “other”), the
  question-level metric is used as the fallback verdict.
* if no confident layout can be derived either, the paper's questions are **not
  placed** and are reported as `flagged_papers_questions`.

Post-review the corpus validates **1,318 / 1,322 (99.7%)** and flags **4**: the
CGL 2020 Finance & Accounting paper (detected subject GK, declared MATH) and
three papers whose source labels are all `Unidentified` (2× CGL Tier-II
AAO/JSO 2023, 1× MTS 2021) — those carry no evidence either way and keep the
declared layout, marked `canonical-unverified`.

Every in-scope question ends up in exactly one of four buckets:

```text
placed + skipped_hindi + unclassified + flagged_papers_questions == 142,090
```

### 2.4 Hindi

A question is `lang=hi` when its **prompt or any of its options** matches
`[\u0900-\u097F]`. Hindi questions are counted, put in the skip bucket, kept out
of every subject database and **never** sent to an LLM. This is the whole GD
Hindi section (positions 81–100, 3,420 questions) plus 36 stray Hindi items.

The solution field is deliberately **not** scanned: geometry solutions in this
corpus use the Devanagari **danda `।।`** as a "parallel" symbol, which the raw
range matches. `database/_meta/coverage.md` reports all four detector variants
(16,613 including the solution; 3,456 for prompt+options; 3,431 without the
danda) so the choice is auditable. Change it via
`config/settings.json → hindi.include_solution` / `hindi.exclude_punctuation`.

### 2.5 Labels → concept → chapter/topic

`state/alias_map.json` maps every raw `concept`/`tag` string to a canonical
concept. Resolution order per label:

1. curated alias table (560 entries);
2. exact taxonomy vocabulary match — **the whole string first**, so genuine
   comma-bearing names such as *Acids, Bases & Salts* are never split;
3. multi-label strings only: split on `,` and take the first component that maps
   to a canonical taxonomy name (*Circles, Chords and Tangents* → *Circles* →
   `maths/geometry/circle/circles`). A concept is **never** built by
   concatenating the raw multi-label string — that used to put synthetic slugs
   such as `circles-chords-and-tangents` into the tree;
4. the label itself, marked `observed`.

Of the 536 distinct comma-bearing concept labels in the corpus, **1 resolves as a
whole / 502 are resolved through a component / 33 resolve to nothing** (71
questions; they fall through to the subject keyword ladder instead of creating a
concatenated slug). The per-label outcome is stored on every alias entry as
`multi: whole | split | unresolved` and counted in `stats.multi_*`.

Two guards keep foreign vocabulary out of the tree:

* a label that resolves to **another subject's** chapter/topic may still be
  matched locally by exact/containment matching (*Circles* → the maths topic
  *Circle*), but never through the token-overlap fallback — that rule used to
  file the maths label *Time & Work* under the English grammar rule *At, On, and
  In as Prepositions of Time*;
* in the subjects listed in `classify.LEAF_HYGIENE_SUBJECTS` (English) a concept
  that only another subject's taxonomy owns is dropped, so no English leaf is
  ever named after maths/GK/reasoning vocabulary (previously
  `english/…/time-and-work`, `english/_unclassified/literature`, …).

A label that cannot form a directory name at all (no ASCII slug, e.g. a stray
Devanagari concept on a question the prompt-level Hindi detector did not catch)
is dropped the same way; the raw label stays in `concept_raw` on the pointer
record. `python -m agent.cli audit` enforces all of this.

Three rules keep the leaf taxonomy honest (audit rules 6 and 7):

* **declared scope.** A leaf's concept must be declared by its chapter — the
  chapter's own name, its short name after the em dash (`GENERAL SCIENCE —
  PHYSICS` → *Physics*), one of its topics/subtopics/concepts, or a concept whose
  tokens are all part of the chapter name (*Mensuration* under *Mensuration 2D &
  3D*). A placed question whose concept the chapter does not declare is written
  under the explicit `_other` level (`maths/time-and-distance/_other/…`) instead
  of pretending the chapter declares it;
* **parent consistency.** When a resolved concept is vocabulary of exactly one
  other chapter of the subject, the leaf is re-pointed to that chapter (and its
  topic). Substring collisions are excluded: matching is word-boundary aware, so
  the maths concept *Mensuration* is no longer filed under the chapter *Ratio*
  (``mensu·ration`` used to match ``ratio``) and the keyword ladder no longer
  reads *Operations Research* as *Ratio*;
* **case stability.** Labels are canonicalised case- and diacritic-insensitively
  (first spelling of the taxonomy/alias map wins), so `World Geography` and
  `WORLD GEOGRAPHY` — or `Static GK` and `STATIC GK` — are one leaf and one mock
  pack instead of two whose second write silently dropped the first.

The GK parser additionally relocates concept bullets that *name* another chapter:
§39 *Scientists, Inventions & Discoveries* lists the research fields
(`Physics`, `Chemistry`, `Biology`, `Genetics`, …) among its bullets, and those
used to become §39 concepts, which filed ~3.1k general-science questions under
*Scientists, Inventions & Discoveries* instead of under the PHYSICS / CHEMISTRY /
BIOLOGY chapters. Recorded in `state/taxonomy.json` under `relocations`.

### 2.8 The question index is sharded

`state/index/<subject>.<n>.jsonl` (≤ 25 MB per shard) plus
`state/index/manifest.json` (shard list with record count, size and sha256, and
a sha256 over the sorted `qid` list as the content identity). The single-file
`state/questions_index.jsonl` was ~100 MB, and GitHub rejects any file over
100 MB, so a checkpoint push would have failed. `agent.indexer.read_index()`
reads the shards, the legacy single file or the optional `state/index/ALL.jsonl.gz`
(`PYQ_INDEX_GZIP=1`) transparently, so `build_db`, the phases, `tools/resolve.py`
and `mockdata` are unchanged.

---

## 3. Architecture

```text
repeatermock-pyq-papers/
├── agent/                     python package (stdlib only)
│   ├── paths.py               project-root auto-detection
│   ├── config.py              settings.json + exams.json (year-aware layouts)
│   ├── keywords.py            curated subject keyword table
│   ├── taxonomy.py            MD -> taxonomy.json + alias_map.json + resolver
│   ├── discover.py            paper walk, year filter, normalised records
│   ├── sections.py            section split, positional map, signature, Hindi
│   ├── classify.py            concept/tags -> concept -> chapter/topic
│   ├── indexer.py             sharded state/index/** + coverage identity
│   ├── distribution.py        phase-0 distribution, deltas, importance
│   ├── build_db.py            database/** writer (collapsing tree + _meta)
│   ├── vocab.py               synonym/antonym/OWS/idiom/spelling/homonym
│   ├── grammar.py             grammar question -> one of 129 rules
│   ├── mockdata.py            mock pack catalogue
│   ├── llm.py / router.py     OpenAI-compatible client + GLOBAL HALT
│   ├── debate.py              DeepSeek proposes, GPT-5.6 Sol judges
│   ├── verify.py              resumable AI re-check of extracted items
│   ├── websearch.py           Monid TinyFish search/fetch + cache
│   ├── checkpoint.py          atomic state, work window, resume
│   ├── tracking.py            progress.json, journal, manifest, PROGRESS.md
│   └── phases/                phase0 … phase5
├── config/                    settings.json, exams.json, supplementary_taxonomy.json
├── chapter-and-topic/         canonical taxonomy markdown (READ ONLY)
├── SSC-*/**/*.json            source papers (READ ONLY)
├── tools/                     resolve.py, mocks.py, audit_db.py
├── tests/                     unittest suite for the pure functions
├── docs/                      ARCHITECTURE.md, DATA_MODEL.md, OPERATIONS.md
├── state/                     generated state
└── database/                  generated database
```

`docs/ARCHITECTURE.md` has the full module/flow description,
`docs/DATA_MODEL.md` documents every generated file and field, and
`docs/OPERATIONS.md` covers running, CI, checkpoints and the rate-limit policy.

---

## 4. Running locally

```bash
# 1. taxonomies (fast; writes state/taxonomy.json + state/alias_map.json)
python -m agent.cli taxonomy

# 2. distribution + index + signature validation (~20 s on the full corpus)
python -m agent.cli phase0

# 3. inspect
python -m agent.cli stats

# 4. the whole pipeline (phases 0-5), honours the checkpoint and the work window
python -m agent.cli run

# 5. a single phase
python -m agent.cli run --phase 3      # or: python -m agent.cli phase 3

# 6. mock packs
python -m agent.cli mocks

# 7. structural gate over database/ (exit != 0 on any defect)
python -m agent.cli audit              # or: python tools/audit_db.py --json
```

The audit checks the seven invariants the tree must satisfy: no duplicated
nesting level, no chapter-level `index.md` next to a same-named child, no
another-subject vocabulary in an English leaf, no empty/placeholder slug, no
question filed in two concept leaves (keyed on `qid` + paper + ordinal, because
the source papers reuse a few `qid`s for different questions), no leaf whose
concept the chapter does not declare (those live under the explicit `_other`
level), and no case-only leaf collision or silently overwritten mock pack.

Tests (stdlib `unittest`, no extra dependencies):

```bash
python -m unittest discover -s tests -t .
```

Tools:

```bash
python tools/resolve.py <qid>              # full question from the source paper
python tools/resolve.py --find "spelling"  # find ids by label
python tools/mocks.py kinds
python tools/mocks.py list --kind concept --subject MATH --limit 20
python tools/mocks.py build --kind concept --id math-profit-and-loss-profit-and-loss --resolve
```

> **Interpreter note.** `python -m agent.cli` puts the *current directory* on
> `sys.path`. An embedded/isolated Python build (for example the interpreter
> bundled inside a GUI application, which enables `safe_path`) ignores that, so
> run from the repository root with a regular CPython or set `PYTHONPATH=.`.
> `tools/*.py` insert the project root themselves and work either way.

### Environment variables

| Variable | Purpose |
|---|---|
| `OPENAI_KEYS` | comma-separated key pool for the **agentrouter** provider |
| `DEEPSEEK_KEYS` | comma-separated key pool for the **jw** provider |
| `AR_PROXY_TOKEN` | single bearer token for agentrouter |
| `JW_PROXY_TOKEN` | single bearer token for jw |
| `MONID_API_KEY` | Monid TinyFish web search/fetch |
| `MAX_WORK_SECONDS` | override the work window (default `19800`) |
| `PYQ_INDEX_GZIP` | also emit the optional single-file `state/index/ALL.jsonl.gz` |
| `PYQ_USER_AGENT` | override the browser user agent used for every HTTP call |
| `CHECKPOINT_INTERVAL_SECONDS` | override the checkpoint interval (default `1800`) |
| `RATE_LIMIT_THRESHOLD` | override the consecutive-failure threshold (default `10`) |
| `PYQ_PROJECT_ROOT` | force the project root |
| `PYQ_RUN_ID` | force the run id |

Keys are read from the environment only, are never written to a file, and are
never logged (the router records at most a `...abcd` suffix).

---

## 5. The GitHub Action

`.github/workflows/pyq-agent.yml`

* triggers: `workflow_dispatch` **and** `cron: 0 */6 * * *` (every 6 hours);
* `timeout-minutes: 350`, `concurrency: {group: pyq-agent, cancel-in-progress: false}`,
  `permissions: contents: write`;
* steps: checkout (`fetch-depth: 0`) → setup-python 3.11 →
  `pip install -r requirements.txt` (a no-op: the agent is stdlib-only) →
  `python -m agent.cli run` with `MAX_WORK_SECONDS=19800` and the secrets
  `AR_PROXY_TOKEN`, `JW_PROXY_TOKEN`, `MONID_API_KEY` →
  **`python -m agent.cli audit`** (fails the job on any structural defect) →
  `database/_meta/PROGRESS.md` (plus the audit transcript) in the job summary →
  `upload-artifact` (logs, checkpoint, coverage) → commit the generated tree to
  the long-lived **`pyq-db`** checkpoint branch with `git add -f state database`
  and `git push origin HEAD:pyq-db` (creates the branch on the first run,
  fast-forwards afterwards by re-parenting onto the branch tip; `.tmp-*.part`
  leftovers are deleted first). The default branch is never modified.
* **no-op safe**: with no worker secret configured the job writes
  `status=skipped_no_keys` to the summary and exits 0 instead of calling the
  endpoints unauthenticated;
* every HTTP call uses a **browser `User-Agent`** (override with
  `PYQ_USER_AGENT`): Cloudflare answers the stock Python user agent with
  `HTTP 403 error 1010`.

Manual run options: a single phase (`phase` input) or python-only
(`no_ai=true`, no LLM calls at all). Scheduled runs pass `--fresh` (the corpus
may have changed since the last cron), while manual runs resume from the
published checkpoint unless `--fresh` is requested locally.

---

## 6. Checkpoint, resume and the work window

* Work window: `MAX_WORK_SECONDS` (default **19,800 s = 5.5 h**). When it expires
  the current phase stops and the run exits with `status=time_limit`.
* Checkpoint every `CHECKPOINT_INTERVAL_SECONDS` (default **1,800 s**):
  `state/checkpoint.json` records `{run_id, phase, cursor.completed, status}`.
* `python -m agent.cli run` **resumes**: phases already listed in
  `cursor.completed` are skipped. `--fresh` ignores the checkpoint.
* All state writes are atomic (temp file + `os.replace`), so a killed run never
  leaves a partially written JSON file.
* `state/verify_state.json` stores the verified **batch fingerprints** per
  phase, so `verify-db` resumes and is idempotent: re-applying a correction is a
  set operation on the index record.

---

## 7. The two-model debate

```text
item ──> DeepSeek V4 Flash          : structured JSON proposal
         GPT-5.6 Sol                : agree | counter
         (if counter) DeepSeek V4 Flash : one rebuttal     (max rounds = 1)
         GPT-5.6 Sol                : FINAL verdict + provenance
```

* proposer model: `deepseek-v4-flash`, critic model: `gpt-5.6-sol`;
* provider order **agentrouter ar-rotator**
  (`https://ar-rotator.opencode-5a3.workers.dev/v1`) then **jw-rotator**
  (`https://jw-rotator.opencode-5a3.workers.dev/v1`);
* every verdict carries provenance (which model said what, on which provider, in
  how many rounds) so a result is always traceable;
* unresolved items are appended to `state/disputes.jsonl`.

When no keys are configured the AI steps are skipped with an explicit note
(`skipped_no_keys`) and the python extraction is kept — an offline run still
produces a complete database.

---

## 8. Rate-limit policy (GLOBAL HALT)

* every failure increments a **consecutive-failure** counter;
* **one success resets it to zero**;
* at **10 consecutive failures** — *or immediately when any worker response
  carries a rate-limit / quota-exhaustion signal* (HTTP 402/429/503, or bodies
  containing `rate limit`, `quota`, `exhausted`, `insufficient_quota`,
  `no available`, `overloaded`, `capacity`, `concurrency`, …) — the router sets a
  **global halt**:
  1. the in-flight item is allowed to finish,
  2. no further requests are made (`GlobalHalt` is raised),
  3. the phase checkpoints and the run exits with **`status=rate_limited`**.

`status` values: `ok`, `rate_limited`, `time_limit`, `error`.

---

## 9. Output database layout

```text
database/
├── _meta/
│   ├── distribution.md          exam x year x shift x subject x chapter x topic x concept,
│   │                            last-2-year deltas, importance score, priority lists
│   ├── coverage.md              coverage identity, signature validation, flagged papers,
│   │                            Hindi detector variants
│   ├── schema.md                schema of every generated file
│   ├── PROGRESS.md              phase status, counters, artifacts
│   ├── papers.jsonl             one row per in-scope paper
│   └── flagged_papers.jsonl     one row per NEEDS_AI_REVIEW paper
├── maths/ | reasoning/ | gk/ | english/ | computer/
│   ├── index.md
│   └── <chapter>/
│       ├── index.md
│       └── [<topic>/]
│           └── <concept>/
│               ├── index.md         counts by exam/year + question ids
│               └── questions.jsonl  compact pointer records
├── english/
│   ├── _analysis/vocabulary/{synonyms,antonyms,one-word-substitution,idioms,spelling,homonyms}.md
│   ├── _analysis/vocabulary/all.json
│   ├── _analysis/grammar/{rules.md,rules.json,questions.jsonl,unmapped.jsonl}
│   └── solved-items.json
└── mocks/
    ├── index.json               catalogue (metadata per pack, no question lists)
    └── packs/<kind>/<id>.json   {kind,id,subject,count,exam_filter,year_range,questions[qids]}
```

**Tree rules.** A directory level is never emitted when its name equals its
parent's (`Vocabulary > Vocabulary`, concept `Antonym` under topic `Antonym`,
`_unclassified` under `_unclassified`), so no path contains `a/a/` and every
leaf concept owns exactly one `index.md` and one `questions.jsonl`. A collapsed
node can hold questions *and* children (`english/grammar`), and then gets a
single combined page. `english/_analysis` and `_meta` are reserved: they are the
only places where non-tree files live. The subject subtree is rebuilt from the
index on every run, so a superseded directory cannot survive as a stale shell.

Question text is **not** duplicated into the database; `questions.jsonl` holds
pointer records and `python tools/resolve.py <qid>` re-reads the source paper for
the full prompt, options, solution and image URLs.

### English vocabulary buckets

`asMain` = the word/phrase was asked about; `asOption` = it appeared among
another question's options; `asOptionCorrect` = it was the answer in that
question. A question never counts its own main term as an option (its options are
that question's *meaning options*), and duplicates inside one option set count
once. Ranking: `importance = 3*asMain + asOption`, ties broken alphabetically.

### Grammar rules

Grammar questions are mapped to one of the **129 rules** in
`english-grammar-rules.md` by question type (Error Detection, Sentence
Improvement, Shuffling of Sentence parts, Shuffling of Sentences in a passage,
Sentence Structure, Modal, Phrasal Verb) plus a weighted keyword overlap against
each rule's title and topic. `score == 0` → `unmapped.jsonl`, never a guess.

---

## 10. Generating mocks

```bash
# 1. (re)build the catalogue
python -m agent.cli mocks                    # pack size 100 (default)
python -m agent.cli mocks --pack-size 0      # include every matching question
python -m agent.cli mocks --min-size 10      # drop tiny packs

# 2. discover packs
python tools/mocks.py kinds
python tools/mocks.py list --kind chapter --subject MATH --limit 20
python tools/mocks.py list --kind concept --search profit

# 3. emit a mock
python tools/mocks.py show  --kind concept --id math-profit-and-loss-profit-and-loss
python tools/mocks.py build --kind concept --id math-profit-and-loss-profit-and-loss --resolve > mock.json
```

Pack kinds: `concept`, `topic`, `chapter`, `subject`, `exam`, `year`, `full`.
Pack ids are hierarchical slugs (e.g. `math-profit-and-loss-profit-and-loss`),
which guarantees uniqueness across the tree. Selection is deterministic:
questions are drawn round-robin across `(exam, year, shift)` buckets so a pack
spreads over the corpus instead of exhausting one paper.

---

## 11. Repository data policy

The raw data is **read-only** for this project: no `**/*.json` paper and no
`chapter-and-topic/*.md` was modified. Only new files were added (plus this
README, which replaces the original two-line one). Verify with:

```bash
git status --porcelain
```
