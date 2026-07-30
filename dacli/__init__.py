"""
da — DeviantArt CLI

Single-file Python 3 CLI using only the standard library. Subcommands:

  auth, whoami, refresh         — OAuth 2.1 (PKCE) lifecycle
  config show|set|get|unset|path — manage configuration
  sync feed|artist|watched      — pull deviations into a local folder
  search tag|topic|topics|...   — browse/search DA content
  daily [YYYY-MM-DD]            — Daily Deviation picks
  user profile <username>       — user metadata
  watch list                    — your watched users
  deviation show|morelikethis   — single-deviation lookups
  index show|rebuild            — manage the synced-deviation SQLite index
  diagnose                      — end-to-end health check
  bench                         — synthetic sync benchmark (no network)

Configuration priority (highest first):
  1. CLI flags
  2. Environment variables (DA_CLIENT_ID, DA_CLIENT_SECRET, DA_DESTINATION, ...)
  3. macOS Keychain  (only used for SECRETS like client_secret; service "da-cli")
  4. ~/.config/da-cli/config.json (mode 0600)

State (tokens, sync checkpoints) lives in ~/.local/state/da-cli/state.json (0600).
Neither path is tracked by git.

This file deliberately depends on stdlib only — no pip install — so the CLI
works in fresh environments and is auditable.
"""

