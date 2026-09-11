#!/usr/bin/env bash
# Continuous builder for a Codespace (or any long-lived machine).
#
#   bash scripts/agent-loop.sh            # build until everything is done
#   bash scripts/agent-loop.sh --push     # also commit+push progress to `pyq-db`
#   bash scripts/agent-loop.sh --once     # a single pass, then exit
#
# The pipeline itself already checkpoints every 15 minutes and stops cleanly at
# the end of its work window, so this loop just starts the next window.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PUSH=0; ONCE=0
for a in "$@"; do
  case "$a" in
    --push) PUSH=1 ;;
    --once) ONCE=1 ;;
  esac
done

[ -f .env ] && set -a && . ./.env && set +a

export PYQ_GIT_PUSH="${PUSH}"
export CHECKPOINT_INTERVAL_SECONDS="${CHECKPOINT_INTERVAL_SECONDS:-900}"
export MAX_WORK_SECONDS="${MAX_WORK_SECONDS:-19800}"

trap 'echo; echo "stopping — writing a final checkpoint"; python -m agent.cli run --no-ai >/dev/null 2>&1 || true; exit 0' INT TERM

round=0
while :; do
  round=$((round + 1))
  echo
  echo "===================== round $round  $(date -u '+%Y-%m-%d %H:%M:%SZ') ====================="
  python -m agent.cli run
  rc=$?
  echo "round $round finished with exit=$rc  (0=ok/ai-unavailable, 3=rate limited, 4=time limit, 1=error)"

  case "$rc" in
    0|3|4) ;;                       # normal soft stops
    *)     echo "hard error — sleeping 120s before retrying"; sleep 120 ;;
  esac

  if [ "$ONCE" -eq 1 ]; then break; fi

  # stop once every phase is complete and nothing is left to verify
  if python -m agent.cli stats --json 2>/dev/null | grep -q '"remaining": 0'; then
    echo "nothing left to do — exiting"
    break
  fi
  sleep 30
done

echo
echo "--- summary ---"
python -m agent.cli audit  || true
python -m agent.cli stats  || true
