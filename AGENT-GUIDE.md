# AGENT-GUIDE.md — complete handover for any AI agent

> Read this file first, end to end. It tells you what this project is, what is already done,
> what is left, exactly which commands to run, and every trap we already hit.
> Companion file: **`AI-APIS.txt`** (how to call the AI providers, with copy-paste examples).

Repo: `https://github.com/sujitbhai7710/repeatermock-pyq-papers`

---

## 0. TL;DR for the agent

1. The **deterministic database is 100% done** (142,090 questions classified, published).
2. What is left is the **AI layer** (verification + grammar judging) and **notes**.
3. Work **only on a clone**, run the audit after every change, and **publish with merge, never replace**.
4. **Only one writer** may touch the `pyq-db` branch at a time.
5. You need **2 working AI models** (one proposer, one judge) — not all providers.

---

## 1. What this project is

Turns SSC previous-year question papers (already committed in the repo as JSON) into a
**topic-wise database** + trends + priority lists + mock packs, ready for mock generation.

```
Raw papers (SSC-*/*.json)  ->  classify by subject/chapter/topic/concept  ->  database/ tree  ->  mocks
                                          ^
                                          +-- AI verification / judging layer
```

### Scope (locked)

| | |
|---|---|
| Papers in scope | **2019–2025** |
| Papers | **1,322** (of 1,609 in the repo; 287 out of scope) |
| Questions | **142,090** |
| Subjects | English, GK/GS, Maths, Reasoning, **Computer** (CGL Tier-II) |
| Hindi | detected (Devanagari) and **skipped** — never sent to an AI |
| Pipeline order | **English → GK/GS → Maths → Reasoning → Computer** |

### The 4 source taxonomy files (on `main`, read-only)

| File | Contents |
|---|---|
| `chapter-and-topic/SSC_CGL_Maths_Top_Level_Taxonomy_All_Posts_Improved.md` | 30 maths chapters + AI classification rules |
| `chapter-and-topic/SSC_Reasoning_Master_Syllabus.md` | 77 reasoning sections in 5 families + decision tree |
| `chapter-and-topic/SSC_GK_GS_Master_Syllabus.md` | GK/GS domains 0–106 + classification rules |
| `chapter-and-topic/english-grammar-rules.md` | **129 numbered grammar rules** |

**Never edit these files, and never edit `SSC-*/**/*.json`.** They are the source of truth.

---

## 2. THE TWO BRANCHES — what each is for

| Branch | Holds | What you do with it |
|---|---|---|
| **`main`** | CODE: `agent/`, `tools/`, `tests/`, `config/`, `docs/`, `README.md`, `MEMORY.md`, `LESSONS.md`, `AGENT-GUIDE.md`, `AI-APIS.txt` + the raw papers and the 4 taxonomy MDs | Develop and commit **code** here. `state/` and `database/` are **gitignored** here on purpose. |
| **`pyq-db`** | GENERATED OUTPUT: `state/` (index, checkpoints, progress) + `database/` (the deliverable tree: subject leaves, grammar leaves, mocks) | This is where the **pipeline publishes**. Never develop here. |

**Why two?** The generated tree is ~100 MB and is rewritten every checkpoint. Keeping it off `main`
keeps the code history readable. A single branch is possible but every checkpoint would add a ~100 MB
snapshot to `main`.

**The hard rule:** when you publish, you **merge** into `pyq-db` — you must never delete files that are
already there. The workflow already does this; if you publish by hand, copy the existing branch
content back before committing, exactly like the workflow's "Commit the checkpoint branch" step.

**Only one writer at a time.** Of all the problems in this project, the worst was two agents
(a GitHub Action *and* a cloud agent) publishing to `pyq-db` simultaneously — each overwrote the other's
files and twice wiped the whole `database/` tree.

---

## 3. WHAT IS ALREADY DONE (verified on `pyq-db`, 2026-09-12)

### 3.1 Coverage identity — always must balance

```
placed 130,020  +  unclassified 8,614  +  skipped_hindi 3,456  +  flagged 0  =  142,090   ✅
```

Papers: 1,322 in scope · **1,318 validated** by section-signature · **4 flagged** for AI review.

### 3.2 The database tree (published on `pyq-db`)

| Path | Files | Meaning |
|---|---|---|
| `database/english/` | 461 | vocabulary, grammar (129 rules), verbal-ability + `_unclassified` |
| `database/english/grammar/` | 399 | **134 rule leaves** (one per rule + index) — each links its questions |
| `database/maths/` | 285 | 30 chapters → topics → concepts |
| `database/reasoning/` | 224 | 5 families → 77 topics |
| `database/gk/` | 1,261 | domains → subdomains (+ `notes.md` per topic **not yet generated**) |
| `database/computer/` | 35 | CGL Tier-II computer section as its own subject |
| `database/mocks/` | 1,149 | **1,148 mock packs** (by concept / topic / chapter / subject / exam / year / full) |
| `database/_meta/` | 6 | `coverage.md`, `distribution.md`, `PROGRESS.md`, `schema.md`, `papers.jsonl`, `flagged_papers.jsonl` |
| `*/\_unclassified/` | — | placeholder inside every subject for anything unclassifiable |