import argparse
import contextlib
import errno
import json
import os
import ssl
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Re-exports from the split-out submodules, so `dacli.X` keeps resolving
# for callers and for the test suite (see per-file-ignores in pyproject).
from .auth import (
    LOOPBACK_CERT as LOOPBACK_CERT,
)
from .auth import (
    LOOPBACK_KEY as LOOPBACK_KEY,
)
from .auth import (
    _capture_code_via_paste as _capture_code_via_paste,
)
from .auth import (
    _cmd_auth_status as _cmd_auth_status,
)
from .auth import (
    _ensure_self_signed_cert as _ensure_self_signed_cert,
)
from .auth import (
    access_token,
    authed_http_json,
    cmd_auth,
    cmd_refresh,
    cmd_whoami,
)
from .commands.bench import (
    cmd_bench,
)
from .commands.config import (
    cmd_auth_logout,
    cmd_config_get,
    cmd_config_path,
    cmd_config_set,
    cmd_config_show,
    cmd_config_unset,
    cmd_daily,
    cmd_deviation_show,
)
from .commands.diagnose import (
    _diagnose_checks as _diagnose_checks,
)
from .commands.diagnose import (
    cmd_diagnose,
)
from .commands.index import (
    cmd_index_rebuild,
    cmd_index_show,
)
from .commands.search import (
    cmd_deviation_morelikethis,
    cmd_search_newest,
    cmd_search_popular,
    cmd_search_tag,
    cmd_search_tagsuggest,
    cmd_search_topic,
    cmd_search_topics,
    cmd_search_toptopics,
    cmd_search_user,
)
from .commands.user import (
    cmd_user_profile,
    cmd_watch_list,
)
from .config import (
    _STATE_CORRUPTION_WARNED as _STATE_CORRUPTION_WARNED,
)
from .config import (
    SECRET_KEYS as SECRET_KEYS,
)
from .config import (
    _keychain_get as _keychain_get,
)
from .config import (
    _keychain_set as _keychain_set,
)
from .config import (
    load_config,
    load_state,
    save_state,
    set_config_field,
)
from .constants import (
    API_BASE,
    AUTH_URL,
    CONFIG_DIR,
    CONFIG_PATH,
    DEFAULT_CONCURRENCY,
    DEFAULT_DELAY_API,
    DEFAULT_DELAY_IMAGE,
    DEFAULT_JITTER,
    DEFAULT_LIMIT,
    INDEX_PATH,
    JSON_HELP,
    KEYCHAIN_SERVICE,
    STATE_DIR,
    STATE_PATH,
    TOKEN_URL,
    USER_AGENT,
    __version__,
    jitter_sleep,
    jittered,
    mature_content_param,
)
from .constants import (
    AUTH_DEFAULT_PORT as AUTH_DEFAULT_PORT,
)
from .constants import (
    AUTH_LISTENER_TIMEOUT_S as AUTH_LISTENER_TIMEOUT_S,
)
from .constants import (
    CONCURRENCY_MAX as CONCURRENCY_MAX,
)
from .constants import (
    CONCURRENCY_MIN as CONCURRENCY_MIN,
)
from .constants import (
    DEST_FREE_SPACE_FAIL_GIB as DEST_FREE_SPACE_FAIL_GIB,
)
from .constants import (
    DEST_FREE_SPACE_WARN_GIB as DEST_FREE_SPACE_WARN_GIB,
)
from .constants import (
    FEED_PAGE_CAP as FEED_PAGE_CAP,
)
from .constants import (
    GALLERY_PAGE_CAP as GALLERY_PAGE_CAP,
)
from .constants import (
    HTTP_RETRY_BACKOFF_BASE_S as HTTP_RETRY_BACKOFF_BASE_S,
)
from .constants import (
    HTTP_RETRY_DEFAULT as HTTP_RETRY_DEFAULT,
)
from .constants import (
    HTTP_TIMEOUT_BYTES_S as HTTP_TIMEOUT_BYTES_S,
)
from .constants import (
    HTTP_TIMEOUT_JSON_S as HTTP_TIMEOUT_JSON_S,
)
from .constants import (
    JITTER_FLOOR_S as JITTER_FLOOR_S,
)
from .constants import (
    JITTER_MAX_PCT as JITTER_MAX_PCT,
)
from .constants import (
    LOG_BODY_TRUNCATE as LOG_BODY_TRUNCATE,
)
from .constants import (
    LOOPBACK_CERT_KEY_BITS as LOOPBACK_CERT_KEY_BITS,
)
from .constants import (
    LOOPBACK_CERT_VALIDITY_DAYS as LOOPBACK_CERT_VALIDITY_DAYS,
)
from .constants import (
    METADATA_BATCH_SIZE as METADATA_BATCH_SIZE,
)
from .constants import (
    REFRESH_TOKEN_CRIT_DAYS as REFRESH_TOKEN_CRIT_DAYS,
)
from .constants import (
    REFRESH_TOKEN_TTL_DAYS as REFRESH_TOKEN_TTL_DAYS,
)
from .constants import (
    REFRESH_TOKEN_WARN_DAYS as REFRESH_TOKEN_WARN_DAYS,
)
from .constants import (
    TOKEN_REFRESH_SKEW_S as TOKEN_REFRESH_SKEW_S,
)
from .errors import AuthError, ConfigError, DacliError, HttpError, SyncError
from .index import (
    _BOOTSTRAP_CHECKED_THIS_PROCESS as _BOOTSTRAP_CHECKED_THIS_PROCESS,
)
from .index import (
    _index as _index,
)
from .index import (
    _index_close as _index_close,
)
from .index import (
    index_add,
    index_bootstrap_if_empty,
    index_count,
    index_filter_known,
    index_has,
    index_rebuild_from_disk,
)
from .lock import CommandLockedError
from .lock import _cmd_lock as _cmd_lock
from .net import _request as _request
from .net import http_bytes, http_json, http_post_json
from .output import (
    _OUTPUT_STATE,
    _configure_output,
    log,
    mask_secret,
    safe_filename,
)
from .output import (
    _atomic_write as _atomic_write,
)
from .sync import (
    _cmd_sync_artist_impl as _cmd_sync_artist_impl,
)
from .sync import (
    _concurrency as _concurrency,
)
from .sync import (
    _discover_watched_via_feed as _discover_watched_via_feed,
)
from .sync import (
    _ensure_destination as _ensure_destination,
)
from .sync import (
    _fetch_metadata_batch as _fetch_metadata_batch,
)
from .sync import (
    _list_watched_via_friends as _list_watched_via_friends,
)
from .sync import (
    _resolve_folder as _resolve_folder,
)
from .sync import (
    _save_one as _save_one,
)
from .sync import (
    _save_page_concurrent as _save_page_concurrent,
)
from .sync import (
    cmd_sync_artist,
    cmd_sync_feed,
    cmd_sync_watched,
)

# Public surface. `from dacli import *` and external re-imports (the
# entry-point, the tests) get exactly these names; everything else is
# considered private (the leading-underscore convention is enforced by
# the SLF001 lint rule for non-test code).
__all__ = [
    "API_BASE",
    "AUTH_URL",
    "CONFIG_DIR",
    # Module-level constants that downstream tooling (bench, diagnose)
    # may reasonably want to read or override.
    "CONFIG_PATH",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_DELAY_API",
    "DEFAULT_DELAY_IMAGE",
    "DEFAULT_JITTER",
    "DEFAULT_LIMIT",
    "INDEX_PATH",
    "KEYCHAIN_SERVICE",
    "STATE_DIR",
    "STATE_PATH",
    "TOKEN_URL",
    "USER_AGENT",
    "AuthError",
    "CommandLockedError",
    "ConfigError",
    "DacliError",
    "HttpError",
    "SyncError",
    "__version__",
    "access_token",
    "authed_http_json",
    "build_parser",
    "http_bytes",
    "http_json",
    "http_post_json",
    "index_add",
    "index_bootstrap_if_empty",
    "index_count",
    "index_filter_known",
    "index_has",
    "index_rebuild_from_disk",
    "jitter_sleep",
    "jittered",
    "load_config",
    "load_state",
    "log",
    "main",
    "mask_secret",
    "mature_content_param",
    "safe_filename",
    "save_state",
    "set_config_field",
]


