# LESSONS.md — Problems hit on the SSC PYQ project, and exactly how to fix them

> Every entry: **symptom → cause → fix → how to verify**.
> Read this BEFORE debugging anything. Each of these cost real time once.

---

## L1. Every worker/direct API call returns `403 error code: 1010`

**Symptom**
```
ar-rotator /healthz -> 403 error code: 1010
jw-rotator /v1/chat/completions -> 403 error code: 1010
api.justwoker.icu/v1/messages -> 403 error code: 1010
```
**Cause** Cloudflare bot-protection bans the default Python `User-Agent`
(`Python-urllib/3.x`). Nothing is wrong with the keys.
**Fix** Send a real browser UA on **every** request:
```
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36
```
Implemented in `agent/util.py` (`BROWSER_USER_AGENT` / `user_agent()`, overridable with `PYQ_USER_AGENT`)
and used by `agent/llm.py` + `agent/websearch.py`.
**Verify** `curl -H "Authorization: Bearer <proxy>" -H "User-Agent: Mozilla/5.0" .../v1/models` → 200.

## L2. Positional subject mapping gave near-random results

**Symptom** Using `n` as the paper position produced nonsense section boundaries and ~0 % agreement.
**Cause** **`n` restarts at 1 in every section** (CGL 2024 Tier-I is `1..25, 1..25, 1..25, 1..25`).
It is a *within-section* number, not a paper position.
**Fix** Section identity comes from the index inside `questions[]` (global ordinal = index + 1);
plus year-aware layout rules. See `agent/sections.py` and `config/exams.json`.
**Verify** Re-run `phase0` and read the signature summary; agreement must be ≥ 95 %.

## L3. Paper-level `subject` is useless

**Symptom** 1,591 of 1,609 papers report `subject: "general"`.
**Fix** Derive the subject per question from position + `concept`/`tags`. Never read paper `subject`.

## L4. Hindi detector produced ~16,600 false positives

**Symptom** Scanning the whole record flagged 16,613 questions as Hindi instead of 3,456.
**Cause** Geometry solutions use the Devanagari **danda `।`** as a "parallel" symbol
(`।।`), which is in the Devanagari block.
**Fix** Scan **prompt + options only** (not `solution`), regex `[\u0900-\u097F]`. 3,456 correctly
skipped, of which 3,420 are the GD Hindi section. All four variants are reported in `coverage.md`.
**Verify** `coverage.md → skipped_hindi = 3456`.

## L5. `git push` rejected: file over 100 MB

**Symptom** `state/questions_index.jsonl` was **100.6 MB**; GitHub rejects any file > 100 MB, so the
checkpoint branch could never be published.
**Fix** Shard it: `state/index/<subject>.<n>.jsonl` (≤ 25 MB) + `state/index/manifest.json`
(per-shard records/bytes/sha256 + `qid_sha256`). `read_index()` is transparent over
shards / legacy / gzip. No generated file > 40 MB.
**Verify** `Get-ChildItem -Recurse -File | where Length -gt 40MB` → empty, and the sorted-qid sha256
is identical before/after sharding.

## L6. GitHub rejects the workflow: `Unexpected value 'timeout-minutes'`

**Symptom** `POST /actions/workflows/.../dispatches` → `422 failed to parse workflow: (Line: 26, Col: 1): Unexpected value 'timeout-minutes'`.
**Cause** `timeout-minutes` was written at the **top level** of the YAML (sibling of `on:`/`jobs:`).
It is only valid **inside a job**.
**Fix** Move it under the job:
```yaml
jobs:
  build-database:
    runs-on: ubuntu-latest
    timeout-minutes: 350      # <- must be here
```
**Verify** Re-dispatch; 422 must become 204. Also note a bad workflow shows an empty `name:` in
`GET /actions/workflows`.

## L7. `python -m agent.cli` → `No module named 'agent'`

**Symptom** The bundled interpreter (`C:\Program Files\AutoClaw\resources\python\python.exe`) is an
**isolated** build (`safe_path=True`): it ignores the current directory and `PYTHONPATH`, so it can
never import the local package.
**Fix** Use the system interpreter:
```powershell
$py = "C:\Users\akasa\AppData\Local\Programs\Python\Python311\python.exe"
& $py -m agent.cli stats          # from the project root
```
**Verify** `& $py -c "import sys; print(sys.path[0])"` resolves to the project root.

