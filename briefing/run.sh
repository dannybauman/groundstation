#!/usr/bin/env bash
# Scheduled fleet sweep — the cron/launchd entrypoint. Unix only.
# The OS owns the schedule (see README "Scheduled sweeps"); this wraps one run
# with a lock and a log. Webhook comes from SLACK_WEBHOOK_URL, same env var
# brief.py already reads. Extra args pass through (e.g. --slack-dry-run).
set -u
cd "$(dirname "$0")/.."

# color only on a TTY (and never under NO_COLOR); cron/launchd and the log see plain text
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then C_ACC=$'\033[38;5;166m'; C_NOTE=$'\033[33m'; C_R=$'\033[0m'; else C_ACC=""; C_NOTE=""; C_R=""; fi
[ -t 1 ] && echo "  --=${C_ACC}<o>${C_R}=--  groundstation sweep"

LOCK="briefing/state/.run.lock"
LOG="briefing/state/run.log"
mkdir -p briefing/state

# ponytail: mkdir is the atomic lock; a lock older than 2h is a crashed run, reclaim it
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +120 2>/dev/null)" ]; then
    echo "reclaiming stale lock (>2h old)" >>"$LOG"
    rmdir "$LOCK" 2>/dev/null || true
    mkdir "$LOCK" 2>/dev/null || { echo "${C_NOTE}already running; skipping${C_R}"; exit 0; }
  else
    echo "${C_NOTE}already running; skipping${C_R}"
    exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "=== sweep $(date -u +%FT%TZ) ==="
  uv run briefing/brief.py --fleet briefing/fleet.json "$@"
  echo "=== done $(date -u +%FT%TZ) (exit $?) ==="
} >>"$LOG" 2>&1