# --------------------------------------------------------------------------
# argparse wiring
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """Build the full argparse tree.

    Each subparser is named after what it parses (``feed_parser``,
    ``artist_parser``, etc.) so the wiring is greppable and a newcomer
    can locate the handler for any CLI verb in one jump. Handler
    functions are wired via ``set_defaults(func=cmd_*)``; ``main()``
    dispatches on ``args.func``.
    """
    parser = argparse.ArgumentParser(prog="da", description="DeviantArt CLI")
    parser.add_argument("--version", action="version", version=f"da-cli {__version__}")
    # Global output-control flags — applied in main() via _configure_output()
    # before the subcommand runs. Mutually exclusive to avoid the
    # `--quiet --verbose` ambiguity; argparse renders a helpful error.
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Emit debug-level log lines (HTTP retry traces, internal decisions).",
    )
    output_group.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress info-level output; only warn and error reach the log. "
        "Cron / launchd-friendly.",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="Color output for warn/error markers. 'auto' (default) enables "
        "color on a TTY that doesn't set NO_COLOR; 'never' forces plain; "
        "'always' forces color even when piped.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Override the config file path (default: $XDG_CONFIG_HOME/da-cli/config.json "
        "or ~/.config/da-cli/config.json). This moves the config file only — tokens, the "
        "sync checkpoint and the index stay where they are. For a fully separate second "
        "account, set XDG_CONFIG_HOME and XDG_STATE_HOME instead.",
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    # ---- auth -------------------------------------------------------------
    auth_parser = subparsers.add_parser("auth", help="OAuth 2.1 PKCE flow / log out")
    auth_subparsers = auth_parser.add_subparsers(dest="auth_cmd")
    # `da auth` (no subcommand) → login flow, preserving prior behaviour.
    auth_parser.add_argument(
        "--redirect-uri",
        help="OAuth redirect URI. Loopback (http://localhost:PORT/) runs a "
        "local listener; anything else uses paste-back mode (DA now requires "
        "HTTPS for non-localhost). Must be whitelisted on the OAuth app.",
    )
    auth_parser.add_argument(
        "--paste",
        action="store_true",
        help="Force paste-back flow even for a loopback redirect_uri.",
    )
    auth_parser.add_argument("--scope", default="browse")
    auth_parser.set_defaults(func=cmd_auth)
    auth_logout_parser = auth_subparsers.add_parser("logout", help="Delete local token state")
    auth_logout_parser.set_defaults(func=cmd_auth_logout)
    auth_status_parser = auth_subparsers.add_parser(
        "status",
        help="Print JSON describing the refresh_token chain's remaining "
        "lifetime, having first confirmed with DeviantArt that the "
        "credentials still work. Exits 0 (ok), 1 (warn), or 2 "
        "(crit/unknown/revoked/unreachable). Suitable for cron / launchd "
        "health checks. Use `da diagnose` for a TTL reading without network.",
    )
    auth_status_parser.set_defaults(func=cmd_auth)

    subparsers.add_parser("whoami", help="Verify token + show identity").set_defaults(
        func=cmd_whoami
    )
    subparsers.add_parser("refresh", help="Force-refresh the access token").set_defaults(
        func=cmd_refresh
    )

    # ---- config -----------------------------------------------------------
    config_parser = subparsers.add_parser("config", help="Manage configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_cmd", required=True)
    config_subparsers.add_parser("show", help="Print config (secrets masked)").set_defaults(
        func=cmd_config_show
    )
    config_subparsers.add_parser("path", help="Print config + state file paths").set_defaults(
        func=cmd_config_path
    )
    config_set_parser = config_subparsers.add_parser("set", help="Set a config field")
    config_set_parser.add_argument("key")
    config_set_parser.add_argument("value")
    config_set_parser.set_defaults(func=cmd_config_set)
    config_get_parser = config_subparsers.add_parser(
        "get", help="Read a config field (secrets masked)"
    )
    config_get_parser.add_argument("key")
    config_get_parser.add_argument(
        "--unmask",
        action="store_true",
        help="Print secrets in plaintext (don't pipe to logs / shared terminals)",
    )
    config_get_parser.set_defaults(func=cmd_config_get)
    config_unset_parser = config_subparsers.add_parser("unset", help="Remove a config field")
    config_unset_parser.add_argument("key")
    config_unset_parser.set_defaults(func=cmd_config_unset)

    # ---- sync -------------------------------------------------------------
    sync_parser = subparsers.add_parser("sync", help="Sync DA content into the local destination")
    sync_subparsers = sync_parser.add_subparsers(dest="sync_cmd", required=True)

    feed_parser = sync_subparsers.add_parser("feed", help="Walk watch feed top-down (incremental)")
    feed_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    # Sync commands default --mature on (search/browse default off): a
    # filtered sync silently skips items mid-gallery, which presents as
    # data loss on an unattended run. Pass --no-mature to filter.
    feed_parser.add_argument("--mature", action=argparse.BooleanOptionalAction, default=True)
    feed_parser.add_argument(
        "--time-budget",
        type=int,
        default=540,
        metavar="SECONDS",
        help="Wall-clock cap for the whole walk (default: 540). The walk stops "
        "cleanly a few seconds early so the page in flight can finish. A run cut "
        "short here does not advance the checkpoint, and `da diagnose` reports it "
        "as a warning rather than a clean sync.",
    )
    feed_parser.add_argument("--delay-api", type=float, default=None)
    feed_parser.add_argument("--delay-image", type=float, default=None)
    feed_parser.add_argument(
        "--jitter",
        type=float,
        default=None,
        help="Randomise each sleep by ±PCT of base (0-0.95). "
        "0.4 makes a 1.5s base sleep 0.9-2.1s. Default 0 (off).",
    )
    feed_parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=f"Parallel image-download workers per page. Default {DEFAULT_CONCURRENCY}, "
        "max 16. 1 disables concurrency.",
    )
    feed_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be downloaded without writing any files or "
        "touching the index. Useful for previewing a first-time sync or "
        "auditing what --full would refetch.",
    )
    feed_parser.set_defaults(func=cmd_sync_feed)

    artist_parser = sync_subparsers.add_parser("artist", help="Walk a single artist's gallery")
    artist_parser.add_argument("artist")
    artist_parser.add_argument(
        "--offset",
        type=int,
        default=None,
        metavar="N",
        help="Start at this page offset. Omit to continue from where the last "
        "unfinished walk of this gallery stopped.",
    )
    artist_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    artist_parser.add_argument("--mature", action=argparse.BooleanOptionalAction, default=True)
    artist_parser.add_argument(
        "--time-budget",
        type=int,
        default=540,
        metavar="SECONDS",
        help="Wall-clock cap for this gallery walk (default: 540). On a truncated "
        "run the resume offset is printed and recorded.",
    )
    artist_parser.add_argument("--delay-api", type=float, default=None)
    artist_parser.add_argument("--delay-image", type=float, default=None)
    artist_parser.add_argument(
        "--full",
        action="store_true",
        help="Disable the synced-index early-stop and walk the entire gallery. "
        "Use this if you suspect missed deviations or have rotated content.",
    )
    artist_parser.add_argument(
        "--jitter",
        type=float,
        default=None,
        help="Randomise each sleep by ±PCT (0-0.95). Default 0.",
    )
    artist_parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=f"Parallel image-download workers per page. Default {DEFAULT_CONCURRENCY}, max 16.",
    )
    artist_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be downloaded without writing any files.",
    )
    artist_parser.set_defaults(func=cmd_sync_artist)

    watched_parser = sync_subparsers.add_parser("watched", help="Walk every watched user's gallery")
    # Mutually exclusive, because they are two different answers to "how do
    # I find the artists". --via-feed's help says it skips the friends
    # endpoint "entirely", but `if args.user:` in _cmd_sync_watched_impl
    # silently won — so passing both queried /user/friends anyway and only
    # honoured --via-feed if that happened to return 403. argparse now
    # rejects the contradiction instead of the CLI quietly resolving it.
    watched_discovery = watched_parser.add_mutually_exclusive_group()
    watched_discovery.add_argument(
        "--user",
        help="Skip /user/whoami and use this username for /user/friends/{user} "
        "(needs `user` scope; otherwise the feed fallback is used)",
    )
    watched_discovery.add_argument(
        "--via-feed",
        action="store_true",
        help="Skip the friends endpoint entirely and discover artists "
        "by walking the watch feed (works with `browse` scope alone, "
        "but may miss watched users who haven't posted recently)",
    )
    watched_parser.add_argument(
        "--feed-max",
        type=int,
        default=2000,
        help="Cap on deviations to scan when discovering via feed (default 2000)",
    )
    watched_parser.add_argument("--mature", action=argparse.BooleanOptionalAction, default=True)
    watched_parser.add_argument(
        "--time-budget",
        type=int,
        default=540,
        metavar="SECONDS",
        help="Wall-clock cap for the ENTIRE run across all artists (default: 540) "
        "— not per artist. Each artist is given whatever remains; once too little "
        "is left to be useful the rest are skipped and the run is recorded as "
        "truncated. Re-run to continue.",
    )
    watched_parser.add_argument("--delay-api", type=float, default=None)
    watched_parser.add_argument("--delay-image", type=float, default=None)
    watched_parser.add_argument(
        "--full",
        action="store_true",
        help="Disable per-artist early-stop. Walks every page of every "
        "watched artist's gallery. Slow; use only for paranoid backfills.",
    )
    watched_parser.add_argument(
        "--jitter",
        type=float,
        default=None,
        help="Randomise each sleep by ±PCT (0-0.95). Default 0.",
    )
    watched_parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=f"Parallel image-download workers per page. Default {DEFAULT_CONCURRENCY}, max 16.",
    )
    watched_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be downloaded without writing any files.",
    )
    watched_parser.set_defaults(func=cmd_sync_watched)

    # ---- search / browse --------------------------------------------------
    search_parser = subparsers.add_parser("search", help="Search/browse DA content")
    search_subparsers = search_parser.add_subparsers(dest="search_cmd", required=True)

    popular_parser = search_subparsers.add_parser("popular")
    popular_parser.add_argument("--limit", type=int, default=10)
    popular_parser.add_argument("--mature", action="store_true")
    popular_parser.add_argument("--json", action="store_true", help=JSON_HELP)
    popular_parser.set_defaults(func=cmd_search_popular)

    newest_parser = search_subparsers.add_parser("newest")
    newest_parser.add_argument("--limit", type=int, default=10)
    newest_parser.add_argument("--mature", action="store_true")
    newest_parser.add_argument("--json", action="store_true", help=JSON_HELP)
    newest_parser.set_defaults(func=cmd_search_newest)

    tag_parser = search_subparsers.add_parser("tag")
    tag_parser.add_argument("tag")
    tag_parser.add_argument("--limit", type=int, default=10)
    tag_parser.add_argument("--mature", action="store_true")
    tag_parser.add_argument("--json", action="store_true", help=JSON_HELP)
    tag_parser.set_defaults(func=cmd_search_tag)

    user_search_parser = search_subparsers.add_parser("user")
    user_search_parser.add_argument("query", nargs="+")
    user_search_parser.add_argument("--json", action="store_true", help=JSON_HELP)
    user_search_parser.set_defaults(func=cmd_search_user)

    # Curated-topic / morelikethis / tag-suggest endpoints. Topics return
    # editorial-curated result sets (cleaner than user-tagged searches);
    # morelikethis rides DA's similarity model.
    topic_parser = search_subparsers.add_parser(
        "topic",
        help=(
            "Fetch deviations from a curated DA topic "
            "(e.g. digitalart, nature, fantasy, animals). "
            "Use `search topics` to list valid names."
        ),
    )
    topic_parser.add_argument("topic", help="canonical topic name (lowercase, single token)")
    topic_parser.add_argument("--limit", type=int, default=10)
    topic_parser.add_argument("--mature", action="store_true")
    topic_parser.add_argument("--json", action="store_true", help=JSON_HELP)
    topic_parser.set_defaults(func=cmd_search_topic)

    topics_parser = search_subparsers.add_parser("topics", help="List all DA topics (paginated)")
    topics_parser.add_argument("--limit", type=int, default=10)
    topics_parser.add_argument("--offset", type=int, default=0)
    topics_parser.add_argument("--mature", action="store_true")
    topics_parser.add_argument("--json", action="store_true", help=JSON_HELP)
    topics_parser.set_defaults(func=cmd_search_topics)

    top_topics_parser = search_subparsers.add_parser(
        "toptopics", help="Fetch top topics with one example deviation each"
    )
    top_topics_parser.add_argument("--mature", action="store_true")
    top_topics_parser.add_argument("--json", action="store_true", help=JSON_HELP)
    top_topics_parser.set_defaults(func=cmd_search_toptopics)

    tag_suggest_parser = search_subparsers.add_parser(
        "tag-suggest", help="Autocomplete a tag prefix — validate before searching"
    )
    tag_suggest_parser.add_argument("prefix")
    tag_suggest_parser.add_argument("--json", action="store_true", help=JSON_HELP)
    tag_suggest_parser.set_defaults(func=cmd_search_tagsuggest)

    # ---- user / deviation / daily ----------------------------------------
    user_parser = subparsers.add_parser("user", help="User-related queries")
    user_subparsers = user_parser.add_subparsers(dest="user_cmd", required=True)
    profile_parser = user_subparsers.add_parser("profile")
    profile_parser.add_argument("username")
    profile_parser.set_defaults(func=cmd_user_profile)

    deviation_parser = subparsers.add_parser("deviation", help="Deviation-level queries")
    deviation_subparsers = deviation_parser.add_subparsers(dest="deviation_cmd", required=True)
    deviation_show_parser = deviation_subparsers.add_parser(
        "show", help="Print metadata for a single deviation"
    )
    deviation_show_parser.add_argument("deviationid")
    deviation_show_parser.add_argument(
        "--json", action="store_true", help="Emit raw JSON instead of a summary"
    )
    deviation_show_parser.set_defaults(func=cmd_deviation_show)

    morelikethis_parser = deviation_subparsers.add_parser(
        "morelikethis",
        help="Fetch DA's 'More Like This' suggestions for a seed deviation",
    )
    morelikethis_parser.add_argument("deviationid", help="seed deviation UUID")
    morelikethis_parser.add_argument(
        "--limit", type=int, default=10, help="max items per group (artist / DA)"
    )
    morelikethis_parser.add_argument("--mature", action="store_true")
    morelikethis_parser.add_argument("--json", action="store_true", help=JSON_HELP)
    morelikethis_parser.set_defaults(func=cmd_deviation_morelikethis)

    daily_parser = subparsers.add_parser("daily", help="DA Daily Deviation picks for a date")
    daily_parser.add_argument("date", nargs="?", help="YYYY-MM-DD; default today")
    daily_parser.add_argument("--mature", action="store_true")
    daily_parser.add_argument("--json", action="store_true", help=JSON_HELP)
    daily_parser.set_defaults(func=cmd_daily)

    # ---- index ------------------------------------------------------------
    index_parser = subparsers.add_parser(
        "index",
        help="Manage the synced-deviation index (powers fast incremental sync)",
    )
    index_subparsers = index_parser.add_subparsers(dest="index_cmd", required=True)
    index_subparsers.add_parser(
        "rebuild",
        help="Walk the destination and rebuild the index from existing files. "
        "Idempotent — safe to re-run.",
    ).set_defaults(func=cmd_index_rebuild)
    index_subparsers.add_parser(
        "show",
        help="Print index stats (total rows, top artists, db size)",
    ).set_defaults(func=cmd_index_show)

    # ---- diagnose / bench -------------------------------------------------
    diagnose_parser = subparsers.add_parser(
        "diagnose",
        help="End-to-end self-test: config, auth, scope, destination, index, "
        "last sync, schedule. Exits 0 if all OK, 1 on warnings, 2 on critical.",
    )
    diagnose_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human-readable report. "
        "Schema: {timestamp, overall: {status, warnings, criticals}, "
        "findings: [{level, section, message}], exit_code}.",
    )
    diagnose_parser.set_defaults(func=cmd_diagnose)

    bench_parser = subparsers.add_parser(
        "bench",
        help="Run a synthetic sync benchmark against a fully mocked HTTP "
        "layer to measure CLI overhead. No network. Useful for perf "
        "regression checks.",
    )
    bench_parser.add_argument(
        "--pages", type=int, default=10, help="Pages of synthetic feed (default 10)"
    )
    bench_parser.add_argument(
        "--per-page",
        type=int,
        default=24,
        help="Deviations per page (default 24, DA's gallery cap)",
    )
    bench_parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="Image-download workers per page (default 4)",
    )
    bench_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the summary table",
    )
    bench_parser.set_defaults(func=cmd_bench)

    # ---- watch ------------------------------------------------------------
    watch_parser = subparsers.add_parser("watch", help="Your watch list")
    watch_subparsers = watch_parser.add_subparsers(dest="watch_cmd", required=True)
    watch_list_parser = watch_subparsers.add_parser("list")
    watch_list_parser.add_argument("--limit", type=int, default=50)
    watch_list_parser.add_argument("--offset", type=int, default=0)
    watch_list_parser.set_defaults(func=cmd_watch_list)

    return parser


