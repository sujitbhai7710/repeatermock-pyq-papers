#!/usr/bin/env bash
# Supervisor for a long-lived machine (Codespace / server).
#
#   bash scripts/agent-supervisor.sh              # build + self-heal
#   bash scripts/agent-supervisor.sh --no-fix     # build only
#   bash scripts/agent-supervisor.sh --once       # single pass
#
# What it does
#   1. runs the SSC PYQ pipeline in rounds (each round checkpoints every 15 min
#      and stops cleanly BEFORE the machine's session limit)
#   2. after every round it commits state/ + database/ to the `pyq-db` branch
#   3. if a round fails hard, it hands the logs to the agentic coding CLI and
#      asks it to diagnose and fix the bug, then retries
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FIX=1; ONCE=0
for a in "$@"; do
  case "$a" in
    --no-fix) FIX=0 ;;
    --once)   ONCE=1 ;;
  esac
done

[ -f .env ] && set -a && . ./.env && set +a

# Keep a safety margin below a 4-hour machine/session limit.
export MAX_WORK_SECONDS="${MAX_WORK_SECONDS:-12600}"   # 3.5 h
export CHECKPOINT_INTERVAL_SECONDS="${CHECKPOINT_INTERVAL_SECONDS:-900}"  # 15 min
export PYQ_GIT_PUSH="${PYQ_GIT_PUSH:-1}"

LOG_DIR="reports/supervisor"; mkdir -p "$LOG_DIR"
STAMP() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

publish() {
  git add -f state database >/dev/null 2>&1 || true
  if git diff --cached --quiet 2>/dev/null; then
    echo "  nothing new to publish"; return 0
  fi
  git -c user.email=pyq-agent@users.noreply.github.com -c user.name=pyq-agent \
      commit -q -m "pyq-agent: checkpoint $(STAMP) [skip ci]" || true
  if git ls-remote --exit-code --heads origin pyq-db >/dev/null 2>&1; then
    git fetch -q --depth=1 origin pyq-db || true
    git reset -q --soft FETCH_HEAD 2>/dev/null || true
    git -c user.email=pyq-agent@users.noreply.github.com -c user.name=pyq-agent \
        commit -q -m "pyq-agent: checkpoint $(STAMP) [skip ci]" || true
  fi
  git push -q origin HEAD:pyq-db && echo "  published to pyq-db" || echo "  push failed (will retry next round)"
}

ask_agent_to_fix() {
  local log="$1"
  command -v claude >/dev/null 2>&1 || { echo "  agentic CLI not installed - skipping auto-fix"; return 1; }
  echo "  handing the failure to the coding agent..."
  claude -p "You are the maintainer of this repository. The SSC PYQ pipeline just failed.
Read the tail of $log and the relevant source under agent/.
Diagnose the root cause, make the smallest correct fix, run:
  python -m unittest discover -s tests -t .   and   python -m agent.cli audit
and only then summarise: root cause, files changed, and the verification output.
Do not modify SSC-*/**/*.json or chapter-and-topic/*.md." \
    > "$LOG_DIR/fix-$(STAMP).md" 2>&1
  echo "  agent report -> $LOG_DIR/fix-$(STAMP).md"
}

trap 'echo; say "interrupted - final flush"; publish; exit 0' INT TERM

round=0
while :; do
  round=$((round + 1))
  LOG="$LOG_DIR/round-$round-$(STAMP).log"
  say "round $round  (window ${MAX_WORK_SECONDS}s, checkpoint ${CHECKPOINT_INTERVAL_SECONDS}s)"
  python -m agent.cli run 2>&1 | tee "$LOG"
  rc=${PIPESTATUS[0]}
  echo "round $round exit=$rc"

  publish

  case "$rc" in
    0)   echo "  round complete"
         if python -m agent.cli stats --json 2>/dev/null | grep -q '"remaining": 0'; then
           say "all phases complete"; break
         fi ;;
    3|4) echo "  soft stop (rate limit / window) - continuing in a new round" ;;
    *)   echo "  hard failure"
         [ "$FIX" -eq 1 ] && ask_agent_to_fix "$LOG" ;;
  esac

  [ "$ONCE" -eq 1 ] && break
  sleep 20
done

say "summary"
python -m agent.cli audit || true
python -m agent.cli stats || true
