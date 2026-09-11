# Operations

## 1. First run

```bash
cd repeatermock-pyq-papers
python -m agent.cli taxonomy       # ~5 s   -> state/taxonomy.json, state/alias_map.json
python -m agent.cli phase0         # ~20 s  -> index, distribution, coverage
python -m agent.cli stats          # per-exam / per-subject counts + top concepts
python -m agent.cli run            # phases 0-5, honours checkpoint + work window
python -m agent.cli mocks          # mock pack catalogue
python -m agent.cli audit          # structural gate over database/ (exit 0 = clean)
```

`run` calls `taxonomy` implicitly only in the sense that it *needs*
`state/taxonomy.json` and `state/alias_map.json` to exist; if they are missing it
warns and continues with empty taxonomy data (everything becomes
`unclassified`). Always run `taxonomy` first, or use the CI workflow which does.

## 2. Command reference

| Command | Purpose |
|---|---|
| `taxonomy [--from-cache]` | parse the four MD files; `--from-cache` reuses `state/labels.json` instead of re-reading 1,322 papers |
| `phase0` | discovery, Hindi split, signature validation, index, distribution, `_meta` |
| `phase N` | one phase (0-5) |
| `run [--phase N] [--fresh] [--no-ai]` | full pipeline; `--fresh` ignores the checkpoint, `--no-ai` skips every LLM step |
| `verify-db [--phase] [--limit] [--batch-size] [--report-only]` | resumable AI re-check of extracted items |
| `stats [--top N] [--json]` | counts and top concepts |
| `mocks [--pack-size N] [--min-size N] [--max-packs N]` | rebuild the mock catalogue |
| `audit [--json] [--database DIR]` | structural self-check of `database/` (rules 1-7: duplicated nesting, stray chapter `index.md`, English cross-subject vocabulary, empty/placeholder slugs, a question filed twice, a leaf concept the chapter does not declare, case-only leaf/pack collisions); non-zero exit on any violation |
| `--quiet` | only log errors |

Exit codes: `0` ok, `1` error, `2` usage, `3` rate-limited, `4` time limit.

## 3. Checkpoint & resume

```text
state/checkpoint.json  { run_id, phase, cursor: {completed: [...], next: ...}, status, window }
state/progress.json    phases.<name>.{status, counters, notes}
state/journal.jsonl    append-only event log
state/manifest.json    files produced per phase (path, bytes)
database/_meta/PROGRESS.md  human-readable roll-up of the above
```

* The work window (`MAX_WORK_SECONDS`, default 19,800 s) spans the whole `run`
  and is checked before every batch and phase; when it expires the run stops
  cleanly with `status=time_limit` (exit code 4) after checkpointing — a started
  batch is always allowed to finish.
* `CHECKPOINT_INTERVAL_SECONDS` (default 1,800 s) controls how often the
  checkpoint is rewritten.
* `run` skips phases already present in `cursor.completed`; `--fresh` clears
  that behaviour for the current invocation.
* Every write is atomic, so a killed process cannot corrupt state.

## 4. Secrets

Set these as repository secrets for the workflow, or export them locally:

```bash
export AGENTROUTER_KEYS="key1,key2"   # direct agentrouter.org key pool
export JUSTWOKER_KEYS="key1,key2"     # direct api.justwoker.icu key pool (Anthropic Messages)
export AR_PROXY_TOKEN="..."           # ar-rotator worker bearer token
export JW_PROXY_TOKEN="..."           # jw-rotator worker bearer token
export MONID_API_KEY="..."            # Monid TinyFish
# historical aliases, still accepted:
#   OPENAI_KEYS  -> agentrouter
#   DEEPSEEK_KEYS -> justwoker
```

Rules enforced by the code:

* keys are read **only** from the environment;
* no key is ever written to a file, a log line, a report or an artifact — the
  router masks them and the request history records at most `...abcd`;
* missing keys are not an error: the AI steps report
  `status=skipped_no_keys` and the python extraction is kept.

## 5. Rate limits — route failover + GLOBAL HALT