def _advice_for_http(e: urllib.error.HTTPError) -> str:
    """What went wrong, and what to do about it.

    Every branch here names an action. "HTTP 401" alone tells a reader
    nothing they can act on, and the whole point of catching an error
    rather than letting it crash is that we can say something better.
    """
    if e.code in (401, 403):
        return (
            f"DeviantArt rejected the credentials (HTTP {e.code}).\n"
            f"Run `da auth` to sign in again. `da auth status` will confirm "
            f"whether the stored token is still accepted."
        )
    if e.code == 429:
        return (
            "DeviantArt is rate-limiting this account (HTTP 429).\n"
            "Wait a few minutes and retry. If it keeps happening, raise the "
            "pause between requests with `--delay-api` (see `da sync feed --help`)."
        )
    if 500 <= e.code < 600:
        return (
            f"DeviantArt is having trouble (HTTP {e.code} {e.reason}).\n"
            f"This is usually temporary and on their end. The next run resumes "
            f"where this one stopped; nothing has been lost."
        )
    if e.code == 404:
        return (
            f"DeviantArt has no record of that (HTTP 404): "
            f"{getattr(e, 'url', 'the requested resource')}.\n"
            f"Check the username or deviation id."
        )
    return f"DeviantArt refused the request (HTTP {e.code} {e.reason})."


