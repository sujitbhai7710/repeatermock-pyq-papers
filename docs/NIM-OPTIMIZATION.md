# NIM benchmark: why gemma-4 / diffusiongemma are slow, and what can actually be done

Analysis date: 2026-09-12 · Target: finish **11,600 questions × 4 NIM models in 3–4 hours**
without giving up accuracy.

---

## 1. Measured facts (from the harness itself)

| Source | Value |
|---|---|
| `model-testing-only/papers.jsonl` | **11,600 questions** |
| `compare100.json` | only **100** questions actually run so far |
| Slow model | **avg 115,134 ms = 115 s/question** (one row: 5 questions → 575 s) |
| Faster model | avg 26,566 ms ≈ 27 s/question → 100 questions in 850 s |
| Overall agreement accuracy | 0.844 |

Harness settings in `run_model.py` / `nim_client.py`:

| Setting | Value |
|---|---|
| Batch size | **1** (accuracy-first; batch=8 measured **0/16 chapter-exact**, batch=2 **1/8 exact / 8/8 same subject**) |
| Workers | **10** default, **6** for gemma-4 and diffusiongemma (503 caps) |
| Rate limiter | **30 RPM per key**, 5 keys → **150 requests/minute** shared |
| `reasoning_effort: low` | applied only to `gpt-oss-20b` and `muse-glimmer-30b` (measured safe: muse **137 s → 15 s**) |

---

## 2. The arithmetic — what is and isn't possible

Total requests for one full pass = 11,600 × 4 models = **46,400**.

| Constraint | Throughput | Time for 46,400 |
|---|---|---|
| Current limiter (5 keys × 30 RPM) | 150/min | **≈ 5 h 10 m** |
| Free-tier max (5 keys × **40** RPM) | 200/min | **≈ 3 h 52 m** ✅ |
| One more key (6 × 40) | 240/min | ≈ 3 h 13 m |
| Two more keys (7 × 40) | 280/min | ≈ 2 h 45 m |

**So the 3–4 h goal is reachable — but only by raising the pacing, not by making the slow models faster.**

### Why the slow models are unfixable

| Model | Measured | Requests/min at 6 workers | Time for 11,600 |
|---|---|---|---|
| `gemma-4-31b-it` | 115–236 s | ~2 | **≈ 90–160 hours** |
| `diffusiongemma-26b` | ~34 s | ~11 | **≈ 18 hours** |

Their cost is **architectural**, exactly as you diagnosed:

