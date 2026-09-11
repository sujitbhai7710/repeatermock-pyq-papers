# Running this project in a GitHub Codespace

A Codespace gives the project a **long-lived machine with real write access to this repository**
(no 6-hour job cap, no "which token can push" problem) and a preinstalled agentic coding CLI.

---

## 1. Create the Codespace

1. Open <https://github.com/sujitbhai7710/repeatermock-pyq-papers>
2. **Code ▾ → Codespaces → Create codespace on `main`**
3. First start takes a few minutes. `scripts/codespace-setup.sh` runs automatically.

The dev container is configured in [`.devcontainer/devcontainer.json`](../.devcontainer/devcontainer.json):

| Setting | Value |
|---|---|
| Base image | `mcr.microsoft.com/devcontainers/python:1-3.11-bookworm` |
| Features | Node 20, GitHub CLI |
| Machine | 4 CPU / 8 GB / 32 GB (change to 2 CPU to save free hours) |
| Setup hook | `postCreateCommand` + `postStartCommand` |

---

## 2. Secrets to add (Codespaces → Settings → Secrets)

Set these once; they are injected into the container on every start:

| Secret | Purpose |
|---|---|
| `ZAI_API_KEY` | **z.ai GLM Coding Plan key** — powers the agentic coding CLI |
| `AGENTROUTER_KEYS` | 3 comma-separated agentrouter.org keys (pipeline's AI calls) |
| `JUSTWOKER_KEYS` | 3 comma-separated api.justwoker.icu keys (pipeline's AI calls) |
| `AR_PROXY_TOKEN` / `JW_PROXY_TOKEN` | worker fallbacks (optional) |
| `MONID_API_KEY` | web search / fetch for the agent |

`devcontainer.json` also declares these under `secrets`, so Codespaces will prompt for any that
are missing the first time you create a codespace from this repo.

---

## 3. The coding agent on z.ai (GLM)

z.ai exposes an **Anthropic-compatible** endpoint, so the standard agentic CLI runs on GLM models.
The dev container sets this up for you:

```
ANTHROPIC_BASE_URL = https://api.z.ai/api/anthropic
ANTHROPIC_AUTH_TOKEN = ${localEnv:ZAI_API_KEY}
ANTHROPIC_MODEL = glm-4.6
```

Verify inside the Codespace terminal:

```bash
claude --version          # the CLI
echo "$ANTHROPIC_BASE_URL"
claude -p "summarise README.md in 3 lines"
```

> If your z.ai plan uses the OpenAI-style protocol instead, switch
> `ANTHROPIC_BASE_URL` to the OpenAI-compatible base URL from
> <https://docs.z.ai/devpack/quick-start> and export `OPENAI_API_KEY`.

---

## 4. Run the builder

```bash
# one full pass (deterministic + AI verification)
python -m agent.cli run

# deterministic only, no AI calls at all
python -m agent.cli run --no-ai

# keep building until everything is finished, and push progress to `pyq-db`
bash scripts/agent-loop.sh --push
```

The pipeline checkpoints **every 15 minutes** and, with `PYQ_GIT_PUSH=1`, commits
`state/` + `database/` to the **`pyq-db`** branch each time.

Useful commands:

```bash
python -m agent.cli routes --probe     # which AI routes are alive right now
python -m agent.cli audit              # structural self-check (must print VIOLATIONS: 0)
python -m agent.cli stats --top 20     # counts per exam/subject + top concepts
python -m agent.cli mocks              # rebuild the mock-pack catalogue
```

---

## 5. Important limits (so nothing surprises you)

| Limit | Reality |
|---|---|
| Free Codespaces hours | ~**120 core-hours/month** (e.g. ~60 h on a 4-core, ~120 h on 2-core). Change the machine type to stretch it. |
| Idle timeout | Codespaces **stops after 30 minutes of inactivity** by default. Raise it in your Codespaces settings (max 4 h) for long unattended runs. |
| Stopped codespaces | Persist for your retention window (default 30 days) and keep their disk, so `state/` survives. |
| One-off cost | Codespaces consumes your personal free quota; the GitHub Action remains free for public repos. |

Because of the idle timeout, a Codespace is great for **supervised long runs**, while the
GitHub Action remains the unattended 24/7 worker. The two share the same `pyq-db` branch, so they
can hand off to each other: the Action resumes from whatever the Codespace last published.

---

## 6. Recommended workflow

1. GitHub **Action** — always-on worker (checkpoint every 15 min, 5.5 h per run).
2. **Codespace** — open it when you want to *change* the code, run the coding agent, or push a
   long uninterrupted session.
3. Both write to `pyq-db`; `main` stays code-only.
