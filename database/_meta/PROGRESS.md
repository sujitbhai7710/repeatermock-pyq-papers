# PYQ Agent — Progress

_generated 2026-09-11T06:07:20Z_

## Run status

- status: **`ai_unavailable`**
- meaning: no AI route available – the deterministic pipeline (phase0 + python extraction + database + mocks) completed anyway; only the AI verification step was skipped
- recorded: 2026-09-11T06:07:20Z
- route health last updated: 2026-09-11T06:07:12Z

## Last checkpoint

- run id: `run-20260911T060421Z-24a091de`
- phase: `phase5`
- status: `ai_unavailable`
- last checkpoint at: 2026-09-11T06:07:20Z
- work window: elapsed 174.766s of 19800s (checkpoint every 900s)
- cursor: `{'completed': ['phase0', 'phase1', 'phase2', 'phase3', 'phase4', 'phase5'], 'next': None}`

## Phases (work items)

| Phase | Status | Items | Done | % | This run | Remaining | ETA | Updated |
|---|---|---:|---:|---:|---:|---:|---:|---|
| `phase0` | ok | 142,090 | 142,090 | 100.0% | 142,090 | 0 | 0s | 2026-09-11T06:05:00Z |
| `phase1` | ai_unavailable | 37,990 | 20 | 0.1% | 20 | 37,970 | 55.9h | 2026-09-11T06:07:02Z |
| `phase2` | ai_unavailable | 0 | 0 | - | 0 | 0 | 0s | 2026-09-11T06:07:05Z |
| `phase3` | ai_unavailable | 0 | 0 | - | 0 | 0 | 0s | 2026-09-11T06:07:08Z |
| `phase4` | ai_unavailable | 0 | 0 | - | 0 | 0 | 0s | 2026-09-11T06:07:11Z |
| `phase5` | ai_unavailable | 0 | 0 | - | 0 | 0 | 0s | 2026-09-11T06:07:12Z |

_Items = questions the phase must cover (AI verification for phases 1-5, extracted questions for phase0); ETA extrapolates this run's throughput._

## Notes

