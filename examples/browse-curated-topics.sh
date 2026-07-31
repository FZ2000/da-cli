#!/usr/bin/env bash
# examples/browse-curated-topics.sh
#
# List what curated DA topics currently hold: deviation ids and titles,
# one line each. Useful for finding reference material without ingesting
# every watched artist's full output.
#
# This script DOWNLOADS NOTHING. `da search` is a read-only browse — see
# docs/commands/search.md. To actually fetch what you find here, pass an
# artist to `da sync artist <name>`; there is no per-topic sync mode.
#
# Prereqs: `./install.sh` and `da auth` done once.

set -euo pipefail

# Topics are DA-curated editorial categories. List the live set with:
#   da search topics
TOPICS=(
  digitalart
  nature
  fantasy
  animals
)

failed=0
for topic in "${TOPICS[@]}"; do
  echo "=== pulling topic: $topic ==="
  # 24 is DA's hard cap for /browse/topic -- one over and the request
  # comes back HTTP 400, which under `set -e` ended this script on its
  # first topic. See docs/commands/search.md for the per-endpoint caps.
  #
  # Single-quoted shell string + double-quoted Python strings: no
  # escaping, and no f-string (an f-string with escaped quotes is a
  # SyntaxError on every Python version).
  da search topic "$topic" --limit 24 --json \
    | python3 -c 'import json, sys
body = json.load(sys.stdin)
for r in body.get("results", []):
    print("  {}  {}".format(r.get("deviationid"), r.get("title")))' \
    || { echo "  (topic $topic failed -- continuing)" >&2; failed=1; }
done

# Report a partial run honestly: a scheduler reading only the exit code
# should not see success when some topics never came back.
exit "$failed"
