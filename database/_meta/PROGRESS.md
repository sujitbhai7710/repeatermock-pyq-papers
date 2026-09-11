# PYQ Agent — Progress

_generated 2026-09-11T08:33:53Z_

## Run status

- status: **`time_limit`**
- meaning: run complete
- recorded: 2026-09-11T08:33:53Z
- route health last updated: 2026-09-11T05:55:10Z

## Phases (work items)

| Phase | Status | Items | Done | % | This run | Remaining | ETA | Updated |
|---|---|---:|---:|---:|---:|---:|---:|---|
| `phase0` | ok | 142,090 | 142,090 | 100.0% | 142,090 | 0 | 0s | 2026-09-11T08:32:31Z |
| `phase1` | ok | 20 | 20 | 100.0% | 0 | 0 | 0s | 2026-09-11T08:26:59Z |
| `phase2` | pending | - | - | - | - | - | - | - |
| `phase3` | pending | - | - | - | - | - | - | - |
| `phase4` | pending | - | - | - | - | - | - | - |
| `phase5` | pending | - | - | - | - | - | - | - |

_Items = questions the phase must cover (AI verification for phases 1-5, extracted questions for phase0); ETA extrapolates this run's throughput._

## Provider health (per provider + model, 2026-09-11T05:55:10Z)

| Route | State | OK | Fail | Rate-limited | Trips | Cooldown | Retry in | Last reason |
|---|---|---:|---:|---:|---:|---:|---:|---|
| `agentrouter/deepseek-v4-flash` | closed | 0 | 0 | 0 | 0 | - | - | - |
| `agentrouter/gpt-5.6-sol` | closed | 0 | 0 | 0 | 0 | - | - | - |
| `ar-worker/deepseek-v4-flash` | closed | 0 | 0 | 0 | 0 | - | - | closed |
| `ar-worker/gpt-5.6-sol` | closed | 0 | 0 | 0 | 0 | - | - | closed |
| `justwoker/deepseek-v4-flash` | closed | 0 | 0 | 0 | 0 | - | - | closed |
| `justwoker/gpt-5.6-sol` | closed | 0 | 0 | 0 | 0 | - | - | closed |
| `jw-worker/deepseek-v4-flash` | closed | 0 | 0 | 0 | 0 | - | - | closed |
| `jw-worker/gpt-5.6-sol` | closed | 0 | 0 | 0 | 0 | - | - | closed |

## Coverage

- papers: 1322
- questions: 142090
- placed: 130020
- unclassified: 8614
- skipped_hindi: 3456
- flagged_papers_questions: 0
- identity: placed(130020) + skipped_hindi(3456) + unclassified(8614) + flagged(0) = 142090
- papers_validated: 1318
- papers_flagged: 4

## Artifacts per phase

| Phase | Files | Bytes |
|---|---:|---:|
| `mocks` | 1,418 | 1,831,644 |
| `phase0` | 19 | 216,948,831 |
| `phase1` | 855 | 63,600,338 |
| `phase2` | 1,876 | 25,640,753 |
| `phase3` | 423 | 20,965,132 |
| `phase4` | 335 | 20,371,398 |
| `phase5` | 44 | 163,684 |
| `taxonomy` | 3 | 1,434,048 |
