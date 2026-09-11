# Running this project in a GitHub Codespace

A Codespace gives the project a **long-lived machine with real write access to this repository**
(no 6-hour job cap, no token juggling) plus a preinstalled agentic coding agent.

---

## 1. Create the Codespace

1. Open <https://github.com/sujitbhai7710/repeatermock-pyq-papers>
2. **Code ▾ → Codespaces → Create codespace on `main`**
3. `scripts/codespace-setup.sh` runs automatically on create and on every start.

Dev container: [`.devcontainer/devcontainer.json`](../.devcontainer/devcontainer.json)

| Setting | Value |
|---|---|
| Image | `mcr.microsoft.com/devcontainers/python:1-3.11-bookworm` |
| Features | Node 20, GitHub CLI |
| Machine | 4 CPU / 8 GB / 32 GB (use 2 CPU to stretch free hours) |
| Work window | `MAX_WORK_SECONDS=12600` (**3.5 h** — saves everything before a 4-hour session limit) |
| Checkpoint | every **900 s (15 min)**, committed to `pyq-db` |

---

## 2. Secrets (Codespaces → Settings → Secrets)

| Secret | Purpose |
|---|---|
| `JW_PROXY_TOKEN` | jw-rotator worker token — **also powers the in-Codespace coding agent** (`gpt-5.6-sol`) |
| `AGENTROUTER_KEYS` | 3 comma-separated agentrouter.org keys |
| `JUSTWOKER_KEYS` | 3 comma-separated api.justwoker.icu keys |
| `AR_PROXY_TOKEN` | ar-rotator worker token (alternate route) |
| `MONID_API_KEY` | web search / fetch |

`devcontainer.json` declares these under `secrets`, so Codespaces prompts for any missing one.

---

## 3. The coding agent (no purchase needed)

The coding agent runs on **your own jw-rotator worker**, which speaks the Anthropic protocol:

```
ANTHROPIC_BASE_URL   = https://jw-rotator.opencode-5a3.workers.dev
ANTHROPIC_AUTH_TOKEN = ${localEnv:JW_PROXY_TOKEN}
ANTHROPIC_MODEL      = gpt-5.6-sol
```

Verify inside the Codespace:

```bash
claude --version
claude -p "summarise README.md in 3 lines"
```

### Verified route matrix (measured)

| Route | Model | Status |
|---|---|---|
| `api.justwoker.icu` direct | `gpt-5.6-sol` | ✅ works (all 3 keys) |
| `jw-rotator` worker | `gpt-5.6-sol` | ✅ works |
| `ar-rotator` worker | `deepseek-v4-flash` | ✅ works — needs `max_tokens ≥ 3000` (reasoning model) |
| `agentrouter.org` direct | any | ❌ Aliyun WAF blocks non-Cloudflare IPs (also blocked in India) |
| `ar-rotator` worker | `gpt-5.6-sol`, `claude-opus-5` | ❌ 402 no credits |

The pipeline already tries **all four routes, direct first, worker as alternate**, with
per-`(provider, model)` circuit breakers and cross-model fallback, so it uses whatever is alive.

---

## 4. Run it

```bash
# supervised builder: rounds + commit to pyq-db + auto-fix by the coding agent on hard failures
bash scripts/agent-supervisor.sh

# just the builder, no auto-fix
bash scripts/agent-supervisor.sh --no-fix

# a single pass
bash scripts/agent-supervisor.sh --once
```

`agent-supervisor.sh`:
1. runs the pipeline in rounds — each round checkpoints every 15 min and stops cleanly at 3.5 h
2. commits `state/` + `database/` to `pyq-db` after every round (and the pipeline also commits mid-round)
3. on a hard failure, hands the log to the coding agent (`claude -p ...`) to diagnose, fix, run the
   tests and the audit — then retries

Useful commands:

```bash
python -m agent.cli routes --probe      # which AI routes are alive right now
python -m agent.cli audit               # structural self-check (must print VIOLATIONS: 0)
python -m agent.cli stats --top 20      # counts + top concepts
python -m agent.cli run --no-ai         # deterministic only, zero AI calls
```

---

## 5. Limits to respect

| Limit | Reality |
|---|---|
| Free hours | ~**120 core-hours/month** (≈60 h on 4-core, ≈120 h on 2-core) |
| Idle stop | Codespaces stops after **30 min idle** by default — raise it in settings (max 4 h) for unattended runs |
| Session safety | the supervisor stops at **3.5 h** so everything is committed before any 4-hour cutoff |
| Persistence | a stopped codespace keeps its disk, so `state/` survives and the next round resumes |

The GitHub Action remains the always-on unattended worker; the Codespace is for long supervised
sessions and code changes. Both publish to the same `pyq-db` branch, so they hand off cleanly.