### 3.3 Trends, importance and priority — **already implemented**

They are **not** separate `database/trends/` folders. They live in **`database/_meta/distribution.md`**:

- *Top chapters by importance* — columns: Questions · **Last 2y** · **Prev 2y** · **Delta** · Importance
- *Top concepts by importance* — same columns
- *Priority chapters per subject* — the priority file the spec asked for

Machine-readable equivalents: `state/distribution.json`.

### 3.4 State (on `pyq-db`)

`state/index/` (9 shards), `papers.json`, `taxonomy.json`, `alias_map.json`, `distribution.json`,
`progress.json`, `checkpoint.json`, `manifest.json`, `journal.jsonl`, `labels.json` — all present.

### 3.5 Quality gates currently green

- `python -m agent.cli audit` → **VIOLATIONS: 0** (rules 1–11)
- `python -m unittest discover -s tests -t .` → **231 tests, OK**
- `python tools/cross_subject_audit.py` → `definitely wrong 0 / ambiguous 11,060 / ok 127,574`
- `python tools/audit_rule_demo.py` → `4/4 demonstration(s) failed the audit as intended`

### 3.6 Per-subject breakdown (published on `pyq-db`)

Every subject is already built. This is exactly what each one contains today:

| Subject | Taxonomy source | Top-level nodes | Leaf nodes (`index.md`) | `questions.jsonl` | Mocks | Status |
|---|---|---|---|---|---|---|
| **English** | `english-grammar-rules.md` (129 rules) | 6 (`grammar`, `vocabulary`, `verbal-ability`, `active-and-passive-voice`, `_analysis`, `_unclassified`) | **162** (of which **134 are the grammar rules**) | 157 | yes | ✅ built |
| **Maths** | `SSC_CGL_Maths_...Improved.md` (30 chapters) | **32** | **156** | 128 | yes | ✅ built |
| **Reasoning** | `SSC_Reasoning_Master_Syllabus.md` (77 sections / 5 families) | **32** | **132** | 91 | yes | ✅ built |
| **GK / GS** | `SSC_GK_GS_Master_Syllabus.md` (domains 0–106) | **49** | **701** | 559 | yes | ✅ built |
| **Computer** | derived (CGL Tier-II §Computer) | **9** (`computer-fundamentals`, `computer-hardware`, `computer-software`, `internet-and-networking`, `ms-office`, `operating-systems`, …) | **22** | 12 | yes | ✅ built |

Totals: **947 leaf nodes** (`questions.jsonl` link files), **1,148 mock packs**, **3,421 files** under `database/`.

**English inside-out** (the most customised subject):
- `english/grammar/` — **134 leaves** (all 129 rules + index + `_unclassified`); each rule links its own questions
- `english/vocabulary/` — synonym / antonym / OWS / idioms / spelling / homonyms (4-bucket counting)
- `english/verbal-ability/` — cloze test, para jumbles, reading comprehension, fill-in-the-blanks, etc.

**What is still missing per subject** (same for all five):

| Item | Applies to |
|---|---|
| AI verification of that subject's questions (`phase` step) | English (phase1) 440/37,990; GK, Maths, Reasoning, Computer not started |
| Notes — **GK/GS topic notes**, **grammar notes** | GK/GS and English |
| Anything unclassifiable | lands in `database/<subject>/_unclassified/` (already present for every subject) |

**Where the counts live:** `database/_meta/distribution.md` (per-subject trends + priority),
`database/_meta/coverage.md` (coverage identity), `state/distribution.json` (machine-readable),
`python -m agent.cli stats --top 20` (live counts).

---

## 4. WHAT IS **NOT** DONE (the actual work left)

| # | Gap | Detail |
|---|---|---|
| 1 | **AI verification** | `phase1` is at **440 / 37,990** items. The AI ran rarely because providers were unreachable from CI runners. |
| 2 | **Grammar AI verdicts** | ~140 of ~7,190 unassigned grammar questions have an AI rule verdict. `state/grammar_ai_state.json` is **not** currently on `pyq-db`. |
| 3 | **Notes** | GK/GS notes and grammar notes were specified but are **not generated**. |
| 4 | **Remaining/error datastore** | `state/remaining.json` and `state/errors.jsonl` are **not** implemented in the shipped code — so a "green" run can silently do no AI work. |
| 5 | **Parallel AI** | Batches are processed **sequentially** — the single biggest speedup available. |
| 6 | **4 flagged papers** | Need AI review (`database/_meta/flagged_papers.jsonl`). |