- `phase1`: halted (AI unavailable): no healthy route for deepseek-v4-flash after 1 attempt(s) (tried justwoker); agentrouter: cooling down for 300s after 3 consecutive errors: invalid JSON from https://agentrouter.org/v1: <!doctype html> <meta charset="UTF-8"> <meta name="aliyun_waf_aa" content="ff926c7f07e45e2e487a29a6197d3460"> <meta name="aliyun_waf_bb" content="eade71455e2ad9c6d08b82bc7d98df8c"> <title></title> <meta name="viewport" content="width=device-width,initial-scale=1"> <script> !function(){var __webpack_; ar-worker: cooling down for 298s after HTTP 503 from https://ar-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":null,"last_status":0,"failover_count":0,"failed_keys":0}}; jw-worker: cooling down for 299s after HTTP 503 from https://jw-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":"auth-403","last_status":403,"failover_count":3,"failed_keys":3}}; justwoker: cooling down for 300s after 3 consecutive errors: HTTP 403 from https://api.justwoker.icu/v1: 
- `phase2`: no healthy route for deepseek-v4-flash at startup: agentrouter: cooling down for 286s after 3 consecutive errors: invalid JSON from https://agentrouter.org/v1: <!doctype html> <meta charset="UTF-8"> <meta name="aliyun_waf_aa" content="ff926c7f07e45e2e487a29a6197d3460"> <meta name="aliyun_waf_bb" content="eade71455e2ad9c6d08b82bc7d98df8c"> <title></title> <meta name="viewport" content="width=device-width,initial-scale=1"> <script> !function(){var __webpack_; ar-worker: cooling down for 284s after HTTP 503 from https://ar-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":null,"last_status":0,"failover_count":0,"failed_keys":0}}; jw-worker: cooling down for 285s after HTTP 503 from https://jw-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":"auth-403","last_status":403,"failover_count":3,"failed_keys":3}}; justwoker: cooling down for 286s after 3 consecutive errors: HTTP 403 from https://api.justwoker.icu/v1: 
- `phase2`: no healthy route for deepseek-v4-flash at startup: agentrouter: cooling down for 286s after 3 consecutive errors: invalid JSON from https://agentrouter.org/v1: <!doctype html> <meta charset="UTF-8"> <meta name="aliyun_waf_aa" content="ff926c7f07e45e2e487a29a6197d3460"> <meta name="aliyun_waf_bb" content="eade71455e2ad9c6d08b82bc7d98df8c"> <title></title> <meta name="viewport" content="width=device-width,initial-scale=1"> <script> !function(){var __webpack_; ar-worker: cooling down for 284s after HTTP 503 from https://ar-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":null,"last_status":0,"failover_count":0,"failed_keys":0}}; jw-worker: cooling down for 285s after HTTP 503 from https://jw-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":"auth-403","last_status":403,"failover_count":3,"failed_keys":3}}; justwoker: cooling down for 286s after 3 consecutive errors: HTTP 403 from https://api.justwoker.icu/v1: 
- `phase3`: no healthy route for deepseek-v4-flash at startup: agentrouter: cooling down for 283s after 3 consecutive errors: invalid JSON from https://agentrouter.org/v1: <!doctype html> <meta charset="UTF-8"> <meta name="aliyun_waf_aa" content="ff926c7f07e45e2e487a29a6197d3460"> <meta name="aliyun_waf_bb" content="eade71455e2ad9c6d08b82bc7d98df8c"> <title></title> <meta name="viewport" content="width=device-width,initial-scale=1"> <script> !function(){var __webpack_; ar-worker: cooling down for 281s after HTTP 503 from https://ar-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":null,"last_status":0,"failover_count":0,"failed_keys":0}}; jw-worker: cooling down for 282s after HTTP 503 from https://jw-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":"auth-403","last_status":403,"failover_count":3,"failed_keys":3}}; justwoker: cooling down for 283s after 3 consecutive errors: HTTP 403 from https://api.justwoker.icu/v1: 
- `phase3`: no healthy route for deepseek-v4-flash at startup: agentrouter: cooling down for 283s after 3 consecutive errors: invalid JSON from https://agentrouter.org/v1: <!doctype html> <meta charset="UTF-8"> <meta name="aliyun_waf_aa" content="ff926c7f07e45e2e487a29a6197d3460"> <meta name="aliyun_waf_bb" content="eade71455e2ad9c6d08b82bc7d98df8c"> <title></title> <meta name="viewport" content="width=device-width,initial-scale=1"> <script> !function(){var __webpack_; ar-worker: cooling down for 281s after HTTP 503 from https://ar-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":null,"last_status":0,"failover_count":0,"failed_keys":0}}; jw-worker: cooling down for 282s after HTTP 503 from https://jw-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":"auth-403","last_status":403,"failover_count":3,"failed_keys":3}}; justwoker: cooling down for 283s after 3 consecutive errors: HTTP 403 from https://api.justwoker.icu/v1: 
- `phase4`: no healthy route for deepseek-v4-flash at startup: agentrouter: cooling down for 280s after 3 consecutive errors: invalid JSON from https://agentrouter.org/v1: <!doctype html> <meta charset="UTF-8"> <meta name="aliyun_waf_aa" content="ff926c7f07e45e2e487a29a6197d3460"> <meta name="aliyun_waf_bb" content="eade71455e2ad9c6d08b82bc7d98df8c"> <title></title> <meta name="viewport" content="width=device-width,initial-scale=1"> <script> !function(){var __webpack_; ar-worker: cooling down for 278s after HTTP 503 from https://ar-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":null,"last_status":0,"failover_count":0,"failed_keys":0}}; jw-worker: cooling down for 279s after HTTP 503 from https://jw-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":"auth-403","last_status":403,"failover_count":3,"failed_keys":3}}; justwoker: cooling down for 280s after 3 consecutive errors: HTTP 403 from https://api.justwoker.icu/v1: 
- `phase4`: no healthy route for deepseek-v4-flash at startup: agentrouter: cooling down for 280s after 3 consecutive errors: invalid JSON from https://agentrouter.org/v1: <!doctype html> <meta charset="UTF-8"> <meta name="aliyun_waf_aa" content="ff926c7f07e45e2e487a29a6197d3460"> <meta name="aliyun_waf_bb" content="eade71455e2ad9c6d08b82bc7d98df8c"> <title></title> <meta name="viewport" content="width=device-width,initial-scale=1"> <script> !function(){var __webpack_; ar-worker: cooling down for 278s after HTTP 503 from https://ar-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":null,"last_status":0,"failover_count":0,"failed_keys":0}}; jw-worker: cooling down for 279s after HTTP 503 from https://jw-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":"auth-403","last_status":403,"failover_count":3,"failed_keys":3}}; justwoker: cooling down for 280s after 3 consecutive errors: HTTP 403 from https://api.justwoker.icu/v1: 
- `phase5`: no healthy route for deepseek-v4-flash at startup: agentrouter: cooling down for 277s after 3 consecutive errors: invalid JSON from https://agentrouter.org/v1: <!doctype html> <meta charset="UTF-8"> <meta name="aliyun_waf_aa" content="ff926c7f07e45e2e487a29a6197d3460"> <meta name="aliyun_waf_bb" content="eade71455e2ad9c6d08b82bc7d98df8c"> <title></title> <meta name="viewport" content="width=device-width,initial-scale=1"> <script> !function(){var __webpack_; ar-worker: cooling down for 275s after HTTP 503 from https://ar-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":null,"last_status":0,"failover_count":0,"failed_keys":0}}; jw-worker: cooling down for 276s after HTTP 503 from https://jw-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":"auth-403","last_status":403,"failover_count":3,"failed_keys":3}}; justwoker: cooling down for 277s after 3 consecutive errors: HTTP 403 from https://api.justwoker.icu/v1: 
- `phase5`: no healthy route for deepseek-v4-flash at startup: agentrouter: cooling down for 277s after 3 consecutive errors: invalid JSON from https://agentrouter.org/v1: <!doctype html> <meta charset="UTF-8"> <meta name="aliyun_waf_aa" content="ff926c7f07e45e2e487a29a6197d3460"> <meta name="aliyun_waf_bb" content="eade71455e2ad9c6d08b82bc7d98df8c"> <title></title> <meta name="viewport" content="width=device-width,initial-scale=1"> <script> !function(){var __webpack_; ar-worker: cooling down for 275s after HTTP 503 from https://ar-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":null,"last_status":0,"failover_count":0,"failed_keys":0}}; jw-worker: cooling down for 276s after HTTP 503 from https://jw-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown. Try again later or add more keys.","last_error":"auth-403","last_status":403,"failover_count":3,"failed_keys":3}}; justwoker: cooling down for 277s after 3 consecutive errors: HTTP 403 from https://api.justwoker.icu/v1: 