def _advice_for_transport(e: OSError) -> str | None:
    """Advice for a connection that failed rather than a disk that did.

    All of these arrive as OSError subclasses, and none of them carries an
    errno worth branching on. They are also the most ordinary failures
    there are — a timeout is not a defect, and surfacing one as a
    traceback would teach people to ignore the tracebacks that matter.
    """
    if isinstance(e, TimeoutError):
        # socket.timeout IS TimeoutError, and carries no errno.
        return (
            "Timed out waiting for DeviantArt.\n"
            "Usually their end or a slow connection. The next run resumes where "
            "this one stopped; nothing has been lost."
        )
    if isinstance(e, ssl.SSLError):
        # Almost always a proxy or VPN intercepting HTTPS, or a stale
        # system trust store.
        return (
            f"Could not establish a secure connection to DeviantArt: {e}.\n"
            f"If you are behind a corporate proxy or VPN it may be intercepting "
            f"HTTPS. Check your system certificates, or try from another network."
        )
    if isinstance(e, ConnectionError):
        # ConnectionResetError, BrokenPipeError: the peer went away.
        return (
            f"The connection to DeviantArt was interrupted ({type(e).__name__}).\n"
            f"This is usually transient. The next run resumes where this one "
            f"stopped; nothing has been lost."
        )
    return None


