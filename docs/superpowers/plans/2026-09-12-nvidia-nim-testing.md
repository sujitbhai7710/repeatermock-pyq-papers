# NVIDIA NIM 6-Model Testing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build isolated AI-only speed+accuracy harness for 6 NIM models on same 10 papers with consensus scoring and HTML report.

**Architecture:** Manifest-driven; one Q per NIM call (temp 0.0); per-model runs; consensus scorer; static HTML report. Scripts only orchestrate IO — all verdicts come from NIM AI.

**Tech Stack:** Python 3.11 stdlib only, OpenAI-compatible POST to integrate.api.nvidia.com/v1.

**Spec:** docs/superpowers/specs/2026-09-12-nvidia-nim-model-testing-design.md

## Global Constraints

- New code lives ONLY in `model-testing-only/`; never edit `SSC-*/`, `chapter-and-topic/`, `state/`, `database/`.
- Keys ONLY from env `NVIDIA_NIM_KEYS`; never log or commit keys.
- temperature 0.0, top_p 1.0, max_tokens 1024, JSON-only, batch=1 per request.
- Throttle 40 RPM/key with round-robin + backoff on 429/503.
- Same 10 papers for all 6 models.
- AI-only verdicts: no keyword/deterministic classifier decides subject/chapter/rule.

---

### Task 1: Paper manifest (10 full papers)

**Files:**
- Create: `model-testing-only/build_manifest.py`
- Create: `model-testing-only/papers.jsonl`
- Test: `model-testing-only/test_manifest.py`

**Interfaces:**
- Consumes: `SSC-*/**/*.json` raw papers, `config/exams.json` layout not needed (use raw Qs as-is).
- Produces: `papers.jsonl` rows `{paper_id, exam, tier, year, path, qid, ordinal, question, options, answer}`.

- [ ] **Step 1: Write the failing test**

```python
def test_manifest_shape():
    import json
    rows = [json.loads(l) for l in open("model-testing-only/papers.jsonl", encoding="utf-8")]
    assert 900 <= len(rows) <= 2000
    r0 = rows[0]
    for k in ("paper_id","exam","year","qid","question","options","answer"):
        assert k in r0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest model-testing-only/test_manifest.py -v`
Expected: FAIL (file not found)

- [ ] **Step 3: Write minimal implementation**

```python
# model-testing-only/build_manifest.py — scans SSC-CGL/CHSL/CPO/Selection-Post,
# prefers years 2023-2025, picks 10 papers, dumps every Q with options+answer.
```

(Full script in execution; stdlib json/glob only; picks 4 CGL-T1 + 2 CGL-T2 + 2 CHSL + 1 CPO + 1 SelPost.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python model-testing-only/build_manifest.py` then `python -m pytest model-testing-only/test_manifest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-09-12-nvidia-nim-model-testing-design.md docs/superpowers/plans/2026-09-12-nvidia-nim-testing.md model-testing-only/build_manifest.py
git commit -m "feat: add NIM testing manifest builder"
```

### Task 2: NIM runner (6 models, AI-only)

**Files:**
- Create: `model-testing-only/nim_client.py`
- Create: `model-testing-only/run_model.py`
- Create: `model-testing-only/prompts.py`
- Test: `model-testing-only/test_nim_client.py`

**Interfaces:**
- Consumes: `papers.jsonl` rows; env `NVIDIA_NIM_KEYS`.
- Produces: `runs/<slug>/results.jsonl` rows `{qid, paper_id, subject, chapter, topic, rule, vocab_kind, verbal_kind, confidence, reason, latency_ms, model, error}`.
- Functions: `nim_client.chat(model, messages) -> (text, latency_ms)`; `prompts.build(qrow) -> messages`.

- [ ] **Step 1: Write the failing test**

```python
def test_prompt_has_taxonomy_and_answer():
    from prompts import build
    msgs = build({"question":"Select synonym of Abandon","options":["Leave","Keep"],"answer":"Leave","qid":"t1"})
    blob = msgs[1]["content"]
    assert "129" in blob and "synonym" in blob.lower() and "Leave" in blob
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest model-testing-only/test_nim_client.py -v`
Expected: FAIL (module missing)

- [ ] **Step 3: Write minimal implementation**

```python
# nim_client.py: urllib POST, Bearer nvapi-*, UA Mozilla/5.0, temp 0.0, response_format json_object, rotation + 1.6s throttle + 3 retries on 429/503.
# prompts.py: system = SSC classifier, full taxonomy slice (grammar 129 titles + vocab kinds + verbal kinds + math/reason/gk/computer hints), user = Q+options+answer, demand JSON keys.
# run_model.py: for each paper row, chat, extract_json, append results.jsonl with latency.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest model-testing-only/test_nim_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add model-testing-only/nim_client.py model-testing-only/prompts.py model-testing-only/run_model.py
git commit -m "feat: add NIM AI-only runner"
```

### Task 3: Consensus compare + per-subject stats

**Files:**
- Create: `model-testing-only/compare.py`
- Create: `model-testing-only/compare.json`
- Test: `model-testing-only/test_compare.py`

**Interfaces:**
- Consumes: `runs/*/results.jsonl`.
- Produces: `compare.json` `{per_model: {acc, avg_ms, total_s}, per_subject: {...}, consensus_dist, winner}`.
- Functions: `consensus(rows) -> key`; `score(model_rows, cons) -> float`.

- [ ] **Step 1: Write the failing test**

```python
def test_consensus_majority():
    from compare import consensus
    rows = [{"subject":"ENG"},{"subject":"ENG"},{"subject":"ENG"},{"subject":"ENG"},{"subject":"MATH"},{"subject":"GK"}]
    assert consensus(rows) == "ENG"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest model-testing-only/test_compare.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

```python
# compare.py: group by qid, majority subject+chapter/rule, per-model match%, per-subject match%, latency aggregates, winner pick.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest model-testing-only/test_compare.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add model-testing-only/compare.py
git commit -m "feat: add consensus scorer"
```

### Task 4: HTML report + audit

**Files:**
- Create: `model-testing-only/report.py`
- Create: `model-testing-only/report.html`
- Test: `model-testing-only/test_report.py`

**Interfaces:**
- Consumes: `compare.json` + `runs/*/results.jsonl`.
- Produces: `report.html` (leaderboard, heatmap, speed, methodology, disagreements).

- [ ] **Step 1: Write the failing test**

```python
def test_report_exists():
    import pathlib
    assert pathlib.Path("model-testing-only/report.html").exists()
    assert "Leaderboard" in pathlib.Path("model-testing-only/report.html").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest model-testing-only/test_report.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

```python
# report.py: inline-CSS HTML, tables for leaderboard + per-subject, bar widths inline, no external assets.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python model-testing-only/report.py` then `python -m pytest model-testing-only/test_report.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add model-testing-only/report.py model-testing-only/report.html
git commit -m "feat: add NIM comparison report"
```
