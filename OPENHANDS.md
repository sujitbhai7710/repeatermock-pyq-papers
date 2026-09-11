# OPENHANDS.md — how the OpenHands Cloud agent runs this project

Codespaces were removed. **OpenHands Cloud** is now the remote worker that keeps building the
database. This file explains how it is wired and how to drive it.

---

## 1. What is set up

| Thing | Value |
|---|---|
| API base | `https://app.all-hands.dev` |
| API key | `sk-oh-…` (a classic OpenHands Cloud key — keep it private) |
| Auth headers | `Authorization: Bearer <key>` for conversations, `X-Access-Token: <key>` for secrets/events |
| Repo | `sujitbhai7710/repeatermock-pyq-papers`, branch `main` |
| Model | `openhands/deepseek-v4-flash` — **free on OpenHands**, used for all agent/coding work |
| Secrets | created in OpenHands Cloud (see §3) |

## 2. How the agent works

1. You POST a new conversation with an **initial message** (the task) and the repo to work on.
2. OpenHands creates a **sandbox**, clones the repo, installs skills, then runs the agent against it.
3. The agent works in the sandbox, makes commits and can open a **pull request**.
4. You poll the conversation until the task reaches a terminal state.

Start states: `WORKING → WAITING_FOR_SANDBOX → PREPARING_REPOSITORY → SETTING_UP_SKILLS → READY`.
Execution states: `idle · running · paused · waiting_for_confirmation · finished · error · stuck`.

**Terminal states to stop polling:** `finished`, `error`, `stuck`, `waiting_for_confirmation`
(the last one needs a human click in the UI).

## 3. Secrets created in OpenHands Cloud

These are injected into the sandbox, so the pipeline can call its own AI routes without any file on disk:

| Secret | Used for |
|---|---|
| `AGENTROUTER_KEYS` | 3 agentrouter.org keys (pipeline AI calls) |
| `JUSTWOKER_KEYS` | 3 api.justwoker.icu keys (pipeline AI calls) |
| `AR_PROXY_TOKEN` | ar-rotator worker token (fallback route) |
| `JW_PROXY_TOKEN` | jw-rotator worker token (fallback route) |
| `MONID_API_KEY` | web search / fetch for the pipeline |

`deepseek-v4-flash` for the *agent itself* is free on OpenHands, so it does not consume these keys.

## 4. Driving it (copy-paste)

**Start a conversation**

```bash
curl -X POST "https://app.all-hands.dev/api/v1/app-conversations" \
  -H "Authorization: Bearer $OPENHANDS_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "initial_message": {"content": [{"type": "text", "text": "Read TASK.md, MEMORY.md and LESSONS.md, then continue the SSC PYQ build. Start with: python -m agent.cli grammar --ai"}]},
    "selected_repository": "sujitbhai7710/repeatermock-pyq-papers"
  }'
```

**Poll the start task** → `GET /api/v1/app-conversations/start-tasks?ids=<task-id>`
**Poll the run** → `GET /api/v1/app-conversations?ids=<conversation-id>` (watch `sandbox_status` + `execution_status`)
**List everything** → `GET /api/v1/app-conversations/search?limit=20`
**Read the agent's events** → `GET /api/v1/conversation/<conversation-id>/events/search?limit=100`
(header `X-Access-Token`)

**Create another secret**

```bash
curl -X POST "https://app.all-hands.dev/api/v1/secrets" \
  -H "X-Access-Token: $OPENHANDS_KEY" -H "Content-Type: application/json" \
  -d '{"name":"MY_KEY","value":"…","description":"what it is for"}'
```

## 5. Rate limits & cost

- Too many concurrent conversations → older ones are **paused** (they resume automatically).
- `deepseek-v4-flash` is free on OpenHands; only the pipeline's own keys cost anything.

## 6. Where the results land

The agent commits in its sandbox. To publish the generated tree, run `python -m agent.cli publish`
(or the GitHub Action does it every 6 h) → the **`pyq-db`** branch. `main` stays code-only.

## 7. If it does not work

- `Authorization` errors → the key is wrong or lacks access to the repo.
- `ERROR` on the start task → repo name/access problem.
- `waiting_for_confirmation` → the agent needs a human to approve something in the OpenHands UI.
- If OpenHands is unavailable, say so and fall back to the GitHub Action (it already runs the same
  pipeline every 6 h and publishes to `pyq-db`).

---

## 8. Verified live route matrix (2026-09-11, measured)

| Route | Model | Status |
|---|---|---|
| `ar-rotator` worker | `deepseek-v4-flash` | ✅ **working** (free; needs `max_tokens >= 8192`) |
| `jw-rotator` worker | `gpt-5.6-sol` | ✅ **working** |
| `api.justwoker.icu` direct (`/v1/messages`) | `gpt-5.6-sol` | ✅ **working** |
| `agentrouter.org` direct | any | ❌ Aliyun WAF blocks non-Cloudflare IPs |
| `ar-rotator` worker | `gpt-5.6-sol`, `claude-opus-5` | ❌ 402 — no credits on agentrouter |
| any | `glm-5.3` | ❌ 3-7x slower — **do not use** |

**Use:** proposer = `deepseek-v4-flash` on `ar-rotator`; judge = `gpt-5.6-sol` on `jw-rotator`
(or direct justwoker). Check anytime with `python -m agent.cli routes --probe`.
