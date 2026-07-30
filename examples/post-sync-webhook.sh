#!/usr/bin/env bash
# examples/post-sync-webhook.sh
#
# Wrap `da sync feed` so a webhook fires after each run — e.g. to ping
# a Discord channel, kick off a separate processing pipeline, or update
# a status page. Uses `da diagnose --json` to decide payload contents.
#
# Wiring: run this from your own launchd job or cron entry. Do NOT
# substitute it for `da sync feed` inside the plist install_schedule.sh
# writes -- that plist points ProgramArguments[0] at the da-sync.app
# bundle precisely so macOS Full Disk Access can be granted to a stable
# path, and replacing it drops the bundle out of the exec chain, so a
# scheduled sync to an external volume starts failing on TCC.

set -euo pipefail

WEBHOOK_URL="${DA_SYNC_WEBHOOK_URL:-}"
TIME_BUDGET="${DA_TIME_BUDGET:-540}"
JITTER="${DA_JITTER:-0.4}"

# A fixed /tmp path is shared with every other user on the machine:
# theirs may already exist (so the redirect fails), or be a symlink they
# control. mktemp gives us one nobody else can have pre-created.
LOG_FILE="$(mktemp -t da-sync.XXXXXX)"
trap 'rm -f "$LOG_FILE"' EXIT

# 1. Run the sync. `da` handles its own logging; we capture the exit code.
set +e
da sync feed --time-budget "$TIME_BUDGET" --jitter "$JITTER" >"$LOG_FILE" 2>&1
sync_rc=$?
set -e

# 2. Capture the structured health snapshot. Diagnose exits 0/1/2 for
#    OK/WARN/FAIL; we don't let that abort the script.
set +e
diagnose_payload="$(da diagnose --json)"
diagnose_rc=$?
set -e

# 3. Build a tiny JSON envelope. Keep the schema stable so the receiver
#    can diff across runs.
payload="$(python3 -c '
import json, os, sys
sync_rc = int(sys.argv[1])
diag_rc = int(sys.argv[2])
# Tolerant on purpose: if `da` is missing or crashed, its output is not
# JSON, and a strict json.loads here would raise under `set -e` and kill
# the script before the webhook fires -- silencing the alert in exactly
# the situation it exists to report.
try:
    diag = json.loads(sys.argv[3])
except ValueError:
    diag = {}
try:
    log_tail = open(sys.argv[4], encoding="utf-8", errors="replace").read().splitlines()[-10:]
except OSError:
    log_tail = []
print(json.dumps({
    "event": "da-sync",
    "sync_exit_code": sync_rc,
    "diagnose_exit_code": diag_rc,
    "diagnose_status": diag.get("overall", {}).get("status"),
    "last_sync": diag.get("findings", []),
    "log_tail": log_tail,
}))
' "$sync_rc" "$diagnose_rc" "$diagnose_payload" "$LOG_FILE")"

# 4. Fire the webhook if configured.
if [[ -z "$WEBHOOK_URL" ]]; then
  echo "DA_SYNC_WEBHOOK_URL unset; would have posted:"
  echo "$payload"
  exit "$sync_rc"
fi

# The URL is passed on stdin, not argv: for Discord and Slack the webhook
# URL *is* the credential, and anything in argv is readable by `ps` for
# the life of the request.
printf 'url = "%s"\n' "$WEBHOOK_URL" | curl -fsS -X POST --config - \
  -H "Content-Type: application/json" \
  --data "$payload" || echo "webhook POST failed" >&2

# Exit with the SYNC's status, not curl's. This script stands in for
# `da sync feed`, so a scheduler reading its exit code must see whether
# the sync worked -- reporting curl's status made every failed sync look
# successful as long as the webhook was reachable.
exit "$sync_rc"