## L8. PowerShell: quoted executable paths and heredocs

**Symptom** `Unexpected token '"C:\...\python.exe"'` / `The string is missing the terminator: '@`.
**Cause** PowerShell needs the call operator for a quoted path, and multi-line here-strings are
fragile when assembled in one command.
**Fix**
```powershell
& $py "C:\path\to\script.py"          # call operator, not bare "quoted path"
```
and write multi-line scripts with a file-writing tool instead of inlining here-strings.
Never use `cmd /c`; use native cmdlets and single-quoted literal paths.

## L9. The live run died at 73 s with `GLOBAL HALT ... from jw`

**Symptom**
```
WARN GLOBAL HALT: rate-limit/exhaustion signal from jw: HTTP 503 ... {"type":"all_keys_exhausted"}
EXIT CODE: 1
```
**Cause — TWO compounding defects**
1. The router halted the **whole run** as soon as *one* provider reported exhaustion, even though
   `ar-rotator` was healthy.
2. `deepseek-v4-flash` is a **reasoning** model: with the small `max_tokens` used by a batch it burns
   the budget on `reasoning_content` and returns `content:""` with `finish_reason:"length"`. The
   client treated that client-side truncation as a *provider* failure → fell through to the dead jw → halted.
**Fix**
- Provider-aware failover: a provider-level exhaustion trips **that provider's** circuit breaker
  (cooldown 300 s, doubling to 3600 s) and the request continues to the next provider.
  `GlobalHalt` **only** when no healthy provider remains, or after 10 consecutive failures.
- Reasoning-truncation escalation: empty `content` + `finish_reason=length` ⇒ retry the *same*
  provider with a larger `max_tokens` (≥4096, cap 16384).
**Verify** With jw exhausted and ar healthy: `verify-db --limit 20` → exit 0, router reports
`{"agentrouter": {"ok": 4, "fail": 0}}`. With **only** the exhausted token → exit **3**, checkpoint
`status=rate_limited` (not 1).

## L10. A soft stop turned the GitHub job red

**Symptom** A clean rate-limit/time-limit stop (exit 3/4) made the whole CI job fail, so the audit
and checkpoint-publish steps never ran.
**Fix** `agent/cli.py` maps status → exit code (`ok→0`, `rate_limited→3`, `time_limit→4`, else 1), and
the workflow maps `0/3/4 → status=ok/rate_limited/time_limit` with `exit 0`. Any other non-zero still fails.
**Verify** Run the workflow's "Run the agent" shell block with stubbed exit codes 0/3/4/1 →
`ok/rate_limited/time_limit/failed`.

## L11. The generated tree had 167 duplicated nesting levels

**Symptom** Paths like `database/english/grammar/grammar/index.md`,
`database/computer/computer-fundamentals/computer-fundamentals/`, `.../antonym/antonym/`.
**Cause** When a concept resolved to the same name as its parent level, the writer still emitted a
child directory with that name.
**Fix** `leaf_levels()` in `agent/build_db.py` merges any level whose slug equals its parent's, and
`prune_subject_tree()` deletes the previous revision so stale dirs cannot linger. Result: 1,704 → 1,001 dirs.
**Verify** `audit` rule 1 = 0 violations; no `<x>/<x>/` path exists.

## L12. English grammar leaves got maths topic names

**Symptom** `database/english/at-on-and-in-as-prepositions-of-time/time-and-work/index.md` — a maths
topic under a grammar rule. Also 3,093 science questions under the GK chapter
*"Scientists, Inventions & Discoveries"*.
**Cause** A cross-subject keyword fallback matched (e.g. the word *Time* → maths `Time and Work`),
and the GK taxonomy parser let §39's concept set swallow the Physics/Chemistry/Biology concepts.
**Fix** `foreign_vocabulary()` guard (a label owned by another subject can never be matched via the
fuzzy path), `relocate_cross_chapter_concepts()` in `taxonomy.py`, and word-boundary matching
(kills `ratio` ⊂ "ope**ratio**n", `art` ⊂ "P**art**nership", `age` ⊂ "p**assage**").
**Verify** `audit` rule 3 = 0; GK histogram: *Scientists…* 3,201 → 32, Physics 181 → 754,
Chemistry 115 → 1,161, Biology 182 → 1,697.

