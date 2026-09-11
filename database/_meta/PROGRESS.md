# PYQ Agent — Progress

_generated 2026-09-11T21:30:35Z_

## Run status

- status: **`ai_unavailable`**
- meaning: no AI route available – the deterministic pipeline (phase0 + python extraction + database + mocks) completed anyway; only the AI verification step was skipped
- route health last updated: 2026-09-11T21:30:35Z

## Last checkpoint

- run id: `run-20260911T211340Z-4039a9c3`
- phase: `phase1`
- status: `ai_unavailable`
- last checkpoint at: 2026-09-11T21:30:35Z
- work window: elapsed 1011.21s of 19800s (checkpoint every 900s)
- cursor: `{'completed': ['phase0'], 'verify': {'phase': 'phase1', 'batch': 0, 'batches_done': 22, 'items_done': 440, 'items_total': 37990, 'state_file': 'state/verify_state.json', 'started_wall': '2026-09-11T21:14:18Z'}}`

## Phases (work items)

| Phase | Status | Items | Done | % | This run | Remaining | ETA | Updated |
|---|---|---:|---:|---:|---:|---:|---:|---|
| `phase0` | ok | 142,090 | 142,090 | 100.0% | 142,090 | 0 | 0s | 2026-09-11T21:14:10Z |
| `phase1` | ? | 37,990 | 440 | 1.2% | 440 | 37,550 | 23.1h | 2026-09-11T21:30:35Z |
| `phase2` | pending | - | - | - | - | - | - | - |
| `phase3` | pending | - | - | - | - | - | - | - |
| `phase4` | pending | - | - | - | - | - | - | - |
| `phase5` | pending | - | - | - | - | - | - | - |

_Items = questions the phase must cover (AI verification for phases 1-5, extracted questions for phase0); ETA extrapolates this run's throughput._

## Notes

- `phase1`: same_model_fallback: true – proposer and critic were both served by gpt-5.6-sol; no second model had a working route

## Provider health (per provider + model, 2026-09-11T21:30:35Z)

| Route | State | OK | Fail | Rate-limited | Trips | Cooldown | Retry in | Last reason |
|---|---|---:|---:|---:|---:|---:|---:|---|
| `agentrouter/deepseek-v4-flash` | closed | 0 | 1 | 0 | 0 | - | - | - |
| `agentrouter/deepseek-v4.1-flash:free` | closed | 0 | 1 | 0 | 0 | - | - | - |
| `agentrouter/gpt-5.6-sol` | closed | 0 | 2 | 0 | 0 | - | - | - |
| `ar-worker/deepseek-v4-flash` | closed | 0 | 1 | 1 | 0 | - | - | - |
| `ar-worker/deepseek-v4.1-flash:free` | closed | 0 | 1 | 1 | 0 | - | - | - |
| `ar-worker/gpt-5.6-sol` | closed | 0 | 2 | 2 | 0 | - | - | - |
| `justwoker/deepseek-v4-flash` | closed | 0 | 2 | 0 | 0 | - | - | - |
| `justwoker/deepseek-v4.1-flash:free` | closed | 0 | 2 | 0 | 0 | - | - | - |
| `justwoker/gpt-5.6-sol` | closed | 84 | 3 | 1 | 0 | - | - | - |
| `jw-worker/deepseek-v4-flash` | closed | 0 | 2 | 2 | 0 | - | - | - |
| `jw-worker/deepseek-v4.1-flash:free` | closed | 0 | 2 | 2 | 0 | - | - | - |
| `jw-worker/gpt-5.6-sol` | closed | 0 | 3 | 3 | 0 | - | - | - |
| `tokenharbor/deepseek-v4-flash` | unavailable | 0 | 0 | 0 | 0 | - | - | no credentials |
| `tokenharbor/deepseek-v4.1-flash:free` | unavailable | 0 | 0 | 0 | 0 | - | - | no credentials |
| `tokenharbor/gpt-5.6-sol` | unavailable | 0 | 0 | 0 | 0 | - | - | no credentials |
| `zen-rotator/deepseek-v4-flash` | unavailable | 0 | 0 | 0 | 0 | - | - | no credentials |
| `zen-rotator/deepseek-v4.1-flash:free` | unavailable | 0 | 0 | 0 | 0 | - | - | no credentials |
| `zen-rotator/gpt-5.6-sol` | unavailable | 0 | 0 | 0 | 0 | - | - | no credentials |

## Artifacts per phase

| Phase | Files | Bytes |
|---|---:|---:|
| `phase0` | 18 | 111,514,929 |
| `taxonomy` | 3 | 1,447,678 |