Each request walks the route order configured in the `providers` block of
`config/settings.json` (`providers.order`, default
`agentrouter` → `ar-worker` → `jw-worker` → `justwoker`; per-role overrides in
`debate.proposer_provider_order` / `debate.critic_provider_order`, and per-model
overrides in `debate.model_provider_orders`). A route that answers with a
rate-limit / quota-exhaustion signal
(`halt_on_any_rate_limit_signal`): HTTP 402 / 429 / 503, or a body containing
`rate limit`, `rate-limit`, `ratelimit`, `too many requests`, `quota`,
`exhausted`, `all_keys_exhausted`, `insufficient_quota`, `insufficient balance`,
`no available`, `capacity`, `overloaded`, `temporarily unavailable`,
`try again later`, `concurrency` — is treated as a **route-level outage**:

* its circuit breaker trips for `rate_limit.provider_cooldown_seconds`
  (default 300 s, doubling per consecutive trip up to
  `provider_cooldown_max_seconds`, default 3,600 s);
* the request fails over to the next route **for the same model**;
* a tripped route is skipped while cooling down (not retried per item) and is
  probed exactly once after the cooldown — success closes the breaker, failure
  re-opens it with a doubled cooldown;
* `rate_limit.provider_error_strike_limit` (default 3) consecutive hard errors
  (`401`, `403`, `5xx`, transport failures) open the same breaker.

Breakers are keyed by **`(provider, model)`** — a worker that 503s
`deepseek-v4-flash` keeps serving `gpt-5.6-sol`.

A model whose providers are *all* down falls back to the next **candidate model**
of its role (`debate.proposer_models` / `debate.critic_models` in
`config/settings.json`, ordered, primary model first; override without editing the
file with `PYQ_PROPOSER_MODELS` / `PYQ_CRITIC_MODELS`). A request walks the
candidates outermost-first — for every candidate model, every provider in that
model's order — and the serving pair is recorded in the verdict provenance
(`proposer=…@…`, `critic=…@…`) and counted in `RouterStats.model_fallbacks`.
That is what keeps a runner green where the direct `agentrouter` endpoint is
blocked by the Aliyun WAF, both workers report `all_keys_exhausted` for
`deepseek-v4-flash` and direct `justwoker` answers 403: `jw-worker` still serves
`gpt-5.6-sol`, so the debate runs instead of being skipped with
`status=ai_unavailable`.

When the only working model ends up serving both roles the run proceeds but says
so: `same_model_fallback: true` lands in the verdict provenance, the phase
counters (`batches_same_model_fallback`) and `PROGRESS.md`.

The router counts **consecutive** failures per model (one success resets the
counter). A **global halt** happens only when no `(provider, model)` candidate is
healthy for a role:

* every candidate route of the role is exhausted / cooling down / without
  credentials, or
* **10 consecutive failures** (`rate_limit.consecutive_failure_threshold`) were
  recorded per model and no route absorbed them.

On halt the AI step stops for that model; the deterministic pipeline keeps
going. Phases finish normally, `database/` and the mock catalogue are written,
the checkpoint records `status=ai_unavailable`, and the run exits **0** (the AI
outage is not a failure). Every phase in `database/_meta/PROGRESS.md` reports
items total / done / % / this run / remaining / ETA plus the per-route health
(ok / fail / rate-limited / cooldown / retry-in) and the last checkpoint time.
`status=rate_limited` (exit 3) remains a legacy soft stop; `status=time_limit`
(exit 4) is still how a spent work window ends a run.

## 6. CI (`.github/workflows/pyq-agent.yml`)

