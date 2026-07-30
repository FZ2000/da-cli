#!/usr/bin/env python3
"""Catch the two ways a pull request can silently fail to reach main.

Both have happened in this repo:

1. **Mis-stacked.** A PR opened with ``base`` set to another feature
   branch. If that branch merges first and is consumed, merging the
   child puts its commits on a branch nothing pulls from. The PR
   reports ``merged: true`` and the work never lands.

2. **Stranded.** Same outcome, discovered after the fact — the commits
   exist somewhere, just not on ``main``.

Run ``--audit`` before merging anything, and ``--verify N`` after
merging PR N. Neither needs write access.

    python3 tools/check_pr_hygiene.py --audit
    python3 tools/check_pr_hygiene.py --verify 26

Credentials come from ``git credential fill`` for the API host, so
there is nothing to configure and no token to store.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request

API = "http://localhost:3000/api/v1"
REPO = "FZ2000/da-cli"
TRUNK = "main"


def _auth_header() -> dict[str, str]:
    host = API.split("//", 1)[1].split("/", 1)[0]
    proto = API.split(":", 1)[0]
    out = subprocess.run(
        ["git", "credential", "fill"],
        input=f"protocol={proto}\nhost={host}\n\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    creds = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    token = base64.b64encode(f"{creds['username']}:{creds['password']}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _get(path: str) -> object:
    req = urllib.request.Request(f"{API}{path}", headers=_auth_header())
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def _sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=False).stdout.strip()


def audit() -> int:
    """Flag every open PR whose base is not the trunk."""
    prs = _get(f"/repos/{REPO}/pulls?state=open&limit=50")
    bad = [p for p in prs if p["base"]["ref"] != TRUNK]
    for p in prs:
        mark = "  " if p["base"]["ref"] == TRUNK else "!!"
        print(f"{mark} #{p['number']:<4} base={p['base']['ref']:<28} {p['title'][:44]}")
    if bad:
        print(f"\n{len(bad)} PR(s) not based on {TRUNK}. If one of those base")
        print("branches merges first, merging the child strands its work.")
        print("Retarget while still open:")
        for p in bad:
            url = f"{API}/repos/{REPO}/pulls/{p['number']}"
            print(f"""  curl -X PATCH -d '{{"base":"{TRUNK}"}}' {url}""")
        print("\nAfter a PR is merged this is unrecoverable — the API refuses")
        print("to retarget a merged PR, and recovery means cherry-pick + new PR.")
        return 1
    print(f"\nAll {len(prs)} open PR(s) target {TRUNK}.")
    return 0


def verify(number: int) -> int:
    """Confirm a merged PR's head commit actually reached the trunk."""
    pr = _get(f"/repos/{REPO}/pulls/{number}")
    head = pr["head"]["sha"]
    print(f"#{number}  {pr['title'][:60]}")
    print(f"  state={pr['state']} merged={pr.get('merged')} base={pr['base']['ref']}")

    _sh("git", "fetch", "-q", "origin")
    reachable = (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", head, f"origin/{TRUNK}"],
            capture_output=True,
            check=False,  # a non-zero exit IS the answer we are testing for
        ).returncode
        == 0
    )
    if reachable:
        print(f"  head {head[:12]} IS an ancestor of origin/{TRUNK} — the work landed.")
        return 0

    print(f"  head {head[:12]} is NOT an ancestor of origin/{TRUNK}.")
    if pr.get("merged"):
        print(f"  This PR reports merged, but its commits are not on {TRUNK}.")
        print(f"  It merged into '{pr['base']['ref']}', which never reached {TRUNK}.")
        print("  Recover with:")
        print(f"    git checkout -b recover/pr{number} origin/{TRUNK}")
        print(f"    git cherry-pick {head}")
        print("    # then open a PR against main")
    else:
        print("  (not merged yet, so this is expected)")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--audit", action="store_true", help="flag open PRs not based on main")
    g.add_argument("--verify", type=int, metavar="N", help="confirm PR N's work reached main")
    args = ap.parse_args()
    try:
        return audit() if args.audit else verify(args.verify)
    except urllib.error.HTTPError as e:
        print(f"API error {e.code}: {e.reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
