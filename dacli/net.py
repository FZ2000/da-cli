"""The HTTP layer — every outbound request goes through here.

Named ``net`` rather than ``http`` on purpose. Importing a submodule
binds it as an attribute of its package, so ``dacli/http.py`` would
overwrite the stdlib ``http`` that ``dacli/auth.py`` imports for
``http.server`` — breaking the OAuth loopback listener with
``module 'dacli.http' has no attribute 'server'``.

Extracted verbatim from the single-file module; see ADR 0007.

These three functions are the most-patched names in the test suite:
``http_json`` (39 sites), ``http_bytes`` (21) and ``http_post_json``
(13) are stubbed so tests never reach DeviantArt. That works because
the suite patches them on the package (``dacli.http_json``) and every
caller still resolves them there — either because the caller lives in
``dacli/__init__.py``, whose globals are the package namespace, or
because it looks them up as ``dacli.http_json`` at call time.

A module that imports one of these by value would bypass the stub and
make real network calls while the suite still reported green. That is
the failure this layout exists to prevent.
"""

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import dacli

from .constants import (
    HTTP_RETRY_BACKOFF_BASE_S,
    HTTP_RETRY_DEFAULT,
    HTTP_TIMEOUT_BYTES_S,
    HTTP_TIMEOUT_JSON_S,
    USER_AGENT,
)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _request(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    form: dict[str, str | list[str]] | None = None,
) -> urllib.request.Request:
    """Build a urllib Request with our User-Agent, optional Bearer token,
    and optional form-encoded POST body. Used by http_json / http_post_json
    so retry logic has a single Request-building seam.
    """
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", USER_AGENT)
    if token:
        # add_unredirected_header, NOT add_header: urllib re-sends normal
        # headers when it follows a redirect, including to a *different*
        # host. That would hand our OAuth bearer token to whoever the
        # redirect points at. Verified against this interpreter — a 302
        # from one origin to another delivered the header verbatim:
        #
        #     host B received Authorization: 'Bearer SECRET-TOKEN-123'
        #
        # DA's JSON API answers directly today, so this is a door rather
        # than a hole; the image CDN already 302s to wixmp.com, and it is
        # one API change away from mattering. Unredirected headers are
        # dropped on redirect, which is the behaviour we want: a
        # cross-host hop should fail unauthenticated, not leak.
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    if form is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        # doseq so a list value repeats its key, which is the shape DA's
        # /user/whois wants (`usernames[]=a&usernames[]=b`). Scalars are
        # unaffected, so every existing caller encodes identically.
        req.data = urllib.parse.urlencode(form, doseq=True).encode()
    return req


# HTTP codes that warrant a retry. Anything else (4xx — including 429,
# which the caller handles explicitly) fails fast: 4xx errors are
# permanent for this request shape, retrying just delays the inevitable.
RETRYABLE_HTTP_CODES = frozenset({500, 502, 503, 504})


def _retry_backoff(attempt: int, base: float) -> float:
    """Exponential backoff with ±10% jitter. attempt=0 gives ~base,
    attempt=1 gives ~2*base, attempt=2 gives ~4*base, etc. Jitter
    avoids thundering-herd retries when many workers fail at once.
    """
    return base * (2**attempt) * random.uniform(0.9, 1.1)


def _redact(url: str) -> str:
    """A URL safe to print. Strips any token that reached the query string.

    DA's own API takes its token in the Authorization header, so for
    ``http_json`` this is belt-and-braces. The image CDN is the exception
    and the reason ``token`` is in the pattern: wixmp signs every content
    URL with a bare ``?token=<JWT>``, so the URLs ``http_bytes`` fetches
    carry a live credential in the query string as a matter of course.

    ``access_token`` stays listed ahead of ``token``. Alternation is
    ordered and ``re.sub`` consumes what it matches, so ``access_token=``
    is claimed whole rather than leaving ``access_`` behind.

    `-v` output is exactly what people paste into bug reports, so a debug
    line that leaks a credential would be worse than no debug line at all.
    """
    return re.sub(r"((?:access_token|client_secret|code|token)=)[^&]+", r"\1<redacted>", url)


