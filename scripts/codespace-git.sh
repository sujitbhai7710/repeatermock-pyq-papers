#!/usr/bin/env bash
# Make this Codespace able to edit and PUSH the project by itself.
# Run once per codespace (postAttachCommand runs it automatically).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }

echo "== git identity =="
git config --global user.name  "pyq-codespace-agent"
git config --global user.email "pyq-codespace-agent@users.noreply.github.com"
git config --global pull.rebase true
git config --global init.defaultBranch main
ok "user.name / user.email set"

# Codespaces injects GITHUB_TOKEN with write access to the repo it was created from.
if [ -n "${GITHUB_TOKEN:-}" ]; then
  git config --global credential.helper ""
  git config --global url."https://x-access-token:${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"
  ok "git credential wired to the Codespace GITHUB_TOKEN (push enabled)"
else
  warn "GITHUB_TOKEN not set — push will not work (enable 'Workflow permissions: read and write' + Codespaces access)"
fi

echo "== remotes =="
git remote -v | sed 's/x-access-token:[^@]*@/x-access-token:***@/'
ok "remotes listed"

echo "== branch =="
git fetch --quiet origin main pyq-db 2>/dev/null || warn "fetch failed"
git status -sb | head -3

cat <<'EOF'

You can now change code and publish:
  git add -A && git commit -m "..." && git push origin HEAD:main
Publish the generated database (merge, never replace):
  python -m agent.cli publish
EOF
