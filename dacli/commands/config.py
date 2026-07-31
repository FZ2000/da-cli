"""Config commands.

Names the test suite patches are read through the ``dacli`` package at
call time; see ADR 0007.
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.parse

import dacli

from ..config import SECRET_KEYS
from ..constants import API_BASE, KEYCHAIN_SERVICE, mature_content_param
from ..output import _atomic_write
from .search import _print_results


# --------------------------------------------------------------------------
# Config commands
# --------------------------------------------------------------------------
def cmd_config_show(args: argparse.Namespace) -> None:
    cfg = dacli.load_config()
    masked = dict(cfg)
    for k in SECRET_KEYS:
        if k in masked:
            masked[k] = dacli.mask_secret(masked[k])
    print(json.dumps(masked, indent=2, ensure_ascii=False))
    print(f"\nconfig file: {dacli.CONFIG_PATH}")
    print(f"state file:  {dacli.STATE_PATH}")
    if sys.platform == "darwin":
        print(f'keychain:    service="{KEYCHAIN_SERVICE}" (used for {sorted(SECRET_KEYS)})')


def cmd_config_set(args: argparse.Namespace) -> None:
    dacli.set_config_field(args.key, args.value)


def cmd_config_get(args: argparse.Namespace) -> None:
    cfg = dacli.load_config()
    if args.key not in cfg or cfg[args.key] in (None, ""):
        dacli.log(f"(not set: {args.key})", "warn")
        sys.exit(1)
    v = cfg[args.key]
    if args.key in SECRET_KEYS:
        if args.unmask:
            # Unmasked secret → write to stderr so piping stdout to a file
            # (`da config get client_secret --unmask > cfg.txt`) does NOT
            # silently capture the raw secret. The value lands in the
            # user's terminal, not their piped file.
            print(v, file=sys.stderr)
        else:
            print(dacli.mask_secret(v))
    else:
        # Non-secret value — stdout is fine.
        print(v)


def cmd_config_unset(args: argparse.Namespace) -> None:
    removed_anywhere = False
    if args.key in SECRET_KEYS and sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", args.key],
                capture_output=True,
                check=False,
            )
            if r.returncode == 0:
                dacli.log(f"removed {args.key} from macOS Keychain")
                removed_anywhere = True
        except FileNotFoundError:
            pass
    if dacli.CONFIG_PATH.exists():
        try:
            cfg = json.loads(dacli.CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cfg = {}
        if args.key in cfg:
            del cfg[args.key]
            _atomic_write(dacli.CONFIG_PATH, json.dumps(cfg, indent=2), 0o600)
            dacli.log(f"removed {args.key} from {dacli.CONFIG_PATH}")
            removed_anywhere = True
    if not removed_anywhere:
        dacli.log(f"(nothing to unset: {args.key})", "warn")


def cmd_config_path(args: argparse.Namespace) -> None:
    print(f"config: {dacli.CONFIG_PATH}")
    print(f"state:  {dacli.STATE_PATH}")


def cmd_auth_logout(args: argparse.Namespace) -> None:
    """Clear the local token state. The DA app remains authorised; revoke at
    https://www.deviantart.com/settings/applications if you want to fully
    invalidate the refresh token server-side.
    """
    if dacli.STATE_PATH.exists():
        dacli.STATE_PATH.unlink()
        dacli.log(f"removed {dacli.STATE_PATH}")
        dacli.log(
            "note: DA-side authorisation is unchanged. Revoke the app at "
            "https://www.deviantart.com/settings/applications to invalidate "
            "the refresh token server-side.",
            "warn",
        )
    else:
        dacli.log("not logged in (no state file)")


def cmd_deviation_show(args: argparse.Namespace) -> None:
    """Print metadata + (optionally) raw JSON for a single deviation."""
    cfg = dacli.load_config()
    state = dacli.load_state()
    token = dacli.access_token(cfg, state)
    qs = f"deviationids[]={urllib.parse.quote(args.deviationid)}"
    body = dacli.http_json(
        f"{API_BASE}/deviation/metadata?{qs}&mature_content=true&ext_camera=true&ext_stats=true&ext_submission=true",
        token=token,
    )
    md = (body.get("metadata") or [None])[0]
    if not md:
        dacli.log(f"no metadata for deviationid {args.deviationid}", "error")
        sys.exit(2)
    if args.json:
        print(json.dumps(md, indent=2, ensure_ascii=False))
        return
    print(f"deviationid: {md.get('deviationid')}")
    print(f"title:       {md.get('title')}")
    print(f"author:      @{(md.get('author') or {}).get('username')}")
    print(f"url:         {md.get('url')}")
    print(f"is_mature:   {md.get('is_mature')}")
    # .get on API data, like every other field here: a tags list whose
    # entries lack tag_name would otherwise raise KeyError mid-print,
    # after several lines had already gone to stdout.
    tags = md.get("tags") or []
    print(f"tags:        {', '.join(str(t.get('tag_name', '?')) for t in tags)}")
    desc = md.get("description") or ""
    if desc:
        # Strip simple HTML for human-readable preview
        plain = re.sub(r"<[^>]+>", "", desc)
        plain = re.sub(r"\s+", " ", plain).strip()
        print(f"\ndescription:\n  {plain[:1000]}{'...' if len(plain) > 1000 else ''}")


def cmd_daily(args: argparse.Namespace) -> None:
    """Show DeviantArt's Daily Deviation picks for a date (default today)."""
    cfg = dacli.load_config()
    state = dacli.load_state()
    token = dacli.access_token(cfg, state)
    # Build the query string explicitly — the date param is optional but
    # mature_content is always present, so we need to choose between
    # leading '?' and joining '&' depending on whether date is set.
    date = args.date or ""
    params = [f"mature_content={mature_content_param(args.mature)}"]
    if date:
        params.insert(0, f"date={urllib.parse.quote(date)}")
    body = dacli.http_json(f"{API_BASE}/browse/dailydeviations?{'&'.join(params)}", token=token)
    if getattr(args, "json", False):
        # ensure_ascii=False to match every other --json emitter. With the
        # default, five commands emitted real UTF-8 and two emitted
        # \uXXXX escapes, so a script consuming both had to handle two
        # encodings of the same DeviantArt title.
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return
    _print_results(body)
