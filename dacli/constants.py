"""Paths and tunable constants.

Extracted verbatim from the single-file module; see ADR 0007.

The five path constants (``CONFIG_DIR``, ``CONFIG_PATH``, ``STATE_DIR``,
``STATE_PATH``, ``INDEX_PATH``) are reassigned by the test suite as
``dacli.CONFIG_PATH = ...``. That works while the consuming code lives in
``dacli/__init__.py``, because its module globals *are* the ``dacli``
namespace. Code that later moves out into another submodule must read
them as ``dacli.CONFIG_PATH`` at call time rather than importing the
value, or the patch will not reach it.
"""

__version__ = "0.3.0"

import os
import random
import time
from pathlib import Path

# --------------------------------------------------------------------------
# Paths and constants
# --------------------------------------------------------------------------
HOME = Path.home()
# `or`, not a get() default: the XDG spec says an empty value means
# unset, and os.environ.get returns "" as a perfectly good value. An
# exported-but-empty XDG_STATE_HOME therefore produced the RELATIVE path
# "da-cli/" — so state, the index and the sync lock fragmented per working
# directory, and two syncs started from different directories ran
# concurrently against one destination, which is precisely what the lock
# exists to prevent.
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "da-cli"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state") / "da-cli"
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = STATE_DIR / "state.json"
INDEX_PATH = STATE_DIR / "index.db"

API_BASE = "https://www.deviantart.com/api/v1/oauth2"
TOKEN_URL = "https://www.deviantart.com/oauth2/token"
AUTH_URL = "https://www.deviantart.com/oauth2/authorize"

DEFAULT_DELAY_API = 5.0
DEFAULT_DELAY_IMAGE = 1.5
DEFAULT_JITTER = 0.0  # off by default; 0.3 means ±30% of base when enabled
DEFAULT_LIMIT = 24
DEFAULT_CONCURRENCY = 4  # parallel image downloads per page; bounded for CDN politeness
KEYCHAIN_SERVICE = "da-cli"
USER_AGENT = f"da-cli/{__version__} (+https://github.com/FZ2000/da-cli)"

# Named timeouts / thresholds — gathered here so they're greppable and
# tweakable in one place rather than scattered as inline magic numbers.
HTTP_TIMEOUT_JSON_S = 30  # http_json / http_post_json
HTTP_TIMEOUT_BYTES_S = 60  # http_bytes (image CDN; larger because images are bigger)
HTTP_RETRY_BACKOFF_BASE_S = 1.5
HTTP_RETRY_DEFAULT = 2  # try N+1 times total before propagating
AUTH_LISTENER_TIMEOUT_S = 300  # 5-minute browser-window wait
# Per-connection cap on the loopback listener: bounds the TLS handshake
# and the request read so a client that connects and stays silent
# cannot hold its thread for the whole flow.
AUTH_CONNECTION_TIMEOUT_S = 20
AUTH_DEFAULT_PORT = 8765  # loopback OAuth callback port
TOKEN_REFRESH_SKEW_S = 60  # refresh this many seconds before real expiry
# How long a process waits for another one's refresh before giving up and
# proceeding unlocked. A refresh is one HTTP round trip, so this is
# generous; the timeout exists so a stuck holder degrades to the old racy
# behaviour rather than hanging the CLI.
TOKEN_LOCK_TIMEOUT_S = 30.0
TOKEN_LOCK_POLL_S = 0.05
METADATA_BATCH_SIZE = 50  # /deviation/metadata caps at 50 deviationids per call
GALLERY_PAGE_CAP = 24  # /gallery/all caps at 24 per page
FEED_PAGE_CAP = 50  # /browse/deviantsyouwatch caps at 50 per page
FRIENDS_PAGE_CAP = 50  # /user/friends caps at 50 per page
# Backstop on the /user/friends walk. Unlike the sync walks it has no time
# budget to fall back on, so a server that keeps saying has_more would
# otherwise page forever. 200 pages is 10k watched artists — far past any
# real account, so tripping this means the API is misbehaving, not that
# someone watches a lot of people.
FRIENDS_PAGE_MAX = 200
JITTER_FLOOR_S = 0.05  # jitter Sleep can never go below this
JITTER_MAX_PCT = 0.95  # pct clamped to this so jitter never zeroes the base
CONCURRENCY_MAX = 16  # cap on --concurrency; higher is CDN-unfriendly + GIL-bound
CONCURRENCY_MIN = 1  # floor (sequential mode)
DEST_FREE_SPACE_WARN_GIB = 5.0  # diagnose warns below this
DEST_FREE_SPACE_FAIL_GIB = 1.0  # diagnose fails below this