## Provider health (per provider + model, 2026-09-11T06:07:12Z)

| Route | State | OK | Fail | Rate-limited | Trips | Cooldown | Retry in | Last reason |
|---|---|---:|---:|---:|---:|---:|---:|---|
| `agentrouter/deepseek-v4-flash` | open | 0 | 3 | 0 | 1 | 5m | 5m | 3 consecutive errors: invalid JSON from https://agentrouter.org/v1: <!doctype html> <meta  |
| `agentrouter/gpt-5.6-sol` | closed | 0 | 1 | 0 | 0 | - | - | - |
| `ar-worker/deepseek-v4-flash` | open | 1 | 1 | 1 | 1 | 5m | 5m | HTTP 503 from https://ar-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_e |
| `ar-worker/gpt-5.6-sol` | open | 0 | 1 | 1 | 1 | 5m | 4m | HTTP 503 from https://ar-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_e |
| `justwoker/deepseek-v4-flash` | open | 0 | 3 | 0 | 1 | 5m | 5m | 3 consecutive errors: HTTP 403 from https://api.justwoker.icu/v1:  |
| `justwoker/gpt-5.6-sol` | closed | 0 | 0 | 0 | 0 | - | - | - |
| `jw-worker/deepseek-v4-flash` | open | 0 | 1 | 1 | 1 | 5m | 5m | HTTP 503 from https://jw-rotator.opencode-5a3.workers.dev/v1: {"error":{"type":"all_keys_e |
| `jw-worker/gpt-5.6-sol` | closed | 1 | 0 | 0 | 0 | - | - | - |

## Artifacts per phase

| Phase | Files | Bytes |
|---|---:|---:|
| `mocks` | 1,171 | 1,680,701 |
| `phase0` | 18 | 111,514,929 |
| `phase1` | 75 | 43,501,409 |
| `phase2` | 1,269 | 20,461,575 |
| `phase3` | 301 | 17,855,676 |
| `phase4` | 242 | 18,797,602 |
| `phase5` | 37 | 163,518 |
| `taxonomy` | 3 | 1,447,678 |
