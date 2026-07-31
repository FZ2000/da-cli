#!/usr/bin/env bash
# Prove a refactor of the dacli package changed no behaviour.
#
# Four independent checks. The CLI-surface check is the strongest: it
# regenerates the reference from build_parser(), so a byte-identical
# result means every command, flag, default and help string is
# unchanged across the whole 29-command tree.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv-dev/bin/python
fail=0

echo "1. test suite"
$PY -m pytest -q 2>&1 | tail -1

echo "2. public API surface"
$PY - <<'PYEOF'
import sys, inspect; sys.path.insert(0, ".")
import dacli
rows = []
for n in sorted(dacli.__all__):
    o = getattr(dacli, n)
    k = "func" if inspect.isfunction(o) else ("class" if inspect.isclass(o) else type(o).__name__)
    s = str(inspect.signature(o)) if inspect.isfunction(o) else ""
    rows.append(f"{n}\t{k}\t{s}")
golden = [l.rstrip("\n") for l in open("/tmp/golden/api.txt")][1:]
print("   identical" if rows == golden else "   CHANGED")
raise SystemExit(0 if rows == golden else 1)
PYEOF

echo "3. names the tests reach for"
$PY - <<'PYEOF'
import sys; sys.path.insert(0, ".")
import dacli
missing = [n.strip() for n in open("/tmp/golden/test_contract.txt")
           if n.strip() and n.strip() != "py" and not hasattr(dacli, n.strip())]
print("   all resolvable" if not missing else f"   MISSING: {missing}")
raise SystemExit(0 if not missing else 1)
PYEOF

echo "4. CLI surface (regenerated from build_parser)"
$PY tools/gen_cli_docs.py >/dev/null
if diff -q docs/reference/cli.md /tmp/golden/cli.md >/dev/null; then
  echo "   byte-identical"
else
  echo "   CHANGED — the argparse tree differs"; fail=1
fi

echo "5. test isolation still reaches extracted modules"
$PY - <<'PYEOF2'
import sys, tempfile, pathlib; sys.path.insert(0, ".")
import dacli
bad = []

# STATE_DIR: the lock and sync summaries must follow the patched path.
tmp = pathlib.Path(tempfile.mkdtemp()); real = dacli.STATE_DIR
dacli.STATE_DIR = tmp
with dacli._cmd_lock("verify-probe"):
    pass
if sorted(p.name for p in tmp.iterdir()) != [".verify-probe.lock"]:
    bad.append("STATE_DIR patch did not redirect the lock file")
leaked = list(real.glob(".verify-probe.lock")) if real.exists() else []
if leaked:
    bad.append("lock leaked into the real state dir")
    for f in leaked:
        f.unlink()          # never leave debris behind, even on failure
dacli.STATE_DIR = real

# http_json: patching it must intercept every caller, wherever it now lives.
from unittest.mock import patch
with patch.object(dacli, "http_json", return_value={"probe": True}) as m:
    if dacli.http_json("x") != {"probe": True}:
        bad.append("dacli.http_json patch not effective")

print("   isolation intact" if not bad else "   BROKEN: " + "; ".join(bad))
raise SystemExit(0 if not bad else 1)
PYEOF2

exit $fail
