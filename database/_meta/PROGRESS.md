# PYQ Agent — Progress

_generated 2026-09-11T23:12:00Z_

## Run status

- status: **`time_limit`**
- meaning: no AI route available – the deterministic pipeline (phase0 + python extraction + database + mocks) completed anyway; only the AI verification step was skipped
- recorded: 2026-09-11T23:12:00Z
- route health last updated: 2026-09-11T21:31:28Z

## Last checkpoint

- run id: `run-20260911T211340Z-4039a9c3`
- phase: `phase5`
- status: `ai_unavailable`
- last checkpoint at: 2026-09-11T21:31:35Z
- work window: elapsed 1071.133s of 19800s (checkpoint every 900s)
- cursor: `{'completed': ['phase0', 'phase1', 'phase2', 'phase3', 'phase4', 'phase5'], 'next': None}`

## Phases (work items)

| Phase | Status | Items | Done | % | This run | Remaining | ETA | Updated |
|---|---|---:|---:|---:|---:|---:|---:|---|
| `phase0` | ok | 142,090 | 142,090 | 100.0% | 142,090 | 0 | 0s | 2026-09-11T21:14:10Z |
| `phase1` | ai_unavailable | 37,990 | 440 | 1.2% | 440 | 37,550 | 23.1h | 2026-09-11T21:31:17Z |
| `phase2` | ai_unavailable | 0 | 0 | - | 0 | 0 | 0s | 2026-09-11T21:31:20Z |
| `phase3` | ai_unavailable | 0 | 0 | - | 0 | 0 | 0s | 2026-09-11T21:31:23Z |
| `phase4` | ai_unavailable | 0 | 0 | - | 0 | 0 | 0s | 2026-09-11T21:31:26Z |
| `phase5` | ai_unavailable | 0 | 0 | - | 0 | 0 | 0s | 2026-09-11T21:31:28Z |

_Items = questions the phase must cover (AI verification for phases 1-5, extracted questions for phase0); ETA extrapolates this run's throughput._

## Notes

