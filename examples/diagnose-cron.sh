#!/usr/bin/env bash
# examples/diagnose-cron.sh
#
# Run `da diagnose` on a 10-minute cron; surface failures to stderr so a
# monitoring cron (or systemd-timer / launchd job) can pick them up.
# Designed to run alongside (not replace) `da sync feed`.
#
# Install (macOS launchd):
#   ./install_schedule.sh                 # sets up `da sync feed` daily 03:00
#   # then add this script on a 10-min interval via a second plist, e.g.
#   # ~/Library/LaunchAgents/com.fz2000.da-diagnose.plist

set -euo pipefail

# diagnose exit-code policy: 0 = OK, 1 = WARN, 2 = FAIL.
# rc is initialised before use: `${rc:-0}` alone would inherit an
# exported rc from the calling environment, so a healthy run could report
# somebody else's exit code and raise a false alert.
rc=0
# stderr is kept rather than discarded: when `da` exits non-zero with no
# JSON on stdout, its stderr is the only description of what went wrong.
out="$(da diagnose --json)" || rc=$?

case "$rc" in
  0)
    # Quiet on success — cron shouldn't spam.
    exit 0
    ;;
  1)
    # Warn — print a single line that cron usually emails.
    summary="$(printf '%s' "$out" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["overall"]["status"], "-", d["overall"]["warnings"], "warnings")')"
    echo "[da-cli WARN] $summary" >&2
    ;;
  2)
    # Critical — surface the full structured findings for the operator.
    echo "[da-cli FAIL] structured diagnose output follows:" >&2
    printf '%s\n' "$out" >&2
    ;;
esac

exit "$rc"
