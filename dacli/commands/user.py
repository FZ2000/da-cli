"""User / watch commands.

Names the test suite patches are read through the ``dacli`` package at
call time; see ADR 0007.
"""

import argparse
import sys
import urllib.error
import urllib.parse

import dacli

from ..constants import API_BASE


# --------------------------------------------------------------------------
# User / watch
# --------------------------------------------------------------------------
def cmd_user_profile(args: argparse.Namespace) -> None:
    cfg = dacli.load_config()
    state = dacli.load_state()
    token = dacli.access_token(cfg, state)
    body = dacli.http_json(
        f"{API_BASE}/user/profile/{urllib.parse.quote(args.username)}?mature_content=true",
        token=token,
    )
    user = body.get("user", {})
    print(f"@{user.get('username')}")
    print(f"  userid:    {user.get('userid')}")
    print(f"  profile:   {body.get('profile_url')}")
    print(f"  watching:  {bool(body.get('is_watching'))}")
    print(f"  artist?:   {body.get('user_is_artist')}")
    print(f"  bio:       {body.get('bio') or ''}")


def cmd_watch_list(args: argparse.Namespace) -> None:
    cfg = dacli.load_config()
    state = dacli.load_state()
    token = dacli.access_token(cfg, state)
    try:
        me = dacli.http_json(f"{API_BASE}/user/whoami?mature_content=true", token=token).get(
            "username"
        )
        body = dacli.http_json(
            f"{API_BASE}/user/friends/{urllib.parse.quote(str(me))}"
            f"?limit={args.limit}&offset={args.offset}&mature_content=true",
            token=token,
        )
    except urllib.error.HTTPError as e:
        if e.code == 403:
            dacli.log(
                "`watch list` needs `user` scope — current token only has the listed scope", "error"
            )
            dacli.log(
                '(re-run `da auth --scope "user browse"` to broaden, or use '
                "`da sync watched --via-feed` which works with browse scope alone)",
                "warn",
            )
            sys.exit(2)
        raise
    for r in body.get("results", []):
        u = r.get("user", {})
        print(f"  @{u.get('username')}  ({u.get('type')})")
    if body.get("has_more"):
        print(f"\nhas_more: True (next_offset={body.get('next_offset')})")