## L13. Case-only duplicate concepts silently overwrote records

**Symptom** `Abandon` vs `abandon`; `To spill the beans` vs `spill the beans`; two leaves differing
only by case; a few pack ids colliding (1,176 catalogue entries vs 1,173 files).
**Fix** `fold_key()` (casefold + diacritics + whitespace, `to `-strip for idioms) used as the
counting/dedup key, with a display `variants` list; `_disambiguate_ids()` gives colliding mock pack
ids a digest suffix instead of overwriting.
**Verify** `audit` rule 7 = 0; `sum(variants)` per vocabulary table reproduces the pre-fix row count.

## L14. A leaf's concept did not belong to its chapter

**Symptom** 391 leaves / 15,944 records where the concept was not declared by the chapter.
**Fix** `ConceptScope.declares()` + `_align_parent()` re-point the leaf to the concept's owning
chapter; anything the taxonomy genuinely does not declare goes to an explicit **`_other`** level
(378 leaves / 12,800 records). `audit` rule 6 enforces it.
**Note** `_other` is the honest answer — do **not** invent taxonomy to make the number look better.

## L15. 27 `qid`s are reused by the source papers

**Symptom** One `qid` appears in two different leaves for two *different* questions.
**Cause** Source-data quirk (e.g. `6263156db633f426ea23fd96` is a GD 2021 Reasoning question and a
STENO 2025 Seating Arrangement question). Raw data is read-only.
**Fix** Audit rule 5 keys on question identity (`qid` + paper + ordinal) and reports qid reuse as a
**warning**, not a violation. Do not "fix" the data.

## L16. ZCode reported success — but the output had real defects

**Lesson** A green self-report is *execution evidence*, not verification. The first build passed its
own checks yet had 167 malformed directories and 19,767 audit violations. **Always** re-run the audit
and re-inspect with your own commands, and keep an independent reviewer for high-risk output.
**Fix** `tools/audit_db.py` now exists precisely so this class of defect is caught mechanically and
fails CI.

## L17. "Keys look masked" — they were not

**Symptom** The key file appeared to contain `sk-IRS…ivO0` (truncated).
**Cause** The *viewer* redacts secret-looking strings from tool output; the file on disk is complete.
**Fix** Have a script read the file and use the values without printing them; confirm by probing the
endpoint. Never re-paste secrets into chat.

## L18. Setting GitHub secrets needs libsodium

**Symptom** Setting a repo secret requires an encrypted value; a plaintext PUT does nothing useful.
**Fix** `pip install pynacl`, fetch the repo public key
(`GET /repos/{owner}/{repo}/actions/secrets/public-key`), sealed-box encrypt, then
`PUT /repos/{owner}/{repo}/actions/secrets/{name}` with `{encrypted_value, key_id}`.
**Verify** `GET .../actions/secrets` lists the names.

---

## L19. `ai_unavailable` usually means *no keys*, not *dead providers*

Symptom: `state/progress.json` shows `phase1.status = ai_unavailable`, and every probe answers
`401 Invalid or missing API key`, `403`, or `503 all_keys_exhausted`.

Cause: the runner had **no keys in its environment**. The endpoints were alive and answering with
well-formed JSON. A truly dead host returns an HTML WAF page or nothing — not a clean 401.

Fix: export the keys, then run `python -m agent.cli routes --probe` **before** concluding anything is
broken. With keys present, `ar-worker/deepseek-v4-flash`, `jw-worker/gpt-5.6-sol` and
`justwoker/gpt-5.6-sol` all report `ok`.

Rule: a clean `{"error":{"type":"unauthorized"}}` + HTTP 401 means **reachable, key missing/wrong**.
Only call a provider dead after you have sent it a *valid* key.

## L20. `grammar --ai` showing `0/20 answered` — look at who served the judge

Symptom: `batch 1/1 — deepseek-v4-flash via ar-worker proposed, gpt-5.6-sol via - judged, 0/20 answered`.

Cause: the **critic** had no healthy route (`via -` = no provider served it), so the final verdict was
empty and `parse_ai_reply()` found no `items`. The proposer had actually worked.

Fix: re-run it. The next attempt logged `gpt-5.6-sol via justwoker judged, 20/20 answered` and
assigned 17 rules. The pass is resumable, so a retry costs only time.