| Aspect | Value |
|---|---|
| Triggers | `workflow_dispatch` (optional single `phase`, optional `no_ai`) + `cron: 0 */6 * * *` |
| Timeout | `350` minutes |
| Concurrency | group `pyq-agent`, `cancel-in-progress: false` |
| Permissions | `contents: write` |
| Steps | checkout (`fetch-depth: 0`) → setup-python 3.11 → `pip install -r requirements.txt` (a no-op: stdlib only) → `python -m agent.cli routes --probe` (route matrix into the job summary, `continue-on-error`) → `python -m agent.cli run` with `MAX_WORK_SECONDS=19800`, `CHECKPOINT_INTERVAL_SECONDS=900`, `PYQ_GIT_PUSH=1`, `PYQ_DB_BRANCH=pyq-db` and `AGENTROUTER_KEYS` / `JUSTWOKER_KEYS` / `AR_PROXY_TOKEN` / `JW_PROXY_TOKEN` / `MONID_API_KEY` (exit 0 = ok **or ai_unavailable**, 3 = rate_limited, 4 = time_limit are all success; the run step maps them to `status=…` and exits 0 so the audit + publish steps run) → `python -m agent.cli audit` (fails the job on a structural defect) → checkpoint status + `database/_meta/PROGRESS.md` + audit transcript in the job summary → `upload-artifact` (logs, checkpoint, coverage; 14 days) → commit the generated tree with `git add -f state database` and `git push origin HEAD:pyq-db` (branch created on the first run, fast-forward afterwards; a safety net — the agent already published every 15 minutes via `PYQ_GIT_PUSH=1`) |
| Publishing | `agent/gitpush.py`: on every checkpoint tick (throttled to `CHECKPOINT_INTERVAL_SECONDS`) commit `state` + `database` with `pyq-agent: checkpoint <phase> <done>/<total> [skip ci]`, re-parent onto the existing `pyq-db` tip so the push is a fast-forward, and push. Uses the credentials `actions/checkout` persisted (optional `PYQ_PUSH_TOKEN` override, never logged); `.tmp-*.part` files are deleted/unstaged; a failed commit or push is a warning, never a failed run. Also available as `python -m agent.cli publish [--force]` |
| Branches | `main` is code only (`/state/` and `/database/` are gitignored); all generated artefacts live on `pyq-db` and are marked generated in `.gitattributes` |

Local sanity check of the workflow logic:

```bash
python -m agent.cli run --fresh --no-ai     # full python-only run, no keys needed
```

## 7. Performance

Measured on the full corpus (1,322 papers, 142,090 questions):

| Step | Time |
|---|---|
| `taxonomy` (cold, reads every paper for labels) | ~5 s |
| `taxonomy --from-cache` | ~0.2 s |
| `phase0` | ~20 s |
| `phase1` (ENG, includes vocabulary + grammar) | ~12 s |
| `phase2` (GK, 34,578 questions, ~2,000 files) | ~5 s |
| `phase3` / `phase4` / `phase5` | ~3 s / ~3 s / ~2 s |

Dominant cost is JSON parsing of the source papers (once per phase that needs
question text). `phase0` reads every paper once.

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `No module named 'agent'` | you are not in the project root, or you are using an isolated Python build (`sys.flags.safe_path` is `True`, typical for interpreters bundled inside GUI apps). Use a regular CPython from the repo root, or `PYTHONPATH=. python -m agent.cli …` |
| `taxonomy/alias map missing` warning | run `python -m agent.cli taxonomy` first |
| Everything is `unclassified` | the alias map was built against a different label inventory; rebuild with `taxonomy --from-cache` (or without it) and re-run `phase0` |
| No worker secret is configured | the job is a no-op dry run: it writes `status=skipped_no_keys` to the summary and exits 0 |
| A paper is flagged `NEEDS_AI_REVIEW` | expected for 4 papers (1 layout mismatch + 3 with no usable labels); inspect `database/_meta/flagged_papers.jsonl` and confirm with `verify-db` |
| `audit` fails | the tree has a structural defect; the report names the rule, the path and the detail. Re-run the affected phase (`python -m agent.cli phase N`) — the subject subtree is pruned and rewritten |
| `coverage identity is NOT balanced` | a paper's `placeable` flag and its per-question `subject` disagree; the warning prints both sides. Re-run `phase0` and check `state/papers.json` |
| `status=rate_limited` | every configured provider is exhausted/rate limited; the circuit breaker skips the outages (a single healthy provider keeps the run going), the checkpoint resumes automatically. Exit code 3 is a soft stop — the job stays green |
| `unittest` reports `Start directory is not importable` | run with `-t .` from the project root: `python -m unittest discover -s tests -t .` |
| Mock pack id not found | ids are hierarchical slugs; use `python tools/mocks.py list --search <text>` |

## 9. Verification checklist

```bash
python -m agent.cli taxonomy            # exit 0, prints chapter/topic/concept counts
python -m agent.cli phase0              # exit 0, prints the coverage line + balanced=True
python -m agent.cli stats               # per-exam/per-subject matrix + top 20 concepts
python -m agent.cli mocks               # exit 0, prints pack counts per kind
python -m agent.cli audit               # exit 0, VIOLATIONS: 0
python -m unittest discover -s tests -t .   # exit 0, all tests
python tools/resolve.py <qid>           # exit 0, prints the full question
git status --porcelain                  # only added paths + the replaced README
```
