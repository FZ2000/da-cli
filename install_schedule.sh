#!/usr/bin/env bash
# macOS launchd schedule for `da sync feed`.
#
# Builds a small `.app` bundle wrapper at $HOME/Applications/da-sync.app
# and a LaunchAgent that invokes it. The bundle exists so that macOS
# Full Disk Access (TCC) can be granted to a stable path under your
# control, instead of the brew-versioned Python binary at
# /opt/homebrew/Cellar/python@3.14/3.14.x/... — which moves on every
# `brew upgrade`. TCC permissions inherit down the exec tree, so the
# python child inherits the .app's grant.
#
# After running this script, grant Full Disk Access to the bundle:
#   System Settings → Privacy & Security → Full Disk Access → +
#   add ~/Applications/da-sync.app
#
# Without that grant the daily run will fail when writing to TCC-
# protected destinations like /Volumes/, ~/Documents, ~/Desktop, etc.
# (If your destination is under ~/ outside those folders, FDA isn't
# strictly required — `da config get destination` to check.)
#
# Default: runs once a day at 03:00 local time.
# Override with DA_HOUR / DA_MINUTE for a different fixed time, or
# DA_INTERVAL_SECONDS for a fixed-interval cadence (every N seconds).
#
# Usage:
#   ./install_schedule.sh                      # daily at 03:00
#   DA_HOUR=21 DA_MINUTE=30 ./install_schedule.sh   # daily at 21:30
#   DA_INTERVAL_SECONDS=21600 ./install_schedule.sh # every 6h
#   ./install_schedule.sh uninstall            # remove

set -euo pipefail

# Fail fast on non-macOS — every line below assumes macOS primitives.
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: install_schedule.sh is macOS-only (uses launchctl, .app bundles, TCC)." >&2
  echo "       on Linux, use cron or a systemd timer to invoke 'da sync feed'." >&2
  exit 1
fi

# Every value interpolated into the plist goes through this. The numeric
# validators below cover the tunables, but the PATHS are whatever the
# user's $HOME happens to be -- and a relocated home on an external
# volume ("/Volumes/Art & Design/home") produces XML launchctl rejects
# with no hint as to why. Spaces are fine; & < > are not.
xml_escape() {
  local s="$1"
  s="${s//&/&amp;}"
  s="${s//</&lt;}"
  s="${s//>/&gt;}"
  printf '%s' "$s"
}

PLIST_LABEL="com.fz2000.da-cli"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
LOG_FILE="$HOME/Library/Logs/da-cli.log"
APP_BUNDLE="$HOME/Applications/da-sync.app"
TIME_BUDGET="${DA_TIME_BUDGET:-1200}"
JITTER="${DA_JITTER:-0.4}"

# XML-safe copies for the plist body. The raw values stay for filesystem
# use (mkdir, chmod, the human-readable summary at the end).
APP_BUNDLE_XML="$(xml_escape "$APP_BUNDLE")"
LOG_FILE_XML="$(xml_escape "$LOG_FILE")"
HOME_XML="$(xml_escape "$HOME")"




# Validate integer / float inputs up front — a non-numeric $HOUR / $MINUTE /
# $INTERVAL produces a malformed plist and a confusing launchctl load failure.
validate_uint() {
  local name="$1" val="$2"
  if [[ ! "$val" =~ ^[0-9]+$ ]]; then
    echo "error: $name=$val is not a non-negative integer" >&2
    exit 1
  fi
}
validate_uint TIME_BUDGET "$TIME_BUDGET"
# JITTER is a float, not an int — it was the one tunable the block above
# missed, so DA_JITTER=40% installed cleanly and then failed argparse on
# every scheduled run (visible only in the log), and a value containing
# < or & produced a malformed plist.
if [[ ! "$JITTER" =~ ^[0-9]*\.?[0-9]+$ ]]; then
  echo "error: DA_JITTER=$JITTER is not a non-negative number" >&2
  exit 1
fi
if [[ -n "${DA_INTERVAL_SECONDS:-}" ]]; then
  validate_uint DA_INTERVAL_SECONDS "$DA_INTERVAL_SECONDS"
  HOUR=""  # unused in interval mode
  MINUTE=""
