# NVIDIA NIM Model Testing — Design (2026-09-12)

## Goal
Speed + accuracy comparison of 6 NVIDIA NIM models on the SAME SSC papers,
AI-only classification (no deterministic script for verdicts), with per-subject
stats and a detailed HTML report.

## Models (6)
1. nvidia/nemotron-3.5-lightning-30b-a3b
2. openai/gpt-oss-20b
3. google/gemma-4-31b-it
4. google/diffusiongemma-26b-a4b-it
5. mistralai/mistral-nemotron
6. meta/muse-glimmer-30b

Endpoint: `https://integrate.api.nvidia.com/v1/chat/completions` (OpenAI-compatible).
Keys: 3x `nvapi-*` via env `NVIDIA_NIM_KEYS` (comma-separated, never committed).
Throttle: 40 RPM/key -> round-robin + 1.6s min gap, backoff on 429/503.

## Papers
10 full papers (approved), manifest `model-testing-only/papers.jsonl`:
4x CGL Tier-1, 2x CGL Tier-2 Paper-1, 2x CHSL Tier-1/2, 1x CPO Paper-1,
1x Selection Post. Years 95% 2023-2025. Skip MTS/GD/Steno.
Same papers for all models. Each Q carries question+options+answer/solution.
Scalable to 100 papers later without code change (manifest-driven).

## Task per question (AI-only)
Full taxonomy, temperature 0.0, top_p 1.0, max_tokens 1024, JSON-only, batch=1:
- ENG grammar -> 1..129 rule (from english-grammar-rules.md)
- ENG vocab -> synonym/antonym/ows/idiom/spelling/homonym
- ENG verbal -> cloze/parajumble/comprehension/fill-blanks/phrasal-verb/narration/voice
- MATH -> 30 chapters, REAS -> 77 topics/5 families, GK -> domains 0-106, COMPUTER -> 9 nodes
Prompt includes taxonomy slice + Q + options + answer for best accuracy.

## Isolation
New folder `model-testing-only/` only. Reads `SSC-*/**/*.json` + 4 MDs read-only.
Never writes to `state/`, `database/`, `chapter-and-topic/`.
Layout:
- `model-testing-only/papers.jsonl` (manifest)
- `model-testing-only/runs/<slug>/results.jsonl` (per-model, per-Q verdict + latency/tokens)
- `model-testing-only/compare.json` + `report.html`

## Scoring (consensus = accuracy)
No human gold set. Majority vote across 6 models per Q:
>=4 agree = high-confidence, 3 = disputed, no majority = divergent.
Per-model accuracy = % match with consensus. Per-subject breakdown.
Speed = total time, avg ms/Q, Q/s. Winner = highest consensus-match.

## HTML report
`model-testing-only/report.html`: leaderboard, per-subject heatmap,
time-to-10-papers, consensus distribution, 429/failure counts,
sample disagreements, full config + methodology.

## Self-review
- No placeholders; RPM math checked (7800 calls @90RPM safe ~= 90min worst case).
- No scope creep: 10 papers now, 100 later via same manifest.
- No key leaks: env only, masked logs.
- AI-only verdicts honored: scripts only orchestrate IO, never classify.