Rule: **`via -` in the `served` log is the tell.** A `0/N answered` batch is a route failure, not a
prompt or schema bug — do not start rewriting the schema hint.

## L21. "N already decided" in the grammar log is misleading

`grammar AI pass: 7210 pending, 7190 already decided, 1 batch(es) to send` does **not** mean 7,190
questions were decided earlier. The number is `len(pending) - len(outstanding)`, and `outstanding`
has already been truncated by `--limit`. With `--limit 20` it just prints `7210 - 20 = 7190`.

Rule: to see how much AI work is genuinely done, read `state/grammar_ai_state.json` and count
`questions` — never trust this log line.

## L22. Fresh clone: `pyq-db` is remote-only, and the shell does not stay in the repo

Two time-wasters:

- After a clone only `main` exists locally, so `git ls-tree pyq-db` fails with
  `fatal: Not a valid object name pyq-db`. Use **`origin/pyq-db`**.
- Each shell call starts in the workspace root, not the clone. Prefix commands with
  `cd /workspace/repeatermock-pyq-papers && …`, otherwise `git` answers
  `fatal: not a git repository` and the command appears to do nothing.

## L23. Restore the generated tree before you publish

`state/` and `database/` are gitignored on `main` and exist only on `pyq-db`. Publishing from a bare
clone commits just the runner's own tree, and the published `database/` then looks wiped.

Fix: `git archive origin/pyq-db state database | tar -x -C <repo>` first, then work, then `publish`
(which **merges**). Never publish from a tree you have not restored.

## L24. `publish` replaced local `main` with an orphan commit

Symptom: after `python -m agent.cli publish`, `git log` on `main` showed only 2–3 commits,
`git merge-base HEAD origin/main` returned **nothing**, and the next `git push` was rejected
`non-fast-forward`. `origin/main` still had all 53 commits.

Cause: publish stages `state/` + `database/` with `git add -f` (both are gitignored on `main`) and
lands them on an **orphan/root commit**. Local `main` then has no parent, so the real history is no
longer reachable from the branch tip — only the remote still has it. Local-only work (untracked
files, uncommitted edits) is what is actually at risk.

Recovery — back up the generated tree **first**, then rewind (never force-push):

```bash
cp -r state database tools/ config/settings.json MEMORY.md LESSONS.md /tmp/pqy_backup/
git reset --hard origin/main                              # drops the orphan, removes the tree
cp -r /tmp/pqy_backup/state /tmp/pqy_backup/database .    # put the generated tree back
```

Rules:
- Always copy `state/` and `database/` somewhere safe **before** any `git reset`, `checkout` or
  `rebase` here — that tree is hours of AI work and is gitignored on `main`.
- Recover with `reset --hard origin/<branch>`, never `git push --force`.
- Publish is the only thing that should stage `state/` and `database/`.

## L25. A cross-subject audit that compares *labels* finds 4,150 false positives

Symptom: `tools/cross_subject_audit.py` reported "cross-subject concept hits: 4150", led by
`math -> reasoning 'Ratio & Proportion'` (1,223) and `'Speed Time and Distance'` (1,524) — but the
tree was correct.

Cause: the check compared the raw **concept string** with the taxonomy's owning subject. SSC reuses
names across subjects on purpose: *Mathematical Operations* is a **Reasoning** chapter, so a
reasoning question with the concept *Ratio & Proportion* under it is filed correctly even though
"Ratio & Proportion" is also maths vocabulary. The label is shared; the **chapter** is owned.

Rules:
- Placement is decided by **chapter/topic ownership**, never by label text. A leaf is wrong only
  when its chapter is not declared by the filing subject (or, for a chapterless record, when the
  concept naming the leaf is vocabulary only another subject owns).
- Measure it the way the classifier does: the single source of truth is
  `agent.classify._subject_names` (via `Classifier.foreign_vocabulary`). If audit and classifier
  disagree, the next rebuild silently undoes a manual fix.
- Report `definitely wrong / ambiguous / ok`. On this corpus: **165 → 0 wrong**, ~11,000
  "ambiguous" (correct chapter, shared label) out of 138,634 records. Never bulk-move the ambiguous
  set — it is nearly all good data.
