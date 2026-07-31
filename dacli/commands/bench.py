"""Bench.

Names the test suite patches — and, for bench, the ones it swaps
wholesale — are read through the ``dacli`` package at call time.
See ADR 0007.
"""

import argparse
import json
import re
import shutil
import time
import urllib.request
from pathlib import Path

import dacli

from .. import index as _index_module
from ..constants import CONCURRENCY_MAX, CONCURRENCY_MIN
from ..index import _index_close


# --------------------------------------------------------------------------
# Bench: synthetic sync against a fully-mocked HTTP layer
#
# Measures CLI overhead (parsing, index ops, file IO, thread-pool dispatch)
# without touching the network. Useful for:
#   - Detecting perf regressions in CI
#   - Sizing config defaults (concurrency, page size)
#   - Sanity-checking that a code change didn't make things slower
#
# Output the JSON shape of `da bench --json` is stable; record it from
# CI and diff between commits to gate perf regressions.
# --------------------------------------------------------------------------
def cmd_bench(args: argparse.Namespace) -> None:
    """Synthetic feed-sync against a fake CDN. Reports throughput stats.

    Doesn't touch the real DA API. Mocks `urllib.request.urlopen` for
    the duration of the run, points DESTINATION at a tmp dir, mocks
    time.sleep to no-op so we measure code overhead (not jitter delays).
    """
    import tempfile

    pages = max(1, int(args.pages))
    per_page = max(1, int(args.per_page))
    # The same bounds the real sync applies (sync.py::_concurrency). Written
    # as literals here, bench silently kept clamping at 16 after the
    # constant moved, so its numbers stopped describing the sync it exists
    # to measure.
    concurrency = max(CONCURRENCY_MIN, min(int(args.concurrency), CONCURRENCY_MAX))
    n_total = pages * per_page

    # Removed in the finally below. Without that, every `da bench` left
    # a tree behind — 3,076 of them, 125 MB, on the machine this was
    # found on.
    bench_dir = Path(tempfile.mkdtemp(prefix="da-bench-"))
    bench_dest = bench_dir / "dest"
    bench_dest.mkdir()
    bench_index = bench_dir / "index.db"

    # Build canned responses: one feed page per offset, all-fresh devids
    page_responses: dict[int, dict] = {}
    for p in range(pages):
        results = [
            {
                "deviationid": f"BENCH-{p:04d}-{i:03d}",
                "url": "u",
                "title": f"T-{p}-{i}",
                "is_mature": False,
                "is_favourited": False,
                "published_time": "1",
                "stats": {"comments": 0, "favourites": 0},
                "author": {"username": "alice", "userid": "U1"},
                "content": {"src": f"https://cdn.bench/img-{p}-{i}.png"},
            }
            for i in range(per_page)
        ]
        page_responses[p] = {"results": results, "has_more": (p < pages - 1)}

    md_response: dict[str, list[object]] = {"metadata": []}  # _save_one tolerates empty

    class _BenchResponse:
        """Minimal HTTP response stub for the bench fake_urlopen."""

        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self) -> "_BenchResponse":
            return self

        def __exit__(self, *_a: object) -> None:
            pass

        def read(self) -> bytes:
            return self._body

    def fake_urlopen(req: object, timeout: float | None = None) -> _BenchResponse:
        del timeout
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "cdn.bench" in url:
            return _BenchResponse(b"PNGBYTES")
        if "deviation/metadata" in url:
            return _BenchResponse(json.dumps(md_response).encode())
        offset = 0
        m = re.search(r"offset=(\d+)", url)
        if m:
            offset = int(m.group(1))
        page_idx = offset // per_page
        body = page_responses.get(page_idx, {"results": [], "has_more": False})
        return _BenchResponse(json.dumps(body).encode())

    quiet = bool(getattr(args, "json", False))
    cfg = {"destination": str(bench_dest)}
    state = {
        "access_token": "BENCH",
        "expires_at": time.time() + 3600,
        "refresh_token": "BENCH-RT",
        "scope": "browse",
    }

    # Swap these ON THE PACKAGE, not on this module: every caller resolves
    # them as dacli.X, so assigning a module-level name here would change
    # nothing. Save *every* attribute we're about to mutate. Any exception inside
    # cmd_sync_feed must roll all of them back — leaving urlopen/sleep/
    # log/INDEX_PATH/etc. patched would corrupt subsequent commands in
    # the same process (e.g. tests that import dacli and call multiple
    # bench runs back-to-back).
    saved = {
        "INDEX_PATH": dacli.INDEX_PATH,
        "_INDEX_CONN": _index_module._INDEX_CONN,
        "load_config": dacli.load_config,
        "load_state": dacli.load_state,
        "save_state": dacli.save_state,
        "log": dacli.log,
        "urlopen": urllib.request.urlopen,
        "sleep": time.sleep,
    }

    # Apply mocks. save_state is wrapped in an explicit lambda (not
    # state.update bound directly) so a future caller passing a fresh
    # dict gets visible "merge into the closed-over state" semantics,
    # not a silent partial write.
    dacli.INDEX_PATH = bench_index
    _index_module._INDEX_CONN = None
    dacli.load_config = lambda: cfg
    dacli.load_state = lambda: state
    dacli.save_state = lambda s: state.update(s)  # noqa: PLW0108
    if quiet:
        dacli.log = lambda msg, level="info": None
    else:
        dacli.log(
            f"bench: pages={pages} per_page={per_page} "
            f"concurrency={concurrency} total_items={n_total}"
        )
        dacli.log(f"bench dir: {bench_dir}")
    time.sleep = lambda *_a, **_k: None
    urllib.request.urlopen = fake_urlopen

    started = time.time()
    ns = argparse.Namespace(
        limit=per_page,
        mature=True,
        time_budget=3600,
        delay_api=None,
        delay_image=None,
        jitter=0.0,
        concurrency=concurrency,
    )

    try:
        dacli.cmd_sync_feed(ns)
        elapsed = time.time() - started
        indexed = dacli.index_count()
    finally:
        # Restore in reverse order. _index_close FIRST so the bench's
        # SQLite connection is closed before INDEX_PATH points elsewhere.
        _index_close()
        urllib.request.urlopen = saved["urlopen"]
        time.sleep = saved["sleep"]
        dacli.log = saved["log"]
        dacli.save_state = saved["save_state"]
        dacli.load_state = saved["load_state"]
        dacli.load_config = saved["load_config"]
        _index_module._INDEX_CONN = saved["_INDEX_CONN"]
        dacli.INDEX_PATH = saved["INDEX_PATH"]
        # The synthetic gallery and its index. Nothing outside this
        # function refers to them, and leaving them behind meant a temp
        # tree per invocation for the life of the machine.
        shutil.rmtree(bench_dir, ignore_errors=True)

    # Avoid float("inf"): json.dumps would emit "Infinity", which is not
    # valid JSON per RFC 8259. elapsed is monotonic-positive in practice,
    # but if a future platform ever returned a zero delta the JSON output
    # would silently break — fall back to a large finite number.
    safe_elapsed = elapsed if elapsed > 0 else 1e-9
    items_per_sec = indexed / safe_elapsed
    pages_per_sec = pages / safe_elapsed

    result = {
        "pages": pages,
        "per_page": per_page,
        "concurrency": concurrency,
        "total_items": n_total,
        "indexed": indexed,
        "elapsed_s": round(elapsed, 3),
        "items_per_sec": round(items_per_sec, 1),
        "pages_per_sec": round(pages_per_sec, 2),
    }

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    print()
    print(f"  pages          : {pages}")
    print(f"  per page       : {per_page}")
    print(f"  concurrency    : {concurrency}")
    print(f"  total items    : {n_total}")
    print(f"  indexed        : {indexed}")
    print(f"  elapsed        : {elapsed:.3f}s")
    print(f"  items/sec      : {items_per_sec:,.1f}")
    print(f"  pages/sec      : {pages_per_sec:,.2f}")