---

## 5. THE ORDER OF OPERATIONS (do it exactly like this)

```
0. clone the repo, install nothing (the code is stdlib-only), use python 3.11
1. python -m agent.cli taxonomy          # parse the 4 taxonomy MDs  -> state/taxonomy.json
2. python -m agent.cli phase0            # discovery + Hindi split + index + distribution
3. python -m agent.cli routes --probe    # WHICH AI PROVIDERS ARE ALIVE RIGHT NOW (do this first!)
4. python -m agent.cli grammar --ai      # grammar judging (resumable; safe to re-run)
5. python -m agent.cli run               # the AI verification pass (5.5h window, checkpoint 15min)
6. python -m agent.cli audit             # MUST print VIOLATIONS: 0
7. python -m unittest discover -s tests -t .
8. python -m agent.cli publish           # merge-publishes state/ + database/ to pyq-db
```

Steps 4 and 5 are **resumable**: every verdict is written to state as it goes, so stopping costs time,
never work. Always re-run until the counters stop moving.

---

## 6. Command reference

| Command | What it does |
|---|---|
| `python -m agent.cli taxonomy` | parse the 4 taxonomy MDs → `state/taxonomy.json`, `state/alias_map.json` |
| `python -m agent.cli phase0` | walk papers, year filter 2019–2025, Hindi split, build the sharded index + distribution |
| `python -m agent.cli routes --probe` | probe every `(provider, model)` and print the live matrix — **run this before any AI work** |
| `python -m agent.cli grammar --ai [--limit N]` | assign grammar questions to the 129 rules (deepseek proposes, gpt-5.6-sol judges) |
| `python -m agent.cli run [--no-ai] [--fresh]` | full pipeline; `--no-ai` = deterministic only |
| `python -m agent.cli verify-db --phase phase1 [--limit N]` | run the AI verification for one phase |
| `python -m agent.cli audit` | structural self-check (rules 1–11). **Must print `VIOLATIONS: 0`** |
| `python -m agent.cli errors --top 20 [--json]` | summarise the append-only AI failure ledger `state/errors.jsonl` (phase/route/kind/why) |
| `python -m agent.cli stats [--top N]` | counts per exam/subject + top concepts |
| `python -m agent.cli mocks` | rebuild the mock-pack catalogue |
| `python -m agent.cli publish` | publish `state/` + `database/` to the `pyq-db` branch (merge) |
| `python tools/cross_subject_audit.py [--verbose] [--json]` | who owns each chapter, and which leaves are named after *another* subject's vocabulary (`--fix` applies the leaf hygiene) |
| `python tools/audit_rule_demo.py` | prove rules 8–11 fail when deliberately violated (audits a copy, never the real tree) |
| `python tools/grammar_pattern_pass.py --apply` | deterministic TF-IDF pre-pass for the grammar rules (writes `state/grammar_ai_state.json`) |
| `python tools/gen_notes.py` | the per-topic notes generator |

---

## 7. AI USAGE — the part that caused most of the pain

**You only need TWO working models.** A debate needs one *proposer* and one *judge*; more is optional.

| Role | Preferred model |
|---|---|
| proposer | a **DeepSeek-class** model |
| judge (final say) | **`gpt-5.6-sol`** |

See **`AI-APIS.txt`** for the endpoint table, keys, headers and copy-paste request examples.

### 7.1 Rules that make AI work reliably

1. **Always send a browser `User-Agent`** to the worker endpoints, or Cloudflare answers
   `403 error code: 1010`. (Direct `agentrouter.org` instead needs `User-Agent: cline/2.0.0`.)
2. **Use the workers, not the direct upstreams**, from CI/cloud. `agentrouter.org` direct sits behind
   Aliyun WAF and returns an HTML captcha (HTTP 200 + HTML) to most datacenter IPs.
3. **Reasoning models need room.** `deepseek-v4-flash` can spend 6–8k tokens thinking; if `max_tokens`
   is small it returns `content: ""` with `finish_reason: length`. The client must escalate
   `max_tokens` (≥ 8192) and retry the *same* provider — that is not a provider failure.
4. **Fail over per `(provider, model)`**, not per provider: one worker can serve `gpt-5.6-sol` while
   503-ing for `deepseek-v4-flash`.
5. **One bad blip must not kill the run.** Stop only after **6 consecutive** failures, and always
   checkpoint first.
6. **No healthy route → finish the deterministic work, save, exit 0** with `status=ai_unavailable`.
   Never crash the run because the AI is down.
7. **Never log keys.** Never commit keys. Read them from env or the secret store.

### 7.2 Providers that did **not** work (don't waste time on them)

