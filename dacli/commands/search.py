"""Search / browse commands.

Names the test suite patches are read through the ``dacli`` package at
call time; see ADR 0007.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

import dacli

from ..constants import API_BASE, mature_content_param


# --------------------------------------------------------------------------
# Search / browse
# --------------------------------------------------------------------------
def _print_results(body: dict[str, object], as_json: bool = False) -> None:
    """Render search results either as human-readable lines or raw JSON.

    Human-readable mode discards deviationid, content URL, and description.
    ``--json`` exposes the full body so downstream callers (scripts piped
    off stdout) can fetch individual deviations without re-syncing whole
    galleries.
    """
    if as_json:
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return
    for r in body.get("results", []):
        title = r.get("title") or "(untitled)"
        author = (r.get("author") or {}).get("username")
        url = r.get("url")
        mature = " [mature]" if r.get("is_mature") else ""
        print(f"  {author or '?':<24} {title:<50} {url}{mature}")
    if body.get("has_more"):
        print(f"\nhas_more: True (next_offset={body.get('next_offset')})")


def cmd_search_popular(args: argparse.Namespace) -> None:
    """`/browse/popular` was retired by DeviantArt (every variant returns
    HTTP 404 with body `"Api endpoint not found."` against valid auth — same
    token works on /browse/dailydeviations, /browse/topic, etc.). The
    closest live replacements are `da search topic <name>` (curated
    editorial feed) and `da daily` (today's daily-deviation picks).
    """
    del args
    dacli.log("`search popular` is unavailable — DA retired /browse/popular.", "error")
    dacli.log(
        "use `da search topic <name>` (curated topics like digitalart / "
        "nature / animals), `da search tag <tag>`, or `da daily` instead.",
        "error",
    )
    sys.exit(2)


def cmd_search_newest(args: argparse.Namespace) -> None:
    """`/browse/newest` was retired by DeviantArt — same shape as the
    popular deprecation above. Use `da search tag <tag>` for tag-anchored
    chronological browsing, or `da search topic <topic>`.
    """
    del args
    dacli.log("`search newest` is unavailable — DA retired /browse/newest.", "error")
    dacli.log(
        "use `da search tag <tag>` for tag-anchored browsing, or `da search topic <name>`.",
        "error",
    )
    sys.exit(2)


def cmd_search_tag(args: argparse.Namespace) -> None:
    cfg = dacli.load_config()
    state = dacli.load_state()
    token = dacli.access_token(cfg, state)
    body = dacli.http_json(
        f"{API_BASE}/browse/tags?tag={urllib.parse.quote(args.tag)}"
        f"&limit={args.limit}&mature_content={mature_content_param(args.mature)}",
        token=token,
    )
    _print_results(body, as_json=getattr(args, "json", False))


def cmd_search_user(args: argparse.Namespace) -> None:
    """Resolve usernames to user objects via /user/whois.

    /user/whois requires POST with `usernames[]` form fields. Sending GET
    returns 400 Bad Request.
    """
    cfg = dacli.load_config()
    state = dacli.load_state()
    token = dacli.access_token(cfg, state)
    # `usernames[]` repeats once per username, which is exactly what
    # urlencode's doseq gives a list value. This was a hand-built urlopen
    # call, on the belief that urlencode could not produce the shape —
    # and going around http_post_json meant no retry on a 5xx, no `-v`
    # request logging, and invisibility to every test that stubs the HTTP
    # layer, so the suite could not have caught a regression here.
    body = dacli.http_post_json(
        f"{API_BASE}/user/whois?mature_content=true",
        {"usernames[]": list(args.query)},
        token=token,
    )
    if getattr(args, "json", False):
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return
    for entry in body.get("results", []):
        print(f"  @{entry.get('username')}  ({entry.get('userid')})  type={entry.get('type')}")


def cmd_search_topic(args: argparse.Namespace) -> None:
    """Fetch deviations for a curated topic (e.g. digitalart / nature /
    fantasy / animals / traditional). Topics are DA-curated and tend
    to produce cleaner results than a generic tag search because topic
    membership is editorial, not user-tagged.
    """
    cfg = dacli.load_config()
    state = dacli.load_state()
    token = dacli.access_token(cfg, state)
    mature = mature_content_param(args.mature)
    body = dacli.http_json(
        f"{API_BASE}/browse/topic?topic={urllib.parse.quote(args.topic)}"
        f"&limit={args.limit}&mature_content={mature}",
        token=token,
    )
    _print_results(body, as_json=getattr(args, "json", False))


def cmd_search_topics(args: argparse.Namespace) -> None:
    """List all DA topics (paginated). Useful for discovering valid topic
    names to pass to `da search topic <name>`.
    """
    cfg = dacli.load_config()
    state = dacli.load_state()
    token = dacli.access_token(cfg, state)
    mature = mature_content_param(args.mature)
    body = dacli.http_json(
        f"{API_BASE}/browse/topics?limit={args.limit}&offset={args.offset}&mature_content={mature}",
        token=token,
    )
    if getattr(args, "json", False):
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return
    for t in body.get("results", []):
        n_examples = len(t.get("example_deviations") or [])
        print(
            f"  {t.get('canonical_name', '?'):30s}  "
            f"{t.get('name', '?'):30s}  ({n_examples} examples)"
        )
    if body.get("has_more"):
        print(f"  -- has_more, next_offset={body.get('next_offset')}")


def cmd_search_toptopics(args: argparse.Namespace) -> None:
    """Fetch the top topics with one example deviation per topic. Smaller
    set than `topics` — quick way to discover the most-active categories.
    """
    cfg = dacli.load_config()
    state = dacli.load_state()
    token = dacli.access_token(cfg, state)
    mature = mature_content_param(args.mature)
    body = dacli.http_json(
        f"{API_BASE}/browse/toptopics?mature_content={mature}",
        token=token,
    )
    if getattr(args, "json", False):
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return
    for t in body.get("results", []):
        ex = t.get("example_deviations") or []
        ex_title = (ex[0].get("title") if ex else "") or ""
        print(
            f"  {t.get('canonical_name', '?'):25s}  "
            f"{t.get('name', '?'):25s}  example: {ex_title[:50]}"
        )


def cmd_search_tagsuggest(args: argparse.Namespace) -> None:
    """Autocomplete a tag prefix.

    Useful before ``search tag`` to confirm a candidate tag exists on DA
    (e.g. verify ``nature`` is a real tag rather than guessing).
    Returns: list of ``{tag_name}`` hits.
    """
    cfg = dacli.load_config()
    state = dacli.load_state()
    token = dacli.access_token(cfg, state)
    body = dacli.http_json(
        f"{API_BASE}/browse/tags/search?tag_name={urllib.parse.quote(args.prefix)}",
        token=token,
    )
    if getattr(args, "json", False):
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return
    for r in body.get("results", []):
        print(f"  {r.get('tag_name', '?')}")


def cmd_deviation_morelikethis(args: argparse.Namespace) -> None:
    """Given a seed deviation ID, fetch similar deviations via DA's
    'More Like This' recommender. Useful for expanding a known-good
    reference into a larger set of stylistically related works.
    """
    cfg = dacli.load_config()
    state = dacli.load_state()
    token = dacli.access_token(cfg, state)
    mature = mature_content_param(args.mature)
    body = dacli.http_json(
        f"{API_BASE}/browse/morelikethis/preview?seed={urllib.parse.quote(args.deviationid)}"
        f"&mature_content={mature}",
        token=token,
    )
    if getattr(args, "json", False):
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return
    # /browse/morelikethis/preview returns a different shape:
    # {seed, author, more_from_artist[], more_from_da[]}
    author = (body.get("author") or {}).get("username", "?")
    print(f"  seed: {body.get('seed', '?')}  author: @{author}")
    for label, key in [("FROM ARTIST", "more_from_artist"), ("FROM DA", "more_from_da")]:
        items = body.get(key) or []
        print(f"  --- {label} ({len(items)}) ---")
        for r in items[: args.limit]:
            title = (r.get("title") or "")[:60]
            author = (r.get("author") or {}).get("username") or "?"
            print(f"    {r.get('deviationid', '?')[:36]}  @{author:18s}  {title}")
