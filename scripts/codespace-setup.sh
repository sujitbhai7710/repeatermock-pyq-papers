#!/usr/bin/env bash
# Codespace bootstrap for the SSC PYQ project (opencode edition).
# Idempotent: runs on create, and again (quick mode) on every start.
set -uo pipefail

QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }

say "SSC PYQ — Codespace setup (opencode)"

if [ "$QUICK" -eq 0 ]; then
  python -m pip install --quiet --upgrade pip || warn "pip upgrade skipped"
  if [ -s requirements.txt ] && grep -qvE '^\s*(#|$)' requirements.txt; then
    python -m pip install --quiet -r requirements.txt && ok "requirements installed"
  else
    ok "no third-party requirements (stdlib-only pipeline)"
  fi
fi

# ---- opencode CLI (the coding agent you will drive) -------------------------
if command -v opencode >/dev/null 2>&1; then
  ok "opencode already installed: $(opencode --version 2>/dev/null | head -1)"
elif [ "$QUICK" -eq 0 ]; then
  say "Installing opencode"
  npm install -g opencode-ai >/dev/null 2>&1 && ok "installed opencode-ai" \
    || warn "npm install failed — retry: npm i -g opencode-ai"
fi

# ---- .env from Codespaces secrets ------------------------------------------
say "Environment"
{
  for v in AGENTROUTER_KEYS JUSTWOKER_KEYS AR_PROXY_TOKEN JW_PROXY_TOKEN ZEN_PROXY_TOKEN MONID_API_KEY; do
    val="${!v:-}"
    if [ -n "$val" ]; then echo "$v=$val"; ok "$v present"; else warn "$v missing"; fi
  done
} > .env
chmod 600 .env 2>/dev/null || true
ok ".env written (git-ignored)"

# ---- smoke test ------------------------------------------------------------
say "Smoke test"
if python -m agent.cli --help >/dev/null 2>&1; then
  ok "agent CLI loads"
  python -m agent.cli routes --probe 2>/dev/null | tail -12 || warn "route probe unavailable"
  python -m agent.cli audit 2>/dev/null | tail -3 || warn "audit unavailable"
else
  warn "CLI failed to load — run: python -m agent.cli --help"
fi

cat <<'EOF'

------------------------------------------------------------------
NEXT
  1. bash scripts/codespace-git.sh          # wire git push (self-edit)
  2. paste TASK-PROMPT.md into opencode      # or: opencode
  3. python -m agent.cli routes --probe      # what AI is alive
  4. python -m agent.cli audit               # must print VIOLATIONS: 0
The pipeline checkpoints every 15 min and publishes to the `pyq-db` branch.
------------------------------------------------------------------
EOF
