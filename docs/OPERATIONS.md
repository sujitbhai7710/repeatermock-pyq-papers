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

* The work window (`MAX_WORK_SECONDS`, default 19,800 s) is checked between
  phases; when it expires the phase stops and the run ends with
  `status=time_limit`.
* `CHECKPOINT_INTERVAL_SECONDS` (default 1,800 s) controls how often the
  checkpoint is rewritten.
* `run` skips phases already present in `cursor.completed`; `--fresh` clears
  that behaviour for the current invocation.
* Every write is atomic, so a killed process cannot corrupt state.

## 4. Secrets

Set these as repository secrets for the workflow, or export them locally:

```bash
export OPENAI_KEYS="key1,key2"      # agentrouter key pool
export DEEPSEEK_KEYS="key1,key2"    # jw key pool
export AR_PROXY_TOKEN="..."         # single agentrouter bearer token
export JW_PROXY_TOKEN="..."         # single jw bearer token
export MONID_API_KEY="..."          # Monid TinyFish
```

Rules enforced by the code:

* keys are read **only** from the environment;
* no key is ever written to a file, a log line, a report or an artifact — the
  router masks them and the request history records at most `...abcd`;
* missing keys are not an error: the AI steps report
  `status=skipped_no_keys` and the python extraction is kept.

## 5. Rate limits — GLOBAL HALT

The router counts **consecutive** failures (one success resets the counter to 0).
A halt is triggered by either of:

* **10 consecutive failures** (`rate_limit.consecutive_failure_threshold`), or
* **any** response carrying a rate-limit / quota-exhaustion signal
  (`halt_on_any_rate_limit_signal`): HTTP 402 / 429 / 503, or a body containing
  `rate limit`, `rate-limit`, `ratelimit`, `too many requests`, `quota`,
  `exhausted`, `insufficient_quota`, `insufficient balance`, `no available`,
  `capacity`, `overloaded`, `temporarily unavailable`, `try again later`,
  `concurrency`.

On halt: the in-flight item finishes, `GlobalHalt` is raised for everything
after it, the phase checkpoints, and the run exits with
`status=rate_limited` (exit code `3`). The next scheduled run resumes from the
checkpoint.

## 6. CI (`.github/workflows/pyq-agent.yml`)

| Aspect | Value |
|---|---|
| Triggers | `workflow_dispatch` (optional single `phase`, optional `no_ai`) + `cron: 0 */6 * * *` |
| Timeout | `350` minutes |
| Concurrency | group `pyq-agent`, `cancel-in-progress: false` |
| Permissions | `contents: write` |
| Steps | checkout (`fetch-depth: 0`) → setup-python 3.11 → `pip install -r requirements.txt` (a no-op: stdlib only) → `python -m agent.cli run` with `MAX_WORK_SECONDS=19800` and `AR_PROXY_TOKEN` / `JW_PROXY_TOKEN` / `MONID_API_KEY` → `python -m agent.cli audit` (fails the job on a structural defect) → `database/_meta/PROGRESS.md` + audit transcript in the job summary → `upload-artifact` (logs, checkpoint, coverage; 14 days) → commit the generated tree with `git add -f state database` and `git push origin HEAD:pyq-db` (branch created on the first run, fast-forward afterwards) |
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
| `status=rate_limited` | a provider signalled rate limiting; wait for the next scheduled run — the checkpoint resumes automatically |
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