def _advice_for_filesystem(e: OSError) -> str | None:
    """Advice for the disk failures worth anticipating, by errno."""
    where = getattr(e, "filename", None) or "the destination"
    advice = {
        errno.ENOSPC: (
            f"No space left on the disk holding {where}.\n"
            f"Free some space, or point somewhere larger with "
            f"`da config set destination <PATH>`. Downloads already on disk "
            f"are intact and will not be fetched again."
        ),
        errno.EACCES: (
            f"Permission denied writing to {where}.\n"
            f"Check you can write there, or choose another destination with "
            f"`da config set destination <PATH>`."
        ),
        errno.EROFS: (
            f"{where} is on a read-only filesystem.\n"
            f"Remount it read-write, or choose another destination with "
            f"`da config set destination <PATH>`."
        ),
        errno.ENOENT: (
            f"{where} does not exist.\n"
            f"If your destination is an external drive, mount it and retry. "
            f"`da diagnose` will confirm once it is reachable."
        ),
    }
    advice[errno.EPERM] = advice[errno.EACCES]
    return advice.get(e.errno)


def _advice_for_os_error(e: OSError) -> str | None:
    """What to tell the user, or None if this looks like a defect here.

    None is the important case: it sends the exception back out with its
    traceback, which is the honest signal for something the tool cannot
    explain.
    """
    return _advice_for_transport(e) or _advice_for_filesystem(e)


