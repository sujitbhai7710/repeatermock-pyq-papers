#!/usr/bin/env bash
# Codespace bootstrap for the SSC PYQ agent.
# Idempotent: safe to run on create and on every start (--quick skips installs).
set -uo pipefail

QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }

say "SSC PYQ agent — Codespace setup"

# ---------------------------------------------------------------------------
# 1. Python deps (the pipeline is standard-library only, so this is a no-op)
# ---------------------------------------------------------------------------
if [ "$QUICK" -eq 0 ]; then
  python -m pip install --quiet --upgrade pip || warn "pip upgrade failed (continuing)"
  if [ -s requirements.txt ] && grep -qvE '^\s*(#|$)' requirements.txt; then
    python -m pip install --quiet -r requirements.txt && ok "requirements installed"
  else
    ok "no third-party requirements (stdlib-only pipeline)"
  fi
fi

# ---------------------------------------------------------------------------
# 2. Agentic coding CLI (z.ai GLM Coding Plan, Anthropic-compatible endpoint)
# ---------------------------------------------------------------------------
if [ "$QUICK" -eq 0 ]; then
  if command -v claude >/dev/null 2>&1; then
    ok "coding CLI already installed: $(claude --version 2>/dev/null | head -1)"
  else
    say "Installing the agentic coding CLI (Claude Code on z.ai GLM)"
    npm install -g @anthropic-ai/claude-code >/dev/null 2>&1 \
      && ok "installed @anthropic-ai/claude-code" \
      || warn "npm install failed — install manually: npm i -g @anthropic-ai/claude-code"
  fi
fi

# ---------------------------------------------------------------------------
# 3. Write .env from Codespaces secrets (never committed; .gitignore covers it)
# ---------------------------------------------------------------------------
say "Environment"
{
  for v in AGENTROUTER_KEYS JUSTWOKER_KEYS AR_PROXY_TOKEN JW_PROXY_TOKEN MONID_API_KEY; do
    val="${!v:-}"
    if [ -n "$val" ]; then echo "$v=$val"; ok "$v present"; else warn "$v missing"; fi
  done
} > .env
chmod 600 .env 2>/dev/null || true
ok "wrote .env (git-ignored) from Codespaces secrets"

if [ -n "${ZAI_API_KEY:-}" ]; then
  ok "ZAI_API_KEY present (agentic CLI will use glm-4.6 via api.z.ai)"
else
  warn "ZAI_API_KEY missing — set it as a Codespaces secret to use the coding CLI"
fi

# ---------------------------------------------------------------------------
# 4. Smoke test the pipeline
# ---------------------------------------------------------------------------
say "Smoke test"
if python -m agent.cli --help >/dev/null 2>&1; then
  ok "CLI loads"
  python -m agent.cli routes --probe 2>/dev/null | tail -25 || warn "route probe unavailable"
else
  warn "agent CLI failed to load — run: python -m agent.cli --help"
fi

cat <<'EOF'

------------------------------------------------------------------
Next steps
  1. Start the builder:      bash scripts/agent-loop.sh
  2. Full run once:          python -m agent.cli run
  3. Deterministic only:     python -m agent.cli run --no-ai
  4. Check output:           python -m agent.cli audit && python -m agent.cli stats
  5. Publish progress:       bash scripts/agent-loop.sh --push
The loop commits state/ and database/ to the `pyq-db` branch every 15 minutes.
------------------------------------------------------------------
EOF