def http_json(
    url: str,
    *,
    token: str | None = None,
    retries: int = HTTP_RETRY_DEFAULT,
    backoff: float = HTTP_RETRY_BACKOFF_BASE_S,
) -> dict[str, object]:
    """GET + json.loads with retry on 5xx and network errors only.

    Args:
        url: Full DA API URL.
        token: Optional Bearer token for the Authorization header.
        retries: Number of retry attempts after the first failure.
        backoff: Base for the exponential backoff (jittered ±10 %).

    Returns:
        Parsed JSON body as a dict.

    Raises:
        urllib.error.HTTPError: 4xx (incl. 429) immediately, or 5xx after
            `retries` retries are exhausted.
        urllib.error.URLError: network/timeout after `retries` retries.

    4xx (including 429) raises immediately — they're permanent for this
    request, and 429 specifically wants the caller to apply its own
    backoff or stop policy, not the http layer.
    """
    for attempt in range(retries + 1):
        try:
            dacli.log(f"GET {_redact(url)}", "debug")
            with urllib.request.urlopen(
                _request(url, token=token), timeout=HTTP_TIMEOUT_JSON_S
            ) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_HTTP_CODES and attempt < retries:
                wait = _retry_backoff(attempt, backoff)
                dacli.log(f"  HTTP {e.code}; retry {attempt + 1}/{retries} in {wait:.1f}s", "debug")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                wait = _retry_backoff(attempt, backoff)
                dacli.log(
                    f"  {type(e).__name__}; retry {attempt + 1}/{retries} in {wait:.1f}s", "debug"
                )
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("unreachable")


def http_post_json(
    url: str,
    form: dict[str, str | list[str]],
    *,
    token: str | None = None,
    retries: int = HTTP_RETRY_DEFAULT,
    backoff: float = HTTP_RETRY_BACKOFF_BASE_S,
) -> dict[str, object]:
    """POST + json.loads with the same retry contract as ``http_json``.

    Args:
        url: Full URL (typically the OAuth token endpoint).
        form: Form-encoded body fields. A list value repeats its key,
            which is what DA's ``/user/whois`` wants
            (``usernames[]=a&usernames[]=b``).
        token: Optional Bearer token. Needed by ``/user/whois``; the
            token endpoint authenticates with the client_secret in the
            body instead.
        retries: Number of retry attempts after the first failure.
        backoff: Base for the exponential backoff.

    Returns:
        Parsed JSON body.

    Used by the OAuth token endpoint (most network-fragile call we make).
    A transient 502 from DA's auth servers shouldn't kill ``da auth``
    or break the access_token refresh cycle. 4xx (e.g. 400 invalid_grant)
    still fails fast — those are permanent and need user action.
    """
    for attempt in range(retries + 1):
        try:
            # The URL only, never the body: this is the path the OAuth
            # token exchange takes, and `form` holds the client_secret and
            # the refresh token. _redact covers anything that reached the
            # query string.
            dacli.log(f"POST {_redact(url)}", "debug")
            with urllib.request.urlopen(
                _request(url, method="POST", form=form, token=token),
                timeout=HTTP_TIMEOUT_JSON_S,
            ) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_HTTP_CODES and attempt < retries:
                wait = _retry_backoff(attempt, backoff)
                dacli.log(f"  HTTP {e.code}; retry {attempt + 1}/{retries} in {wait:.1f}s", "debug")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                wait = _retry_backoff(attempt, backoff)
                dacli.log(
                    f"  {type(e).__name__}; retry {attempt + 1}/{retries} in {wait:.1f}s", "debug"
                )
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("unreachable")


def http_bytes(
    url: str, *, retries: int = HTTP_RETRY_DEFAULT, backoff: float = HTTP_RETRY_BACKOFF_BASE_S
) -> bytes:
    """GET raw bytes (image CDN). Same retry contract as ``http_json``.

    Args:
        url: Full CDN URL.
        retries: Number of retry attempts after the first failure.
        backoff: Base for the exponential backoff.

    Returns:
        Raw response body bytes. The CDN gets a browser User-Agent
        (Mozilla/5.0) because wixmp rejects requests that look like
        scripts — documented in docs/explanation/security.md.
    """
    for attempt in range(retries + 1):
        try:
            dacli.log(f"GET {_redact(url)}", "debug")
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")  # CDN prefers a browser UA
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_BYTES_S) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_HTTP_CODES and attempt < retries:
                wait = _retry_backoff(attempt, backoff)
                dacli.log(f"  HTTP {e.code}; retry {attempt + 1}/{retries} in {wait:.1f}s", "debug")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                wait = _retry_backoff(attempt, backoff)
                dacli.log(
                    f"  {type(e).__name__}; retry {attempt + 1}/{retries} in {wait:.1f}s", "debug"
                )
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("unreachable")
