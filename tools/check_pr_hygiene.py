#!/usr/bin/env python3
"""Catch the two ways a pull request can silently fail to reach main.

Both happened during this project's development on its previous forge:

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

The token comes from ``gh auth token``, so if you can run ``gh`` you can
run this — nothing to configure and no token stored here. Falls back to
``GITHUB_TOKEN`` for CI use. Neither path needs write access.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = "FZ2000/da-cli"
TRUNK = "main"


def _auth_header() -> dict[str, str]:
    """Bearer token from `gh`, or GITHUB_TOKEN when running in CI.

    Not `git credential fill` as the Gitea version used: the credential
    helper stores a token for github.com (git over HTTPS), not for
    api.github.com, so it is the wrong lookup and would miss.
    """
    token = (
        os.environ.get("GITHUB_TOKEN")
        or subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=False
        ).stdout.strip()
    )
    if not token:
        raise SystemExit("no token: run `gh auth login`, or set GITHUB_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        # Pin the API version so a future default change cannot silently
        # alter the response shape this script reads.
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(path: str) -> object:
    req = urllib.request.Request(f"{API}{path}", headers=_auth_header())
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def _sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=False).stdout.strip()


def audit() -> int:
    """Flag every open PR whose base is not the trunk."""
    prs = _get(f"/repos/{REPO}/pulls?state=open&per_page=50")
    bad = [p for p in prs if p["base"]["ref"] != TRUNK]
    for p in prs:
        mark = "  " if p["base"]["ref"] == TRUNK else "!!"
        print(f"{mark} #{p['number']:<4} base={p['base']['ref']:<28} {p['title'][:44]}")
    if bad:
        print(f"\n{len(bad)} PR(s) not based on {TRUNK}. If one of those base")
        print("branches merges first, merging the child strands its work.")
        print("Retarget while still open:")
        for p in bad:
            print(f"  gh pr edit {p['number']} --base {TRUNK}")
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