def _fail_with_context(message: str, *, offer_traceback: bool = False) -> None:
    """Report a fatal error without throwing away the evidence.

    Replacing a traceback with one line makes the common case readable
    and the exit code usable — but it must not leave someone debugging a
    real failure with nothing to go on. So:

    * the line names what failed, in terms of what the user was doing
    * the full traceback is still printed, at debug level, so `-v` gets
      exactly what the traceback would have shown
    * without `-v`, the last line says how to get it

    Exits 2, the documented "could not do its job" code.
    """
    log(message, "error")
    # Always recorded, never in the way: `-v` shows it, and a report can
    # include it without reproducing the failure first.
    log(traceback.format_exc().rstrip(), "debug")
    if offer_traceback and _OUTPUT_STATE.get("verbosity") != "debug":
        # Only when the message above could not say what to do. Suggesting
        # a traceback after actionable advice just buries the advice.
        log("re-run with -v to see the request that failed", "error")
    sys.exit(2)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    # Apply global output flags BEFORE the subcommand runs so even its
    # early log lines respect --quiet / --verbose / --color. getattr()
    # with defaults keeps main() callable from tests that wire a custom
    # parser without the global flags.
    _configure_output(
        quiet=getattr(args, "quiet", False),
        verbose=getattr(args, "verbose", False),
        color=getattr(args, "color", "auto"),
    )
    # Override config path if requested. Must happen before any cmd_*
    # handler calls load_config(). Tests do the same thing via the
    # isolated_paths fixture; this flag exposes it to the operator.
    config_override = getattr(args, "config", None)
    if config_override:
        # Absolute, for the same reason the destination is: a launchd or
        # cron job starts from the scheduler's cwd, not the author's, so a
        # relative --config in a plist reads AND CREATES a different file
        # than intended — silently, because _atomic_write mkdir-p's the
        # parent. abspath, not resolve(): canonicalising would rewrite a
        # symlinked config path, which is a change of meaning nobody asked
        # for (see _ensure_destination for the same reasoning).
        path = Path(os.path.abspath(os.path.expanduser(config_override)))  # noqa: PTH100
        # Repoint every module-level path that derives from CONFIG_PATH
        # so load_config / set_config_field / cmd_config_show all agree.
        globals()["CONFIG_PATH"] = path
        globals()["CONFIG_DIR"] = path.parent
    try:
        args.func(args)
    except KeyboardInterrupt:
        log("interrupted", "warn")
        sys.exit(130)
    except BrokenPipeError:
        # Someone downstream stopped reading — `da search ... | head -5`,
        # which docs/commands/search.md itself shows. Not a failure of
        # ours, and it must not be reported as one: BrokenPipeError is a
        # ConnectionError is an OSError, so it used to reach the transport
        # advice below and claim "The connection to DeviantArt was
        # interrupted... nothing has been lost" — from `da bench`, which
        # makes no network calls at all.
        #
        # Redirecting stdout to /dev/null before exiting is what keeps the
        # code honest. Without it CPython tries to flush the dead pipe
        # during shutdown, prints "Exception ignored while flushing
        # sys.stdout", and overrides our status with 120 — a code outside
        # the documented 0/1/2/130 contract, so `case $? in 0|1|2)` in a
        # wrapper fell through.
        with contextlib.suppress(OSError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        # 0, not 2: the consumer got what it asked for and left. Exiting
        # non-zero would break every documented `| head` under `set -e`.
        sys.exit(0)
    except DacliError as e:
        # Our own categorised failures: ConfigError, AuthError, HttpError.
        # They carry a finished sentence, so there is nothing to add — and
        # they exist so callers can tell a rejected credential from an
        # unreachable network, which a shared sys.exit(2) could not.
        _fail_with_context(str(e))
    except urllib.error.HTTPError as e:
        # A backstop, not a substitute for handling errors where they
        # happen: the sync walks stop cleanly on their own and record a
        # summary. This catches the calls that do not.
        # Only the fallback branch has nothing specific to suggest.
        specific = e.code in (401, 403, 404, 429) or 500 <= e.code < 600
        _fail_with_context(_advice_for_http(e), offer_traceback=not specific)
    except urllib.error.URLError as e:
        _fail_with_context(
            f"Could not reach DeviantArt: {e.reason}.\n"
            f"Check your network connection. A scheduled run will retry on its "
            f"next fire; nothing has been lost."
        )
    except json.JSONDecodeError:
        # DeviantArt sent something that is not JSON — an outage page, or
        # a captive portal or proxy answering on their behalf. Nothing to
        # do with the code here, so it gets advice rather than a trace.
        _fail_with_context(
            "DeviantArt returned a response that was not valid JSON.\n"
            "That usually means an outage page, or a proxy or captive portal "
            "answering in their place. Retry shortly; nothing has been lost."
        )
    except OSError as e:
        advice = _advice_for_os_error(e)
        if advice is None:
            # Not a failure mode we recognise, so it is most likely a
            # defect in da-cli rather than the machine. Let it out with
            # its traceback: dressing it up as a tidy user-facing error
            # is how a bug gets mistaken for someone's disk being full.
            raise
        _fail_with_context(advice)


if __name__ == "__main__":
    main()
