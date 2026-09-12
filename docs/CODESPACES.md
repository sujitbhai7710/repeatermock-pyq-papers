# Running this project in a GitHub Codespace (with `opencode`)

Everything is preconfigured: [`.devcontainer/devcontainer.json`](../.devcontainer/devcontainer.json) builds a
machine with **Python 3.11**, **Node 20**, the **`opencode`** coding agent, and **git push access**, so the
agent can edit the repo and publish by itself.

---

## 1. Create the Codespace

1. <https://github.com/sujitbhai7710/repeatermock-pyq-papers>
2. **Code ▾ → Codespaces → Create codespace on `main`**
3. Wait for `scripts/codespace-setup.sh` to finish (it prints a route matrix and the audit result).

| | |
|---|---|
| Image | `mcr.microsoft.com/devcontainers/python:1-3.11-bookworm` |
| Features | Node 20, GitHub CLI |
| Machine | 4 CPU / 8 GB / 32 GB (use **2 CPU** to stretch the free 120 core-hours) |
| Work window | `MAX_WORK_SECONDS=12600` (**3.5 h**) — safe under a 4-hour session limit |
| Checkpoint | every **900 s**, published to the `pyq-db` branch |

---

## 2. Secrets (Codespaces → Settings → Secrets)

Add these; `devcontainer.json` declares them so Codespaces prompts for any that are missing:

| Secret | Used for |
|---|---|
| `AGENTROUTER_KEYS` | 3 comma-separated agentrouter keys (pipeline AI) |
| `JUSTWOKER_KEYS` | 3–4 comma-separated justwoker keys (pipeline AI) |
| `AR_PROXY_TOKEN` | ar-rotator worker token → `deepseek-v4-flash` (proposer) |
| `JW_PROXY_TOKEN` | jw-rotator worker token → `gpt-5.6-sol` (judge) |
| `ZEN_PROXY_TOKEN` | zen-rotator token → `muse-spark-1.2` (alternate) |
| `GROQ_API_KEY` | Groq `openai/gpt-oss-120b` — the fastest verified route, runs first |
| `MONID_API_KEY` | web search + fetch (Monid/TinyFish) |

Endpoint details, headers and copy-paste examples are in [`AI-APIS.txt`](../AI-APIS.txt)
(and the same file with live keys is kept privately — never commit it).

---

## 3. Let the Codespace edit itself (git push)

Run once (the `postAttachCommand` already does it, but re-run if push fails):

```bash
bash scripts/codespace-git.sh
```

It sets the git identity and wires git to the Codespace's `GITHUB_TOKEN`, so:

```bash
git add -A && git commit -m "…" && git push origin HEAD:main     # code
python -m agent.cli publish                                      # generated data -> pyq-db
```

> **Only one writer.** If a GitHub Action is enabled at the same time, two processes publish to
> `pyq-db` and the checkpoints overwrite each other. Keep the Action **disabled** while you work here.

---

## 4. Drive the agent

`opencode` is installed and pointed at the project's own AI route:

```
OPENAI_BASE_URL = https://ar-rotator.opencode-5a3.workers.dev/v1
OPENAI_API_KEY  = ${localenv:AR_PROXY_TOKEN}
OPENCODE_MODEL  = deepseek-v4-flash
```

```bash
opencode                 # interactive
# or paste TASK-PROMPT.md into it
```

The exact instructions to give it are in **[`TASK-PROMPT.md`](../TASK-PROMPT.md)**.

Useful commands the agent (or you) will run:

```bash
python -m agent.cli routes --probe     # which AI routes are alive
python -m agent.cli audit              # MUST print VIOLATIONS: 0
python -m agent.cli errors --top 20    # why the AI failed, if it did
python -m agent.cli stats --top 20     # counts
python -m unittest discover -s tests -t .   # 232 tests must stay green
python -m agent.cli run                # the AI verification pass (checkpoints every 15 min)
python -m agent.cli publish            # merge-publish state/ + database/ to pyq-db
```

---

## 5. Limits

| Limit | Reality |
|---|---|
| Free hours | ~**120 core-hours/month** (≈60 h on 4-core, ≈120 h on 2-core) |
| Idle stop | Codespaces stops after **30 min idle** by default — raise it in settings for long runs |
| Session safety | the pipeline stops itself at **3.5 h** and checkpoints, so nothing is lost to a 4-hour cutoff |
| Persistence | a stopped codespace keeps its disk, and all real state lives on the `pyq-db` branch anyway |

---

## 6. First five minutes — checklist

```bash
bash scripts/codespace-git.sh        # 1. enable push
python -m agent.cli routes --probe   # 2. see what AI is alive
python -m agent.cli audit            # 3. expect VIOLATIONS: 0
python -m unittest discover -s tests -t .   # 4. expect OK (232 tests)
opencode                             # 5. paste TASK-PROMPT.md and let it work
```