- **gemma-4** is a reasoning model — the wall-clock is internal chain-of-thought, not output (57 output tokens for 2–4 minutes).
- **diffusiongemma** is a **diffusion** LM: it denoises the whole sequence over many iterative passes. Published measurements put masked-diffusion inference at **16×–4,700× the FLOPs of an autoregressive model per task** ([arXiv 2511.03276](https://arxiv.org/html/2511.03276v1)). A ~30 s fixed floor per request is the signature — 90 tokens out, 34 s in.

No prompt flag, batch size, or worker count removes that. Workers only parallelise the waiting, and you're already at the 503 concurrency ceiling.

---

## 3. Optimisations that DO work (none of them touch accuracy)

### 3.1 Raise the limiter — the single biggest win, zero risk
`nim_client.py` paces at **30 RPM/key** while the free tier allows **40** ([NVIDIA forums](https://forums.developer.nvidia.com/t/request-for-nvidia-nim-api-rate-limit-increase-40-to-200-rpm/374542) — 40 RPM is the free default, and increases are not granted on free tier).
30 → 40 per key is **+33 % throughput for free**. This alone takes 5 h 10 m → **3 h 52 m**.

### 3.2 Turn thinking OFF for the slow models — if the endpoint honours it
vLLM documents the switch: `chat_template_kwargs: {"enable_thinking": false}`
([vLLM reasoning outputs](https://docs.vllm.ai/en/latest/features/reasoning_outputs/)); for gpt-oss the OpenAI-style `reasoning_effort` is the supported knob.
**Verified already:** `reasoning_effort:low` cut muse-glimmer from **137 s → 15 s** with parsing intact.
**Not yet verified:** gemma-4 — the harness marks it "untested → NOT applied (accuracy first)". This is the highest-value experiment left: if gemma-4 honours `enable_thinking:false`, its 2–4 minutes of CoT disappears.

> My own live attempt to measure this was **inconclusive**: the first gemma-4 call ran 28 s and then the endpoint returned `RemoteDisconnected`, and every subsequent call was refused (`URLError`) — consistent with the throttling/503 stalls you measured, not with a rejected parameter. It needs to be re-run **through the harness's own 30/40-RPM limiter and retry logic**, not from an unthrottled script.

### 3.3 Streaming + longer client timeouts
`RemoteDisconnected` after 28 s means the connection is being dropped mid-generation on the long calls. Using `stream: true` (or a keep-alive) lets you receive tokens and avoid the disconnect, and it stops the slow models from holding a worker hostage on a dead socket.

### 3.4 Stop running all four models over all 11,600 questions
This is the honest structural answer. You do not need a 4-model ensemble on every question:

| Model | Workload |
|---|---|
| `gpt-oss-20b` (fast, low effort) | **all 11,600** |
| `muse-glimmer-30b` (fast, low effort) | **all 11,600** |
| `gemma-4-31b-it` | **stratified sample** (e.g. 500) for agreement |
| `diffusiongemma-26b` | **stratified sample** (e.g. 500) for agreement |

That keeps the cross-model agreement signal (which is what the ensemble is for) at ~**1/20th** of the cost, and the fast pair covers the corpus. Total ≈ 23,200 + 1,000 requests → at 200 RPM ≈ **2 h**.

### 3.5 Keep batch = 1
Your own measurement says batching costs chapter precision (batch=2 → 1/8 exact). Since the wall-clock is RPM-bound for the fast models, batching is not needed for the 3–4 h target.

---

## 4. Recommended configuration

```python
# nim_client.py
RPM_PER_KEY = 40                      # was 30 - free-tier max, +33% throughput

# run_model.py
WORKERS = 12                          # fast models, within the 503 cap
WORKERS_PER_MODEL = {"google/gemma-4-31b-it": 6, "google/diffusiongemma-26b-a4b-it": 6}
RE_EFFORT = {
    "openai/gpt-oss-20b":  {"reasoning_effort": "low"},          # verified safe
    "meta/muse-glimmer-30b": {"reasoning_effort": "low"},        # verified safe: 137s -> 15s
    # candidates, pending a parse+accuracy check through the limiter:
    # "google/gemma-4-31b-it": {"chat_template_kwargs": {"enable_thinking": False}},
}
SAMPLE_ONLY = {"google/gemma-4-31b-it", "google/diffusiongemma-26b-a4b-it"}   # ~500 stratified
```

**Expected outcome:** full corpus on the two fast models + a 500-question agreement sample on the slow two ≈ **under 3 hours**, with no accuracy change. All four models over all 11,600 in 3–4 h is **not** achievable — the slow pair alone needs ~90–160 h.

---

## 5. What this means for the real pipeline

The production pipeline does not need a 4-model ensemble. Per `AGENT-GUIDE.md` §7 it needs **one proposer + one judge**, and we already have a fast, verified verifier: **Groq `openai/gpt-oss-120b`** (HTTP 200, low latency). NIM's `gpt-oss-20b` / `muse-glimmer` are good second opinions.

So: keep the 4-model benchmark to **sampled research** on quality, and let the production classification run on the fast pair + Groq. That finishes in hours, not days, at no accuracy cost.

---

## 6. Evidence gaps (stated honestly)

- My direct latency comparison of gemma-4 with/without `enable_thinking:false` **could not be completed** — the endpoint throttled after the first call (`RemoteDisconnected`, then `URLError`). The parameter's effect on gemma-4 is therefore **unverified**; the vLLM documentation and the muse-glimmer result make it the most promising remaining experiment.
- The 115 s figure is one row of `compare100.json` for one model; per-model averages across the full corpus would sharpen the estimate.
- RPM limits are per-key and documented as 40 on the free tier; the harness deliberately leaves headroom at 30.
