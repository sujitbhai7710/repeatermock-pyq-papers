# MEMORY.md — SSC PYQ Database Project

> Durable handover memory for **any** agent or developer who picks this project up.
> Read this first. It contains the facts that are expensive to rediscover.
> Companion file: `LESSONS.md` (problems hit + fixes). Read that before debugging.

---

## 1. What this project is

Turns the raw SSC previous-year question papers in this repo into a **topic-wise database**,
plus trends, priority lists, notes and **mock-generator-ready packs**.

| Fact | Value |
|---|---|
| Repo | `https://github.com/sujitbhai7710/repeatermock-pyq-papers` (public, default branch `main`) |
| Local clone | `C:\Users\akasa\.openclaw-autoclaw\workspace\repeatermock-pyq-papers` |
| Raw data | `SSC-CGL/`, `SSC-CHSL/`, `SSC-CPO/`, `SSC-GD/`, `SSC-MTS/`, `SSC-Selection-Post/`, `SSC-Stenographer/` — one JSON per paper |
| Taxonomy | `chapter-and-topic/*.md` (4 files) — the canonical vocabulary |
| **RAW DATA IS READ-ONLY** | Never edit `SSC-*/**/*.json` or `chapter-and-topic/*.md` |

## 2. Scope (locked)

**Papers from 2019–2025 only.**

| Exam | Papers | Questions |
|---|---|---|
| SSC-CGL | 235 | 23,900 |
| SSC-CHSL | 303 | 30,440 |
| SSC-CPO | 65 | 13,000 |
| SSC-GD | 260 | 26,000 |
| SSC-MTS | 294 | 27,750 |
| SSC-Selection-Post | 120 | 12,000 |
| SSC-Stenographer | 45 | 9,000 |
| **TOTAL in scope** | **1,322** | **142,090** |

Year rule: deepest folder segment matching `20\d\d`, **else** the year inside `title`
(required for Selection-Post, whose folders are by level, not year).
287 papers / 32,800 questions fall outside 2019–2025 and are dropped.

## 3. INVARIANTS — break these and the run is wrong

1. **Coverage identity:** `placed + skipped_hindi + unclassified + flagged_papers_questions == 142,090`.
   Current: `130,020 + 3,456 + 8,614 + 0 = 142,090`.
2. **`python -m agent.cli audit` must print `VIOLATIONS: 0`.** CI fails the job otherwise.
3. **Idempotent:** re-running any phase must change nothing (only `PROGRESS.md` timestamp moves).
4. **No raw-data edits** (see §1).
5. **Never log key material.** Only masked `key_ref` values.
6. **Hindi is skipped**: Devanagari in the question prompt or any option ⇒ `lang=hi`, excluded from
   every subject DB and from all LLM calls.
7. No generated file may exceed **40 MB** (GitHub rejects > 100 MB; we stay far below).

## 4. Data facts worth remembering

Question object keys:
`qid, n, type, concept, confidence, marks_pos, marks_neg, question, options[], correct, solution, tags`.

- `options` is a list of `{label, text}` **or** a dict — handle both.
- `correct` is `"1".."4"`.
- Images are embedded in `question` and `solution` as `[IMAGE: https://cdn.repeatermock.com/tb/<hash>.png]`
  — **preserve verbatim**, never strip.
- `confidence` is `high` or `unidentified` (~8,033 in scope are `unidentified` → these are the AI workload).
- **`n` RESTARTS AT 1 PER SECTION.** It is NOT a paper position. Section identity comes from the
  index within `questions[]` (global ordinal = index + 1), except that papers with a declared layout
  use `n` inside its own section. This one fact breaks naive positional mapping — see `LESSONS.md` L2.
- Paper-level `subject` is junk (`general` for 1,591/1,609 papers). **Never trust it.**

## 5. Subjects

Five first-class subjects: `REAS`, `GK`, `MATH`, `ENG`, **`COMPUTER`** (CGL Tier-II only).
Pipeline order is fixed: **English → GK/GS → Maths → Reasoning → Computer**.

### Section layout table (measured, year-aware — lives in `config/exams.json`)

| Exam / Tier | Layout |
|---|---|
| CGL Tier-I | REAS 1–25 · GK 26–50 · MATH 51–75 · ENG 76–100 |
| CGL Tier-II Paper-I | MATH 30 · REAS 30 · ENG 45 · GK 25 · COMPUTER 20 |
| CGL Tier-II (single-subject papers) | ENG 200 / MATH 100 |
| CHSL Tier-I | 2022-23 & 2023: REAS, GK, MATH, ENG · otherwise: ENG, REAS, MATH, GK |
| MTS | 2023+ (90 Q): MATH, REAS, GK, ENG (20/20/25/25) · 2022: REAS, MATH, ENG, GK |
| CPO Paper-I | REAS 50 · GK 50 · MATH 50 · ENG 50 |
| CPO Paper-II | ENG 200 |
| GD | REAS, GK, MATH, ENG (20 each) + **Hindi 81–100 (skipped)** · 2019/2021: four 25-Q sections, no Hindi |
| Selection Post | REAS 25 · GK 25 · MATH 25 · ENG 25 |
| Stenographer | REAS 50 · GK 50 · ENG 100 |

