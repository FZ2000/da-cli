"""Diagnose.

Names the test suite patches — and, for bench, the ones it swaps
wholesale — are read through the ``dacli`` package at call time.
See ADR 0007.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path

import dacli

from ..constants import (
    AUTH_DEFAULT_PORT,
    DEST_FREE_SPACE_FAIL_GIB,
    DEST_FREE_SPACE_WARN_GIB,
    REFRESH_TOKEN_CRIT_DAYS,
    REFRESH_TOKEN_TTL_DAYS,
    REFRESH_TOKEN_WARN_DAYS,
    TERMINAL_STOP_REASONS,
    WATCHED_ALL_COMPLETE,
)
from ..errors import AuthError, HttpError


# --------------------------------------------------------------------------
# Diagnose: end-to-end self-test
#
# `da diagnose` answers "is the CLI configured correctly and ready to
# sync, or is something quietly broken?". It checks every layer that
# can silently break an unattended scheduled run (token expiry, scope mismatch,
# destination unwritable, launchd job not loaded, index drift, missing
# config) and prints a categorized PASS/WARN/FAIL report with an exit
# code suitable for shell pipelines.
#
# Exit codes:
#   0 — every check passed
#   1 — at least one warning (run still possible, but degraded)
#   2 — at least one critical failure (next sync will fail)
# --------------------------------------------------------------------------
def _diagnose_checks() -> list[tuple[str, str, str]]:
    """Run every check, return list of (level, section, message) tuples.

    Pure function (modulo filesystem reads + one launchctl call) — no
    side effects, no exits. Caller decides how to render and what exit
    code to use. Lets tests assert on the structured output directly.
    """
    out: list[tuple[str, str, str]] = []
    cfg = dacli.load_config()
    state = dacli.load_state()

    # --- Config ---
    if cfg.get("client_id"):
        out.append(("ok", "config", f"client_id: {cfg['client_id']}"))
    else:
        out.append(("fail", "config", "client_id not set — run `da config set client_id <ID>`"))

    if cfg.get("destination"):
        # expanduser to match _ensure_destination — without it a
        # perfectly working "~/dir" destination reported a hard FAIL
        # (exit 2), poisoning the cron/monitoring use case diagnose exists for.
        dest = Path(os.path.expanduser(str(cfg["destination"])))
        if dest.exists() and os.access(dest, os.W_OK):
            out.append(("ok", "config", f"destination: {dest} (writable)"))
            # Free-space check: a sync that fills the disk leaves half-written
            # .part files (cleaned up on next run) but the user wants warning
            # *before* they kick off a long sync. < 1 GiB is the trip wire —
            # an average DA deviation is ~500 KB-2 MB, so 1 GiB ≈ 500-2000
            # more saves before catastrophic failure.
            try:
                free = shutil.disk_usage(dest).free
            except OSError as e:
                out.append(("warn", "config", f"could not check free space on {dest}: {e}"))
            else:
                gib = free / (1024**3)
                if gib < DEST_FREE_SPACE_FAIL_GIB:
                    out.append(
                        (
                            "fail",
                            "config",
                            f"destination has {gib:.2f} GiB free — sync will fail soon",
                        )
                    )
                elif gib < DEST_FREE_SPACE_WARN_GIB:
                    out.append(("warn", "config", f"destination has {gib:.1f} GiB free (low)"))
                else:
                    out.append(("ok", "config", f"destination free space: {gib:.1f} GiB"))
        elif not dest.exists():
            out.append(("fail", "config", f"destination: {dest} does not exist"))
        else:
            out.append(("fail", "config", f"destination: {dest} not writable by current user"))
    else:
        out.append(
            (
                "fail",
                "config",
                "destination not set — sync will exit 2; "
                "set it with `da config set destination <PATH>`",
            )
        )

    redirect_uri = cfg.get("redirect_uri") or f"https://localhost:{AUTH_DEFAULT_PORT}/"
    if redirect_uri.startswith("http://"):
        out.append(
            (
                "warn",
                "config",
                "redirect_uri uses HTTP — DA's dashboard rejects HTTP whitelist entries; "
                "prefer HTTPS",
            )
        )
    else:
        out.append(("ok", "config", f"redirect_uri: {redirect_uri}"))

    # --- Token / scope ---
    if not state.get("access_token"):
        out.append(("fail", "auth", "no access_token in state — run `da auth`"))
    else:
        exp = state.get("expires_at", 0)
        scope = state.get("scope", "?")
        ttl = exp - time.time() if exp else 0
        if ttl > 60:
            out.append(
                ("ok", "auth", f"access_token valid for {int(ttl / 60)} min (scope={scope})")
            )
        elif state.get("refresh_token"):
            # Token expired, refresh_token present in state.json — that
            # means the LOCAL view is "we can still refresh," but DA
            # may have invalidated the grant on their end (this is the
            # silent-multi-hour-cron-failure failure mode: diagnose
            # reported "refresh available" without ever exercising it).
            # Actually exchange the refresh token here so diagnose tells
            # the operator the truth. One HTTP call, fires only when
            # the access_token is already due to refresh anyway, so the
            # operational cost is minimal.
            try:
                dacli.access_token(cfg, state, force_refresh=True)
                out.append(("ok", "auth", f"refresh_token live with DA (scope={scope})"))
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                # Offline / DNS down / DA unreachable. Not a credential
                # problem, and diagnose is the tool you run unattended —
                # it must report, not traceback out with a generic exit 1.
                out.append(
                    (
                        "warn",
                        "auth",
                        f"could not reach DA token endpoint ({type(e).__name__}) — "
                        f"refresh_token liveness unverified",
                    )
                )
            except AuthError as e:
                # DA rejected the grant: a real credential failure, so
                # fail-level. Previously this caught SystemExit, which
                # access_token also raised for an unreachable network —
                # reporting "run `da auth`" for a dropped wifi connection.
                out.append(("fail", "auth", f"{e} "[:200].strip()))
            except HttpError as e:
                # Could not ask. Not a statement about the credential.
                out.append(
                    (
                        "warn",
                        "auth",
                        f"could not verify refresh_token liveness: {e}",
                    )
                )
        else:
            out.append(
                ("fail", "auth", "access_token expired and no refresh_token — re-run `da auth`")
            )

    # --- refresh_token chain TTL (the 90-day ceiling) ---------------------
    # DA's refresh tokens die 90 days after issue. We can't extend it.
    # Surface the remaining lifetime so the user isn't surprised by a
    # dead token at 03:00 with no UI to surface the failure.
    issued_at = state.get("refresh_token_issued_at")
    if isinstance(issued_at, (int, float)) and issued_at > 0:
        days_remaining = REFRESH_TOKEN_TTL_DAYS - (time.time() - issued_at) / 86400.0
        if days_remaining <= REFRESH_TOKEN_CRIT_DAYS:
            out.append(
                (
                    "fail",
                    "auth",
                    f"refresh_token chain expires in {days_remaining:.1f} days — "
                    f"re-run `da auth` NOW or daily sync will start failing",
                )
            )
        elif days_remaining <= REFRESH_TOKEN_WARN_DAYS:
            out.append(
                (
                    "warn",
                    "auth",
                    f"refresh_token chain expires in {days_remaining:.1f} days — "
                    f"re-run `da auth` soon",
                )
            )
        else:
            out.append(
                ("ok", "auth", f"refresh_token chain: {days_remaining:.0f} days remaining (of 90)")
            )

    # --- Index health ---
    if dacli.INDEX_PATH.exists():
        try:
            n = dacli.index_count()
            sz_kb = dacli.INDEX_PATH.stat().st_size // 1024
            out.append(("ok", "index", f"{n:,} synced rows, {sz_kb} KB on disk"))
        except sqlite3.DatabaseError as e:
            out.append(
                ("fail", "index", f"index DB unreadable ({e}) — delete + `da index rebuild`")
            )
    else:
        out.append(("warn", "index", "no index yet — first sync will bootstrap from disk"))

    # --- Last sync (from state) ---
    last = state.get("last_sync") or {}
    if isinstance(last, dict) and last.get("ended_at"):
        ago_s = time.time() - int(last["ended_at"])
        if ago_s > 86400:
            ago = f"{ago_s / 86400:.1f}d ago"
        elif ago_s > 3600:
            ago = f"{ago_s / 3600:.1f}h ago"
        else:
            ago = f"{int(ago_s / 60)}m ago"
        kind = last.get("kind", "?")
        totals = last.get("totals") or {}
        reason = last.get("stop_reason", "?")
        # The level has to follow the stop_reason. Reporting every run as
        # "ok" made a truncated walk, an HTTP 429 abort and a clean pass
        # indistinguishable to a monitor — a nightly job that never
        # finishes looked perfectly healthy right up until you noticed
        # the gallery was missing a year of art.
        #
        # Everything that is not a clean finish is a warning, never a
        # failure: each of these resolves itself on the next run (a
        # bigger budget, a lifted rate limit, an artist who un-deleted
        # their gallery). `da diagnose` exits 2 on a failure, and waking
        # someone at 03:00 for a truncated backfill would be wrong.
        clean = TERMINAL_STOP_REASONS | {WATCHED_ALL_COMPLETE}
        level = "ok" if reason in clean and not int(totals.get("fail", 0)) else "warn"
        out.append(
            (
                level,
                "last sync",
                f"{kind} ended {ago} ({reason}) — totals={dict(totals)}",
            )
        )
    else:
        out.append(("warn", "last sync", "no completed sync recorded yet"))

    # --- launchd schedule (macOS only) ---
    if sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["launchctl", "list", "com.fz2000.da-cli"],
                capture_output=True,
                text=True,
                check=False,
            )
            if r.returncode == 0:
                # Check for last_exit_status — non-zero means last fire failed
                exit_match = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', r.stdout)
                last_exit = int(exit_match.group(1)) if exit_match else 0
                if last_exit == 0:
                    out.append(("ok", "schedule", "launchd job loaded; last exit 0"))
                else:
                    msg = (
                        f"launchd job loaded; last exit {last_exit} — see ~/Library/Logs/da-cli.log"
                    )
                    out.append(("warn", "schedule", msg))
            else:
                out.append(
                    (
                        "warn",
                        "schedule",
                        "launchd job not loaded — run `./install_schedule.sh` to install",
                    )
                )
        except FileNotFoundError:
            pass  # No launchctl on this platform

    # --- Loopback TLS cert (if redirect_uri is https://localhost) ---
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.scheme == "https" and (parsed.hostname or "") in ("localhost", "127.0.0.1"):
        if dacli.LOOPBACK_CERT.exists() and dacli.LOOPBACK_KEY.exists():
            out.append(("ok", "tls", "loopback cert generated"))
        else:
            out.append(
                ("warn", "tls", "loopback cert not yet generated — first `da auth` will create it")
            )

    return out


def cmd_diagnose(args: argparse.Namespace) -> None:
    """Run every health check, print a categorized report, exit with
    a status code matching the worst finding.

    Pass `--json` to emit a machine-readable structure suitable for
    piping into observability tools (e.g. an alerting cron). The JSON
    schema is stable: `{timestamp, overall: {status, warnings, criticals},
    findings: [{level, section, message}, ...], exit_code}`.
    """
    findings = _diagnose_checks()
    crit = sum(1 for level, _, _ in findings if level == "fail")
    warn = sum(1 for level, _, _ in findings if level == "warn")
    if crit:
        overall_status = "FAIL"
        exit_code = 2
    elif warn:
        overall_status = "WARN"
        exit_code = 1
    else:
        overall_status = "OK"
        exit_code = 0

    if getattr(args, "json", False):
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "overall": {
                "status": overall_status,
                "criticals": crit,
                "warnings": warn,
            },
            "findings": [
                {"level": level, "section": section, "message": msg}
                for level, section, msg in findings
            ],
            "exit_code": exit_code,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        sys.exit(exit_code)

    width = 64
    print("=" * width)
    print(f"da-cli diagnose — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * width)
    markers = {"ok": "✓", "warn": "⚠", "fail": "✗"}
    for level, section, msg in findings:
        print(f"  {markers[level]} [{section:10s}] {msg}")
    print("-" * width)
    if crit:
        print(f"OVERALL: FAIL  ({crit} critical, {warn} warnings)")
    elif warn:
        print(f"OVERALL: WARN  ({warn} warnings)")
    else:
        print("OVERALL: OK")
    sys.exit(exit_code)