| Provider | Verdict |
|---|---|
| `agentrouter.org` **direct** | Aliyun WAF — HTML captcha from CI runners and from India. Use the `ar-rotator` worker instead. |
| `ar-rotator` + `gpt-5.6-sol` / `claude-opus-5` | `402 credits` on the free tier — that tier only has deepseek + glm. |
| `glm-5.3` | works but is 3–7× slower; do not use it in bulk. |
| **TokenHarbor** | ⚠️ removed — very small free quota, and its `thk_` keys are rejected by OpenHands' proxy. |
| `zen-rotator` paid models | `503 all_keys_exhausted`; only the free `muse-spark-1.2` / `ling-3.0-fin` work. |
| Any provider from a **GitHub-hosted runner** | mostly blocked; the AI silently becomes `ai_unavailable`. |

---

## 8. Failure playbook (every incident we already hit)

| Symptom | Cause | Fix |
|---|---|---|
| `403 error code: 1010` | missing browser `User-Agent` | send one on every request |
| HTML body instead of JSON | Aliyun WAF (direct agentrouter) | use the worker |
| `content: ""`, `finish_reason: length` | reasoning model burned the budget | escalate `max_tokens` ≥ 8192, retry same provider |
| Run dies at ~70 s with `GLOBAL HALT` | one dead provider halted everything | per-`(provider, model)` breakers only |
| `422 Unexpected value 'timeout-minutes'` | YAML key at top level | `timeout-minutes` must be a **job** key |
| `git push` rejected, file > 100 MB | one giant index file | index is sharded into `state/index/*.jsonl` (≤25 MB) |
| `database/` tree vanished from `pyq-db` | a checkpoint committed only the runner's tree | **merge** before committing (workflow does this now) |
| `ValueError: tuple.index(x): x not in tuple` in tests | tests pinned an old provider list | tests now use a self-contained fixture |
| `No module named 'agent'` | using an isolated Python build | use the **system** Python 3.11 and run from the repo root |
| workflow won't dispatch (`422 … disabled workflow`) | workflow was switched off | re-enable it (`Actions → pyq-agent → Enable`) |
| `LiteLLM Virtual Key expected … thk_` (OpenHands) | OpenHands proxy only accepts `sk-` keys | don't use TokenHarbor as the agent's own model |

---

## 9. Invariants — breaking any of these is a bug

1. `placed + skipped_hindi + unclassified + flagged_papers_questions == 142090`
2. `python -m agent.cli audit` → **`VIOLATIONS: 0`** (rules 1–11, see below)
3. `python -m unittest discover -s tests -t .` stays green
4. Everything is **idempotent** — re-running a phase changes nothing
5. **No raw-data edits** (`SSC-*/**/*.json`, `chapter-and-topic/*.md`)
6. **No keys** in logs or commits
7. A publish to `pyq-db` must **merge**, never delete
8. Never let a single generated file exceed ~40 MB
9. One question lives in **one** leaf (rule 5/8). A qid the *source papers reuse*
   for different questions is a **warning**, never a violation — the ids are not
   unique in the corpus.
10. An **empty** `questions.jsonl` must carry the `No PYQ in scope` marker in its
    `index.md` (rule 9), and a marker on a leaf that has questions is a violation.
11. A subject files **only chapters its taxonomy declares**, and a chapterless leaf
    is never named after vocabulary only another subject owns (rule 10) — this is
    the "cross-subject" class: `gk/_unclassified/verbal-ability` must not exist.
12. The derived `database/english/_analysis` view is **never published** to
    `pyq-db` (rule 11 + `agent.gitpush.PUBLISH_EXCLUDE_PATHS`).
13. **Chapter ownership decides placement.** A shared concept *label* is not a
    misplacement: a *Reasoning* question with the concept *Ratio & Proportion*
    under the reasoning chapter *Mathematical Operations* is correct.  Only a
    chapter (or, for a chapterless record, a leaf name) that its own subject does
    not declare is wrong — see `tools/cross_subject_audit.py`.
14. Every AI failure is recorded in `state/errors.jsonl` (append-only, no keys,
    message ≤ 300 chars); a test run redirects the ledger via `PYQ_ERRORS_LEDGER`
    and **never** appends to the real one.

---

## 10. Quick start for a fresh agent

```bash
git clone https://github.com/sujitbhai7710/repeatermock-pyq-papers.git
cd repeatermock-pyq-papers
python -m agent.cli taxonomy          # rebuild taxonomy from the 4 MDs
python -m agent.cli phase0            # rebuild index + distribution
python -m agent.cli routes --probe    # see what AI is alive
python -m agent.cli audit             # expect VIOLATIONS: 0
python -m unittest discover -s tests -t .   # expect 231 tests OK
python -m agent.cli stats --top 20    # see the counts
```
Then start the AI work (`grammar --ai`, then `run`), and finish with `audit` → `publish`.
