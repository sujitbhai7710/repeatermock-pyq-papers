# TASK-PROMPT.md — paste this into the coding agent

> Copy everything below the line into `opencode` (or any agent) inside the Codespace.
> Read `AGENT-GUIDE.md` first; it has the full context. `AI-APIS.txt` has the provider details.

---

You are working in the repository `repeatermock-pyq-papers`. Read `AGENT-GUIDE.md`, `MEMORY.md` and
`LESSONS.md` at the repo root before you change anything.

**What this project is:** SSC previous-year papers (JSON, already in the repo) turned into a
topic-wise database. Scope is locked to **papers 2019–2025 → 1,322 papers / 142,090 questions**.
The deterministic database is **already built and published**; what is left is the **AI layer**,
notes, and a set of small data fixes.

## Hard rules — breaking any of these is a bug

1. `python -m agent.cli audit` must print **`VIOLATIONS: 0`** after every change.
2. `python -m unittest discover -s tests -t .` must stay green (currently **260 tests**).
3. Coverage identity must always balance:
   `placed + skipped_hindi + unclassified + flagged == 142090` (today: 130020 + 3456 + 8614 + 0).
4. **Never edit raw data**: `SSC-*/**/*.json` and `chapter-and-topic/*.md` are read-only.
5. Everything must be **idempotent** — re-running a step changes nothing.
6. **Never log or commit API keys.** They are in the environment / Codespaces secrets.
7. Publishing to the `pyq-db` branch must **merge** — it must never delete published files.
   `python -m agent.cli publish` already does the right thing; use it instead of your own copy.

## Work queue — in this order

### 0. Orient (2 minutes)
```bash
python -m agent.cli routes --probe     # which AI routes are alive RIGHT NOW
python -m agent.cli audit              # expect VIOLATIONS: 0
python -m agent.cli errors --top 20    # why the AI failed last time
```

### 1. Make the AI layer actually run
Every phase currently reports `ai_unavailable` and English sits at **620 / 37,990**.
- Run `python -m agent.cli routes --probe`, then `python -m agent.cli errors --top 20`, and fix the
  routing so a working route is used **first** (the verified order is in `AI-APIS.txt` §8).
- Then run the verification pass: `python -m agent.cli run` (5.5 h window, checkpoint every 15 min).
- A single provider failing must never kill the run — stop only after 6 consecutive failures, and
  always checkpoint first. If **no** route is healthy: finish the deterministic work, save, exit 0
  with `status=ai_unavailable`.

### 2. Grammar: finish the distribution
- `python -m agent.cli grammar --ai` — assign the remaining unassigned questions to one of the 129 rules
  (DeepSeek proposes, `gpt-5.6-sol` judges). Resumable; re-run until the counters stop moving.
- **Re-open rule 52 (`52-articles-with-joined-nouns`)**: its leaf is marked *"No PYQ in scope"*, but a
  scan found ~42 grammar questions matching its pattern (`… and the …`, i.e. the article repeated before
  each joined noun). Strongest candidate (found by an independent audit): qid `5e8fbf903ab0500d2e510c26`,
  CGL 2019/2020 — *"The famous author and actor are being honoured…"*, whose own solution states the
  article-once / same-person rule verbatim; it currently sits under rule **10**. Verify with the AI whether these belong to rule 52, assign them if so, and only
  keep the "No PYQ" marker if the model agrees none match.

### 3. Review the `_unclassified` buckets
`database/<subject>/_unclassified/` holds questions the classifier could not place (e.g.
`gk/_unclassified/analogy`, `reasoning/_unclassified/vocabulary`). The audit intentionally exempts these
holding buckets — **they are your AI review queue.** For each: ask the model to confirm or correct the
subject; move anything that clearly belongs to another subject into that subject's tree.

### 4. Notes
- GK/GS notes exist for 563 topics and grammar notes for 134 rules.
- **Maths and Reasoning have none.** Generate them in the same format
  (`tools/gen_notes.py` is the existing generator — follow it).

### 5. Speed
Grammar batches are already parallel. Make the **verification** phase parallel too (a thread pool,
at most one in-flight batch per `(provider, model)`), so the 5.5 h window does more work.

### 6. Housekeeping
- `state/errors.jsonl` + `python -m agent.cli errors` is your evidence file — extend it if you find a
  failure that is not recorded.
- Keep `AGENT-GUIDE.md` (per-subject numbers, what's done/left) and `LESSONS.md` (new incidents →
  symptom, cause, fix) up to date as you go.

## Definition of done
- `audit` → `VIOLATIONS: 0` and `unittest` green.
- The AI verification counter is **moving** (not stuck at 440) and `errors` explains any stall.
- The `_unclassified` review queue is reduced, with every move justified by the model.
- Maths + Reasoning notes exist.
- Everything published to `pyq-db` via `python -m agent.cli publish`.

## When you finish a step
Say exactly: what you changed, the commands you ran with their exit codes, the audit line, the test
summary, and the commit hash. If something cannot be done, say so plainly rather than guessing.