- `phase1`: same_model_fallback: true – proposer and critic were both served by gpt-5.6-sol; no second model had a working route
- `phase1`: halted (AI unavailable): no healthy route for any candidate model of proposer (deepseek-v4-flash, deepseek-v4.1-flash:free, gpt-5.6-sol): deepseek-v4-flash: 6 consecutive failures for deepseek-v4-flash (threshold 6); last: HTTP 503 from https://jw-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":null,"last_status":0,"failover_count":0,"failed_keys":0}}; deepseek-v4.1-flash:free: 6 consecutive failures for deepseek-v4.1-flash:free (threshold 6); last: HTTP 503 from https://jw-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":"auth-403","last_status":403,"failover_count":3,"failed_keys":3}}; gpt-5.6-sol: 6 consecutive failures for gpt-5.6-sol (threshold 6); last: HTTP 503 from https://jw-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":null,"last_status":0,"failover_count":0,"failed_keys":0}}
- `phase2`: no healthy route for any candidate model of the proposer, critic role(s) at startup: tokenharbor/deepseek-v4-flash: no credentials; justwoker/deepseek-v4-flash: closed; jw-worker/deepseek-v4-flash: closed; ar-worker/deepseek-v4-flash: closed; zen-rotator/deepseek-v4-flash: no credentials; agentrouter/deepseek-v4-flash: closed; tokenharbor/deepseek-v4.1-flash:free: no credentials; justwoker/deepseek-v4.1-flash:free: closed; jw-worker/deepseek-v4.1-flash:free: closed; ar-worker/deepseek-v4.1-flash:free: closed; zen-rotator/deepseek-v4.1-flash:free: no credentials; agentrouter/deepseek-v4.1-flash:free: closed; tokenharbor/gpt-5.6-sol: no credentials; justwoker/gpt-5.6-sol: closed; jw-worker/gpt-5.6-sol: closed; ar-worker/gpt-5.6-sol: closed; zen-rotator/gpt-5.6-sol: no credentials; agentrouter/gpt-5.6-sol: closed; tokenharbor/gpt-5.6-sol: no credentials; justwoker/gpt-5.6-sol: closed; jw-worker/gpt-5.6-sol: closed; ar-worker/gpt-5.6-sol: closed; zen-rotator/gpt-5.6-sol: no credentials; agentrouter/gpt-5.6-sol: closed; tokenharbor/deepseek-v4.1-flash:free: no credentials; justwoker/deepseek-v4.1-flash:free: closed; jw-worker/deepseek-v4.1-flash:free: closed; ar-worker/deepseek-v4.1-flash:free: closed; zen-rotator/deepseek-v4.1-flash:free: no credentials; agentrouter/deepseek-v4.1-flash:free: closed
- `phase2`: no healthy route for any candidate model of the proposer, critic role(s) at startup: tokenharbor/deepseek-v4-flash: no credentials; justwoker/deepseek-v4-flash: closed; jw-worker/deepseek-v4-flash: closed; ar-worker/deepseek-v4-flash: closed; zen-rotator/deepseek-v4-flash: no credentials; agentrouter/deepseek-v4-flash: closed; tokenharbor/deepseek-v4.1-flash:free: no credentials; justwoker/deepseek-v4.1-flash:free: closed; jw-worker/deepseek-v4.1-flash:free: closed; ar-worker/deepseek-v4.1-flash:free: closed; zen-rotator/deepseek-v4.1-flash:free: no credentials; agentrouter/deepseek-v4.1-flash:free: closed; tokenharbor/gpt-5.6-sol: no credentials; justwoker/gpt-5.6-sol: closed; jw-worker/gpt-5.6-sol: closed; ar-worker/gpt-5.6-sol: closed; zen-rotator/gpt-5.6-sol: no credentials; agentrouter/gpt-5.6-sol: closed; tokenharbor/gpt-5.6-sol: no credentials; justwoker/gpt-5.6-sol: closed; jw-worker/gpt-5.6-sol: closed; ar-worker/gpt-5.6-sol: closed; zen-rotator/gpt-5.6-sol: no credentials; agentrouter/gpt-5.6-sol: closed; tokenharbor/deepseek-v4.1-flash:free: no credentials; justwoker/deepseek-v4.1-flash:free: closed; jw-worker/deepseek-v4.1-flash:free: closed; ar-worker/deepseek-v4.1-flash:free: closed; zen-rotator/deepseek-v4.1-flash:free: no credentials; agentrouter/deepseek-v4.1-flash:free: closed
- `phase3`: no healthy route for any candidate model of the proposer, critic role(s) at startup: tokenharbor/deepseek-v4-flash: no credentials; justwoker/deepseek-v4-flash: closed; jw-worker/deepseek-v4-flash: closed; ar-worker/deepseek-v4-flash: closed; zen-rotator/deepseek-v4-flash: no credentials; agentrouter/deepseek-v4-flash: closed; tokenharbor/deepseek-v4.1-flash:free: no credentials; justwoker/deepseek-v4.1-flash:free: closed; jw-worker/deepseek-v4.1-flash:free: closed; ar-worker/deepseek-v4.1-flash:free: closed; zen-rotator/deepseek-v4.1-flash:free: no credentials; agentrouter/deepseek-v4.1-flash:free: closed; tokenharbor/gpt-5.6-sol: no credentials; justwoker/gpt-5.6-sol: closed; jw-worker/gpt-5.6-sol: closed; ar-worker/gpt-5.6-sol: closed; zen-rotator/gpt-5.6-sol: no credentials; agentrouter/gpt-5.6-sol: closed; tokenharbor/gpt-5.6-sol: no credentials; justwoker/gpt-5.6-sol: closed; jw-worker/gpt-5.6-sol: closed; ar-worker/gpt-5.6-sol: closed; zen-rotator/gpt-5.6-sol: no credentials; agentrouter/gpt-5.6-sol: closed; tokenharbor/deepseek-v4.1-flash:free: no credentials; justwoker/deepseek-v4.1-flash:free: closed; jw-worker/deepseek-v4.1-flash:free: closed; ar-worker/deepseek-v4.1-flash:free: closed; zen-rotator/deepseek-v4.1-flash:free: no credentials; agentrouter/deepseek-v4.1-flash:free: closed
- `phase3`: no healthy route for any candidate model of the proposer, critic role(s) at startup: tokenharbor/deepseek-v4-flash: no credentials; justwoker/deepseek-v4-flash: closed; jw-worker/deepseek-v4-flash: closed; ar-worker/deepseek-v4-flash: closed; zen-rotator/deepseek-v4-flash: no credentials; agentrouter/deepseek-v4-flash: closed; tokenharbor/deepseek-v4.1-flash:free: no credentials; justwoker/deepseek-v4.1-flash:free: closed; jw-worker/deepseek-v4.1-flash:free: closed; ar-worker/deepseek-v4.1-flash:free: closed; zen-rotator/deepseek-v4.1-flash:free: no credentials; agentrouter/deepseek-v4.1-flash:free: closed; tokenharbor/gpt-5.6-sol: no credentials; justwoker/gpt-5.6-sol: closed; jw-worker/gpt-5.6-sol: closed; ar-worker/gpt-5.6-sol: closed; zen-rotator/gpt-5.6-sol: no credentials; agentrouter/gpt-5.6-sol: closed; tokenharbor/gpt-5.6-sol: no credentials; justwoker/gpt-5.6-sol: closed; jw-worker/gpt-5.6-sol: closed; ar-worker/gpt-5.6-sol: closed; zen-rotator/gpt-5.6-sol: no credentials; agentrouter/gpt-5.6-sol: closed; tokenharbor/deepseek-v4.1-flash:free: no credentials; justwoker/deepseek-v4.1-flash:free: closed; jw-worker/deepseek-v4.1-flash:free: closed; ar-worker/deepseek-v4.1-flash:free: closed; zen-rotator/deepseek-v4.1-flash:free: no credentials; agentrouter/deepseek-v4.1-flash:free: closed
- `phase4`: no healthy route for any candidate model of the proposer, critic role(s) at startup: tokenharbor/deepseek-v4-flash: no credentials; justwoker/deepseek-v4-flash: closed; jw-worker/deepseek-v4-flash: closed; ar-worker/deepseek-v4-flash: closed; zen-rotator/deepseek-v4-flash: no credentials; agentrouter/deepseek-v4-flash: closed; tokenharbor/deepseek-v4.1-flash:free: no credentials; justwoker/deepseek-v4.1-flash:free: closed; jw-worker/deepseek-v4.1-flash:free: closed; ar-worker/deepseek-v4.1-flash:free: closed; zen-rotator/deepseek-v4.1-flash:free: no credentials; agentrouter/deepseek-v4.1-flash:free: closed; tokenharbor/gpt-5.6-sol: no credentials; justwoker/gpt-5.6-sol: closed; jw-worker/gpt-5.6-sol: closed; ar-worker/gpt-5.6-sol: closed; zen-rotator/gpt-5.6-sol: no credentials; agentrouter/gpt-5.6-sol: closed; tokenharbor/gpt-5.6-sol: no credentials; justwoker/gpt-5.6-sol: closed; jw-worker/gpt-5.6-sol: closed; ar-worker/gpt-5.6-sol: closed; zen-rotator/gpt-5.6-sol: no credentials; agentrouter/gpt-5.6-sol: closed; tokenharbor/deepseek-v4.1-flash:free: no credentials; justwoker/deepseek-v4.1-flash:free: closed; jw-worker/deepseek-v4.1-flash:free: closed; ar-worker/deepseek-v4.1-flash:free: closed; zen-rotator/deepseek-v4.1-flash:free: no credentials; agentrouter/deepseek-v4.1-flash:free: closed
- `phase4`: no healthy route for any candidate model of the proposer, critic role(s) at startup: tokenharbor/deepseek-v4-flash: no credentials; justwoker/deepseek-v4-flash: closed; jw-worker/deepseek-v4-flash: closed; ar-worker/deepseek-v4-flash: closed; zen-rotator/deepseek-v4-flash: no credentials; agentrouter/deepseek-v4-flash: closed; tokenharbor/deepseek-v4.1-flash:free: no credentials; justwoker/deepseek-v4.1-flash:free: closed; jw-worker/deepseek-v4.1-flash:free: closed; ar-worker/deepseek-v4.1-flash:free: closed; zen-rotator/deepseek-v4.1-flash:free: no credentials; agentrouter/deepseek-v4.1-flash:free: closed; tokenharbor/gpt-5.6-sol: no credentials; justwoker/gpt-5.6-sol: closed; jw-worker/gpt-5.6-sol: closed; ar-worker/gpt-5.6-sol: closed; zen-rotator/gpt-5.6-sol: no credentials; agentrouter/gpt-5.6-sol: closed; tokenharbor/gpt-5.6-sol: no credentials; justwoker/gpt-5.6-sol: closed; jw-worker/gpt-5.6-sol: closed; ar-worker/gpt-5.6-sol: closed; zen-rotator/gpt-5.6-sol: no credentials; agentrouter/gpt-5.6-sol: closed; tokenharbor/deepseek-v4.1-flash:free: no credentials; justwoker/deepseek-v4.1-flash:free: closed; jw-worker/deepseek-v4.1-flash:free: closed; ar-worker/deepseek-v4.1-flash:free: closed; zen-rotator/deepseek-v4.1-flash:free: no credentials; agentrouter/deepseek-v4.1-flash:free: closed
- `phase5`: no healthy route for any candidate model of the proposer, critic role(s) at startup: tokenharbor/deepseek-v4-flash: no credentials; justwoker/deepseek-v4-flash: closed; jw-worker/deepseek-v4-flash: closed; ar-worker/deepseek-v4-flash: closed; zen-rotator/deepseek-v4-flash: no credentials; agentrouter/deepseek-v4-flash: closed; tokenharbor/deepseek-v4.1-flash:free: no credentials; justwoker/deepseek-v4.1-flash:free: closed; jw-worker/deepseek-v4.1-flash:free: closed; ar-worker/deepseek-v4.1-flash:free: closed; zen-rotator/deepseek-v4.1-flash:free: no credentials; agentrouter/deepseek-v4.1-flash:free: closed; tokenharbor/gpt-5.6-sol: no credentials; justwoker/gpt-5.6-sol: closed; jw-worker/gpt-5.6-sol: closed; ar-worker/gpt-5.6-sol: closed; zen-rotator/gpt-5.6-sol: no credentials; agentrouter/gpt-5.6-sol: closed; tokenharbor/gpt-5.6-sol: no credentials; justwoker/gpt-5.6-sol: closed; jw-worker/gpt-5.6-sol: closed; ar-worker/gpt-5.6-sol: closed; zen-rotator/gpt-5.6-sol: no credentials; agentrouter/gpt-5.6-sol: closed; tokenharbor/deepseek-v4.1-flash:free: no credentials; justwoker/deepseek-v4.1-flash:free: closed; jw-worker/deepseek-v4.1-flash:free: closed; ar-worker/deepseek-v4.1-flash:free: closed; zen-rotator/deepseek-v4.1-flash:free: no credentials; agentrouter/deepseek-v4.1-flash:free: closed
- `phase5`: no healthy route for any candidate model of the proposer, critic role(s) at startup: tokenharbor/deepseek-v4-flash: no credentials; justwoker/deepseek-v4-flash: closed; jw-worker/deepseek-v4-flash: closed; ar-worker/deepseek-v4-flash: closed; zen-rotator/deepseek-v4-flash: no credentials; agentrouter/deepseek-v4-flash: closed; tokenharbor/deepseek-v4.1-flash:free: no credentials; justwoker/deepseek-v4.1-flash:free: closed; jw-worker/deepseek-v4.1-flash:free: closed; ar-worker/deepseek-v4.1-flash:free: closed; zen-rotator/deepseek-v4.1-flash:free: no credentials; agentrouter/deepseek-v4.1-flash:free: closed; tokenharbor/gpt-5.6-sol: no credentials; justwoker/gpt-5.6-sol: closed; jw-worker/gpt-5.6-sol: closed; ar-worker/gpt-5.6-sol: closed; zen-rotator/gpt-5.6-sol: no credentials; agentrouter/gpt-5.6-sol: closed; tokenharbor/gpt-5.6-sol: no credentials; justwoker/gpt-5.6-sol: closed; jw-worker/gpt-5.6-sol: closed; ar-worker/gpt-5.6-sol: closed; zen-rotator/gpt-5.6-sol: no credentials; agentrouter/gpt-5.6-sol: closed; tokenharbor/deepseek-v4.1-flash:free: no credentials; justwoker/deepseek-v4.1-flash:free: closed; jw-worker/deepseek-v4.1-flash:free: closed; ar-worker/deepseek-v4.1-flash:free: closed; zen-rotator/deepseek-v4.1-flash:free: no credentials; agentrouter/deepseek-v4.1-flash:free: closed

## Provider health (per provider + model, 2026-09-11T21:31:28Z)

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
| `tokenharbor/deepseek-v4-flash` | unconfigured | 0 | 0 | 0 | 0 | - | - | - |
| `tokenharbor/deepseek-v4.1-flash:free` | unconfigured | 0 | 0 | 0 | 0 | - | - | - |
| `tokenharbor/gpt-5.6-sol` | unconfigured | 0 | 0 | 0 | 0 | - | - | - |
| `zen-rotator/deepseek-v4-flash` | unconfigured | 0 | 0 | 0 | 0 | - | - | - |
| `zen-rotator/deepseek-v4.1-flash:free` | unconfigured | 0 | 0 | 0 | 0 | - | - | - |
| `zen-rotator/gpt-5.6-sol` | unconfigured | 0 | 0 | 0 | 0 | - | - | - |

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
| `mocks` | 1,171 | 1,680,701 |
| `phase0` | 18 | 111,514,929 |
| `phase1` | 860 | 85,573,442 |
| `phase2` | 1,269 | 20,461,575 |
| `phase3` | 301 | 17,855,676 |
| `phase4` | 242 | 18,797,602 |
| `phase5` | 37 | 163,518 |
| `taxonomy` | 3 | 1,447,678 |