Every paper is **signature-validated** (section-level agreement ≥ 95 %, per-question metric secondary).
Current: **1,318 / 1,322 validated, 4 flagged**, 0 unplaceable.

## 6. Repository layout

```
agent/          python package (stdlib only — no third-party runtime deps)
  cli.py        entrypoint:  python -m agent.cli <command>
  taxonomy.py   parses chapter-and-topic/*.md -> taxonomy.json + alias_map.json
  discover.py   walk papers + year filter
  sections.py   positional subject map + signature validation + Hindi detection
  classify.py   concept/tag -> chapter/topic/concept (word-boundary matching)
  indexer.py    sharded index writer/reader
  vocab.py      synonym/antonym 4-bucket, OWS, idioms, spelling, homonyms
  grammar.py    map grammar Qs to the 129 rules
  distribution.py / build_db.py / mockdata.py
  router.py     provider failover + circuit breaker + GLOBAL HALT
  llm.py        OpenAI-compatible client (+ reasoning-truncation escalation)
  debate.py     DeepSeek proposes -> GPT-5.6 Sol judges (max 1 rebuttal)
  verify.py     AI re-check of every python-extracted item (resumable, windowed)
  websearch.py  Monid (TinyFish) search/fetch
  checkpoint.py / tracking.py
  phases/       phase0..phase5 (phase0 first, always)
config/         exams.json (layout table), settings.json, supplementary_taxonomy.json
tools/          resolve.py, mocks.py, audit_db.py
tests/          123 tests (pure functions + failover + soft stop + audit rules)
state/          GENERATED (gitignored on main) — taxonomy, alias map, sharded index, checkpoints
database/       GENERATED (gitignored on main) — the deliverable tree
.github/workflows/pyq-agent.yml   the CI pipeline
```

**Branch model:** `main` = code only. Generated `state/` + `database/` are published to the
**`pyq-db`** branch by CI via `git add -f state database`.

## 7. Commands cheat sheet

Run everything from the project root.

```powershell
# IMPORTANT: use the SYSTEM python, not the bundled one (see LESSONS.md L7)
$py = "C:\Users\akasa\AppData\Local\Programs\Python\Python311\python.exe"

& $py -m agent.cli taxonomy     # parse the 4 taxonomy MDs
& $py -m agent.cli phase0       # discovery + Hindi split + index + distribution
& $py -m agent.cli run          # full pipeline (honours checkpoint/resume + work window)
& $py -m agent.cli run --fresh  # ignore the checkpoint
& $py -m agent.cli run --no-ai  # python extraction only, no LLM calls
& $py -m agent.cli phase 3      # one phase (0-5)
& $py -m agent.cli verify-db --limit 20 --batch-size 20 --report-only
& $py -m agent.cli audit        # MUST print VIOLATIONS: 0
& $py -m agent.cli stats --top 20
& $py -m agent.cli mocks
& $py -m unittest discover -s tests -t .
```

Exit codes: `0` ok · `3` rate_limited (soft stop) · `4` time_limit (soft stop) · `1` error.
Soft stops checkpoint and publish; the workflow treats 3/4 as success.

## 8. Models, providers, secrets

| Provider | Endpoint | Role |
|---|---|---|
| ar-rotator (agentrouter) | `https://ar-rotator.opencode-5a3.workers.dev/v1` | **primary** (OpenAI-compatible) |
| jw-rotator (justwoker) | `https://jw-rotator.opencode-5a3.workers.dev/v1` | **fallback** |
| Monid (TinyFish) | `https://api.monid.ai/v1/{inspect,run,discover}` | web search + fetch (free) |

- Models: **`deepseek-v4-flash`** (proposer) and **`gpt-5.6-sol`** (judge).
- **Every worker call MUST send a browser `User-Agent`** or Cloudflare answers `403 error code: 1010`.
- Repo secrets: `AR_PROXY_TOKEN`, `JW_PROXY_TOKEN`, `MONID_API_KEY` (set, encrypted).
- **Never hardcode keys.** Read from env. `.env`/key files are never committed.

## 9. CI behaviour

`.github/workflows/pyq-agent.yml`: `workflow_dispatch` + cron every 6 h, **`timeout-minutes: 350`
as a JOB key**, `concurrency: {group: pyq-agent, cancel-in-progress: false}`, `permissions: contents: write`.
Steps: checkout(depth 0) → setup-python 3.11 → pip → preflight keys → run agent
(`MAX_WORK_SECONDS=19800`) → `agent.cli audit` → PROGRESS.md into job summary → commit+push
`state`+`database` to `pyq-db` → upload artifacts.
No secrets ⇒ no-op dry run, exit 0, `status=skipped_no_keys`.

## 10. Things you will be tempted to "fix" — don't

- The `_other` bucket (~12,800 records) is **deliberate**: the taxonomy MDs do not declare those
  labels, and raw data is read-only. Promoting them means a curated `config/chapter_aliases.json`
  reviewed by a human — not inventing taxonomy.
- 1 `qid` is reused by the source papers for two different questions — a **source-data quirk**,
  reported as a warning, not a violation.
- 13 CHSL/MTS papers are flagged `NEEDS_AI_REVIEW` by design; `verify-db` (with keys) closes them.