# A sync walk stops this many seconds before its budget so the page in
# flight can finish and the summary can be written.
TIME_BUDGET_MARGIN_S = 10
# `sync watched` will not start another artist with less than this left:
# below TIME_BUDGET_MARGIN_S the artist's own loop cannot run even once,
# so starting one would burn an access-token check and accomplish
# nothing. The extra seconds leave room for a single page fetch.
MIN_ARTIST_BUDGET_S = TIME_BUDGET_MARGIN_S + 20

# Recorded as `stop_reason` when the clock, not the data, ended a walk.
# Distinguishing this from a finished walk is what lets `da diagnose`
# tell a truncated scheduled run from a healthy one.
TIME_BUDGET_EXHAUSTED = "time budget exhausted"
# Recorded by `sync watched` when every discovered artist was walked.
WATCHED_ALL_COMPLETE = "all artists complete"
# stop_reasons that mean the walk genuinely finished its work.
TERMINAL_STOP_REASONS = frozenset({"gallery complete", "caught up", "feed exhausted", "feed empty"})
LOOPBACK_CERT_VALIDITY_DAYS = 825  # macOS notary recommends ≤825 days
LOOPBACK_CERT_KEY_BITS = 3072  # NIST SP 800-57 floor for new RSA issuance in 2026
SHORT_FAST_PATH_DELAY_S = 1.0  # all-known page uses a shorter api delay (one call, no metadata)
LOG_BODY_TRUNCATE = 200  # truncate HTTP error bodies in log lines to this many chars
# DA's refresh_token has a hard 90-day TTL (per DA's auth docs). The CLI
# can't extend it; we surface the remaining lifetime in `da diagnose` so
# the user re-runs `da auth` before it dies (a dead refresh_token at 03:00
# is the canonical silent-cron-failure mode).
REFRESH_TOKEN_TTL_DAYS = 90
REFRESH_TOKEN_WARN_DAYS = 14  # diagnose reports WARN at or below this
REFRESH_TOKEN_CRIT_DAYS = 3  # diagnose reports FAIL at or below this

# Shared argparse help string for the `--json` flag on search / browse commands.
JSON_HELP = "Emit raw JSON (deviationid + content URL + description) instead of summary lines"


def mature_content_param(mature: bool) -> str:
    """Render the `mature_content=` query value DA expects. Centralised
    because every browse/search endpoint repeats it; if DA ever flips the
    encoding (e.g. to `0`/`1`) there's one place to change.
    """
    return "true" if mature else "false"


def jittered(base: float, pct: float) -> float:
    """Return ``base`` multiplied by a uniform jitter factor.

    Args:
        base: Unjittered delay in seconds.
        pct: Jitter fraction. ``pct=0`` returns ``base`` unchanged;
            ``pct=0.4`` randomises by ±40 %. Clamped to JITTER_MAX_PCT
            so the result never goes negative.

    Returns:
        Jittered delay, never negative, and floored at JITTER_FLOOR_S
        when jitter is applied so callers can never end up with a
        sub-millisecond sleep.
    """
    # A negative base is nonsense as an interval and time.sleep() raises
    # on it. The floor lives here, at the last point before the sleep,
    # rather than in each of the callers that resolve a delay — the
    # docstring above promised "never goes negative" while the pct <= 0
    # short-circuit below handed `base` straight back.
    base = max(0.0, base)
    if pct <= 0:
        return base
    pct = min(pct, JITTER_MAX_PCT)  # never let jitter zero out the delay entirely
    return max(JITTER_FLOOR_S, base * random.uniform(1.0 - pct, 1.0 + pct))


def jitter_sleep(base: float, pct: float) -> None:
    """time.sleep for `jittered(base, pct)` seconds. No-op-ish wrapper
    so callers don't repeat the jittered/sleep pair.
    """
    time.sleep(jittered(base, pct))