else
  HOUR="${DA_HOUR:-3}"
  MINUTE="${DA_MINUTE:-0}"
  validate_uint DA_HOUR "$HOUR"
  validate_uint DA_MINUTE "$MINUTE"
  # Force base 10. A cron-habituated user types DA_MINUTE=08, and bash
  # arithmetic reads a leading zero as octal: `(( MINUTE > 59 ))` then
  # errors with "08: value too great for base" -- and because that is an
  # `if` condition, errexit does not fire, so the range check is silently
  # SKIPPED and the script runs on to die later in printf, leaving a
  # rebuilt bundle and no reloaded job.
  HOUR=$((10#$HOUR))
  MINUTE=$((10#$MINUTE))
  if (( HOUR > 23 )); then
    echo "error: DA_HOUR=$HOUR out of range [0, 23]" >&2; exit 1
  fi
  if (( MINUTE > 59 )); then
    echo "error: DA_MINUTE=$MINUTE out of range [0, 59]" >&2; exit 1
  fi
fi

DA_BIN="$(command -v da || true)"
if [[ -z "$DA_BIN" ]]; then
  echo "error: 'da' not found on PATH. Install it first — either" >&2
  echo "       './install.sh' from this repository, or 'pipx install da-sync'." >&2
  exit 1
fi

uninstall() {
  local removed=0
  if [[ -f "$PLIST_PATH" ]]; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    echo "removed: $PLIST_PATH"
    removed=1
  fi
  # Defensive: never rm -rf a path that doesn't end in .app — protects
  # against a misconfigured $HOME that's empty or "/".
  if [[ -d "$APP_BUNDLE" && "$APP_BUNDLE" == *.app ]]; then
    rm -rf "$APP_BUNDLE"
    echo "removed: $APP_BUNDLE"
    removed=1
  fi
  if (( removed == 0 )); then
    echo "no schedule installed."
  fi
}

if [[ "${1:-}" == "uninstall" ]]; then
  uninstall
  exit 0
fi

mkdir -p "$(dirname "$PLIST_PATH")" "$(dirname "$LOG_FILE")" \
         "$APP_BUNDLE/Contents/MacOS"

# 1. Build the .app bundle wrapper. Pass-through: bundle execs `da` with
#    whatever args launchd hands it. Args live in the plist so changing
#    the schedule doesn't require rebuilding the bundle.
cat > "$APP_BUNDLE/Contents/MacOS/da-sync" <<EOF
#!/bin/bash
# da-sync.app wrapper — execs the da CLI with whatever args launchd provides.
# Generated by install_schedule.sh on $(date '+%Y-%m-%d').
#
# Why this bundle exists: macOS Full Disk Access (TCC) is granted by binary
# path. The brew-installed Python moves on every \`brew upgrade\`, so
# granting FDA to it directly is fragile. This bundle is at a stable path
# you control. Grant FDA to ~/Applications/da-sync.app once; the python
# child inherits the grant via TCC's responsible-process chain.
exec "$DA_BIN" "\$@"
EOF
chmod +x "$APP_BUNDLE/Contents/MacOS/da-sync"

cat > "$APP_BUNDLE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>      <string>da-sync</string>
  <key>CFBundleIdentifier</key>      <string>com.fz2000.da-sync</string>
  <key>CFBundleName</key>            <string>da-sync</string>
  <key>CFBundlePackageType</key>     <string>APPL</string>
  <key>CFBundleVersion</key>         <string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key>             <true/>
  <key>LSBackgroundOnly</key>        <true/>
</dict>
</plist>
EOF

# 2. Pick scheduling mode: calendar (default daily) or interval.
if [[ -n "${DA_INTERVAL_SECONDS:-}" ]]; then
  SCHEDULE_KEY="<key>StartInterval</key><integer>$DA_INTERVAL_SECONDS</integer>"
  SCHEDULE_DESC="every ${DA_INTERVAL_SECONDS}s"
else
  SCHEDULE_KEY="<key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>   <integer>$HOUR</integer>
    <key>Minute</key> <integer>$MINUTE</integer>
  </dict>"
  SCHEDULE_DESC=$(printf "daily at %02d:%02d" "$HOUR" "$MINUTE")
fi

# 3. Write the launchd plist. ProgramArguments[0] is the bundle's
#    executable (the path TCC checks). The rest are the da CLI args.
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key> <string>$PLIST_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP_BUNDLE_XML/Contents/MacOS/da-sync</string>
    <string>sync</string>
    <string>feed</string>
    <string>--time-budget</string>
    <string>$TIME_BUDGET</string>
    <string>--jitter</string>
    <string>$JITTER</string>
  </array>
  $SCHEDULE_KEY
  <key>RunAtLoad</key> <false/>
  <key>StandardOutPath</key> <string>$LOG_FILE_XML</string>
  <key>StandardErrorPath</key> <string>$LOG_FILE_XML</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key> <string>$HOME_XML</string>
    <key>PATH</key> <string>$HOME_XML/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF

chmod 0644 "$PLIST_PATH"
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo "scheduled: $PLIST_LABEL"
echo "  cadence:     $SCHEDULE_DESC"
echo "  bundle:      $APP_BUNDLE"
echo "  command:     $DA_BIN sync feed --time-budget $TIME_BUDGET --jitter $JITTER"
echo "  logs:        $LOG_FILE"
echo
echo "next step — grant Full Disk Access to the bundle:"
echo "  System Settings → Privacy & Security → Full Disk Access → +"
echo "  drag in: $APP_BUNDLE"
echo
echo "(Without the FDA grant the daily run will hang on any TCC-protected"
echo " path: /Volumes/, ~/Documents, ~/Desktop, ~/Downloads. If your"
echo " destination is under ~/ outside those folders, FDA isn't required.)"
echo
echo "run now:    launchctl start $PLIST_LABEL"
echo "tail log:   tail -f $LOG_FILE"
echo "uninstall:  ./install_schedule.sh uninstall"
