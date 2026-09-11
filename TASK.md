# TASK.md — standing task for the OpenHands Cloud agent

> This is the handover brief. Read it, then read `MEMORY.md` and `LESSONS.md` in this repo.
> **Do not edit raw data** (`SSC-*/**/*.json`, `chapter-and-topic/*.md`).

## Repository

**`sujitbhai7710/repeatermock-pyq-papers`**, branch **`main`**.
A Codespace/dev-container alternative was removed; OpenHands Cloud is the remote worker now.

## What this project is

Turns SSC previous-year question papers (already in the repo) into a **topic-wise database**,
plus trends, priority lists, notes and mock-generator-ready packs.

- Scope is locked to papers **2019–2025** → **1,322 papers / 142,090 questions**
- Subjects: English, GK/GS, Maths, Reasoning, **Computer** (CGL Tier-II)
- Hindi (Devanagari) questions are **skipped** and never sent to an LLM
- Pipeline order is fixed: **English → GK/GS → Maths → Reasoning → Computer**

## What is ALREADY done (do not redo)

- `phase0` discovery, subject-by-position mapping, Hindi split, signature validation (1,318/1,322 validated)
- Sharded index (`state/index/*.jsonl`, ≤25 MB per shard)
- Distribution, trends, priority files, mock packs
- **English grammar fully distributed across the 129 combined rules**
  (`database/english/grammar/<NN>-<slug>/`), one home per question
- Structural audit passes: `VIOLATIONS: 0`
- 191 unit tests pass

## What to do next (in this order)

1. **Close the grammar residual.** `python -m agent.cli grammar --ai` assigns the remaining
   unassigned grammar questions to a rule (deepseek proposes, gpt-5.6-sol judges). It is resumable —
   run it repeatedly until `unassigned` stops shrinking.
2. **Run the AI verification pass** over the pipeline: `python -m agent.cli run`
   (or `python -m agent.cli verify-db --phase phase1`). It verifies every python-extracted item,
   fixes wrong ones, and stops cleanly (leaving a checkpoint) when a provider dies.
3. **Close the 4 flagged papers** (`database/_meta/flagged_papers.jsonl`).
4. **Work the `unclassified` bucket** (`8,614` questions): the AI clusters them and, when ≥2 share a
   logic pattern, a new concept/rule is minted. Anything left keeps a reason code.
5. **Publish**: `python -m agent.cli publish` (or let the GitHub Action do it) → `pyq-db` branch.

## Hard rules (breaking these is a bug)

1. `placed + skipped_hindi + unclassified + flagged_papers_questions == 142090` must always balance.
2. `python -m agent.cli audit` must print **`VIOLATIONS: 0`**. Never commit with a failing audit.
3. `python -m unittest discover -s tests -t .` must stay green.
4. Everything must be idempotent — re-running a phase changes nothing.
5. No raw-data edits. If the taxonomy lacks a concept, propose `config/chapter_aliases.json` instead.
6. Never print, log or commit keys. Secrets are provided by OpenHands Cloud (see `OPENHANDS.md`).

## Commands

```bash
python -m agent.cli taxonomy      # parse chapter-and-topic/*.md
python -m agent.cli phase0        # discovery + Hindi split + index + distribution
python -m agent.cli run           # full pipeline (checkpoint every 15 min)
python -m agent.cli run --no-ai   # deterministic only
python -m agent.cli grammar --ai  # grammar distribution + AI judge pass
python -m agent.cli routes --probe  # which AI routes are alive right now
python -m agent.cli audit         # must print VIOLATIONS: 0
python -m agent.cli stats --top 20
python -m agent.cli mocks
python -m unittest discover -s tests -t .
```

## Model policy

- **`deepseek-v4-flash` is free on OpenHands** — use it for all coding/agent work.
- Our own keys (`AGENTROUTER_KEYS`, `JUSTWOKER_KEYS`, `AR_PROXY_TOKEN`, `JW_PROXY_TOKEN`) are for the
  **pipeline's own AI calls** (analysis/verification/debate), where `gpt-5.6-sol` is the judge.
- `MONID_API_KEY` gives the pipeline web search/fetch.
- If every AI route is down the run must **stop after saving** and record `ai_unavailable`.