- A chapterless record such as `gk/_unclassified/verbal-ability` looks wrong but usually has the
  *subject* right and the **source label** wrong: read the question text first ("What best describes
  bulimia?" is GK, whatever the paper's tag says). The fix is to drop the bogus concept (keep
  `concept_raw`), never to re-file the subject.

## L26. Tests wrote 334 rows into the real AI error ledger

Symptom: after a test run, `state/errors.jsonl` (append-only, 104 real rows) had 438 rows with
`phase: "-"` — the failover tests simulate hundreds of route outages and the router records one
ledger row per failed route attempt.

Rules:
- `tests/__init__.py` points `PYQ_ERRORS_LEDGER` at a temp file, so a suite run can never append to
  the real ledger; `tests/test_error_ledger.py` asserts the real file is byte-identical afterwards.
- Any new writer of a shared append-only file needs the same isolation **and** a test that proves it.

## L27. An empty `questions.jsonl` is indistinguishable from a failed write

Symptom: 5 grammar rule leaves (`19-correlative-conjunctions`, `52-articles-with-joined-nouns`,
`100-even-if-vs-even-though`, `116-because-of-vs-due-to`, `128-emphatic-pronouns`) shipped an empty
`questions.jsonl` with no explanation — nobody could tell "the corpus never asks this" from "the
pass never ran".

Rules:
- An empty leaf must carry the marker `No PYQ in scope` in its `index.md`; `audit` rule 9 fails an
  unmarked empty leaf *and* a stale marker on a leaf that does have questions.
- The marker is emitted by the generator (`agent.grammar.render_rule_leaf`), so a regeneration
  cannot drop it.
- Before declaring a rule empty, search the residual pool with its own patterns: 4 of the 5 had
  free questions (19 → 14, 128 → 3, 100 → 1, 116 → 1). Rule 52's only real question is owned by the
  **keyword matcher** (rule 10), and a stored AI decision cannot override a matcher assignment
  (`agent/grammar.py`, the `item.match.rule is not None` branch) — that is a rules change, not a
  leaf fix.

## Quick troubleshooting index

| Symptom | Go to |
|---|---|
| `403 error code: 1010` on any API | L1 |
| Wrong subjects / sections | L2, L3 |
| Hindi count implausible | L4 |
| `git push` refused, big file | L5 |
| Workflow dispatch `422` | L6 |
| `No module named 'agent'` | L7 |
| PowerShell quoting / heredoc errors | L8 |
| Run dies with `GLOBAL HALT` | L9 |
| CI job red after a clean stop | L10 |
| Malformed `database/` paths | L11, L12 |
| Duplicate/overwritten questions | L13 |
| `audit` rule 6 failures | L14 |
| `qid` in two leaves | L15 |
| "It said it worked" | L16 |
| Secrets appear truncated | L17 |
| Cannot set a repo secret | L18 |
| `ai_unavailable` / all routes 401 | L19 |
| `0/N answered` after a batch | L20 |
| "N already decided" looks huge | L21 |
| `not a git repository` / `Not a valid object name pyq-db` | L22 |
| Published `database/` looks emptied | L23 |
| `main` lost its history / push non-fast-forward | L24 |
| Cross-subject audit floods you with "wrong" leaves | L25 |
| `state/errors.jsonl` grows after a test run | L26 |
| A rule leaf has 0 questions and nobody knows why | L27 |

## Conventions that keep this project healthy

1. Run `python -m agent.cli audit` after **every** change to the DB writer or classifier. It must print
   `VIOLATIONS: 0` (rules 1–11; rules 8–11 cover question uniqueness, empty-leaf markers,
   subject/chapter ownership and the unpublished `_analysis` view).
2. Run the unit tests: `python -m unittest discover -s tests -t .` (231 tests).
3. Keep the coverage identity at **142,090**; if a bucket moves, report the old and new numbers and why.
4. Never edit raw data. If the taxonomy is missing a concept, propose `config/chapter_aliases.json` for
   a human to review.
5. Never log keys. Never commit keys. Rotate anything that has been pasted into a chat.
6. Delegate repository-scale changes to the coding executor and then **verify independently** (L16).
7. Before moving a question, ask *who owns the chapter* — not what the label looks like (L25).
8. An AI failure must be explainable from `state/errors.jsonl` (`python -m agent.cli errors --top 20`).
