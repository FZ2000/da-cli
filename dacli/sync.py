"""The sync engine: walk DeviantArt, save deviations, keep the checkpoint.

Extracted verbatim from the single-file module; see ADR 0007.

Every name below is read through the ``dacli`` package at call time,
because each is either stubbed by the test suite or swapped wholesale by
``cmd_bench``:

* ``http_json`` / ``http_bytes`` / ``authed_http_json`` — the network.
  A value-import here would make the suite download from DeviantArt for
  real while still reporting green.
* ``load_state`` / ``save_state`` / ``load_config`` — bench swaps these
  for in-memory stand-ins.
* ``log`` — bench's ``--json`` mode replaces it with a no-op; a direct
  import would leak prose into machine-readable output.
* ``_cmd_sync_artist_impl`` — patched by the watched-sync tests.
"""

import argparse
import concurrent.futures
import contextlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
from pathlib import Path

import dacli

from .config import coerce_number
from .constants import (
    API_BASE,
    CONCURRENCY_MAX,
    CONCURRENCY_MIN,
    DEFAULT_CONCURRENCY,
    DEFAULT_DELAY_API,
    DEFAULT_DELAY_IMAGE,
    DEFAULT_JITTER,
    FEED_PAGE_CAP,
    FRIENDS_PAGE_CAP,
    FRIENDS_PAGE_MAX,
    GALLERY_PAGE_CAP,
    JITTER_MAX_PCT,
    METADATA_BATCH_SIZE,
    MIN_ARTIST_BUDGET_S,
    SHORT_FAST_PATH_DELAY_S,
    TIME_BUDGET_EXHAUSTED,
    TIME_BUDGET_MARGIN_S,
    WATCHED_ALL_COMPLETE,
    jitter_sleep,
    mature_content_param,
)
from .index import (
    index_add,
    index_bootstrap_if_empty,
    index_filter_known,
    index_has,
    read_folder_description,
    read_synced_folder,
)
from .lock import CommandLockedError, _cmd_lock, _record_sync_summary
from .output import _atomic_write, safe_filename


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------
def _ensure_destination(cfg: dict[str, str], *, create: bool = True) -> Path:
    """Resolve the configured destination.

    ``create=False`` resolves without creating anything. `da index
    rebuild` needs that: creating the directory first turns "the drive is
    not mounted" into "the destination is empty", which is precisely the
    case ``index_rebuild_from_disk`` refuses to act on.
    """
    dest = cfg.get("destination")
    if not dest:
        dacli.log(
            "no destination configured — set DA_DESTINATION or `da config set destination <PATH>`",
            "error",
        )
        sys.exit(2)
    # Absolute, always. index_add stores str(folder), so a relative
    # destination writes cwd-relative rows — every one of which stops
    # resolving the moment the next run starts from a different
    # directory, and a launchd job starts from "/".
    #
    # abspath, NOT resolve(): resolve() follows symlinks, which rewrites
    # the destination for anyone whose gallery is behind one — and on
    # macOS /tmp is itself a symlink. Every index row written before that
    # rewrite records the old path, so the lookup misses and the entire
    # gallery downloads again. Making the path absolute is the whole fix;
    # canonicalising it is an unrelated change with a much bigger blast
    # radius. The suppression below is for PTH100, which suggests exactly
    # the resolve() call this comment exists to rule out.
    p = Path(os.path.abspath(os.path.expanduser(dest)))  # noqa: PTH100
    if not create:
        return p
    if not p.parent.exists():
        dacli.log(f"parent of destination does not exist: {p.parent}", "error")
        sys.exit(2)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _resolve_folder(
    d: dict[str, object],
    dest_root: Path,
    fallback_artist: str | None,
) -> tuple[str, str, Path]:
    """
    Pick the destination folder, disambiguating title collisions by appending
    a deviationid prefix when an existing folder is for a different deviation.

    Backward compatible: existing single-deviation folders without the suffix
    keep their path; the suffix is only added on collision.
    """
    artist = safe_filename(d.get("author", {}).get("username", fallback_artist or "unknown"))
    title = safe_filename(d.get("title", "untitled"))
    devid = d["deviationid"]
    base = dest_root / artist / title
    desc = base / "description.json"

    if base.exists() and desc.exists():
        # Unreadable / malformed / wrong-shaped description.json reads as
        # "no existing record", and we fall through to the un-suffixed
        # folder — whoever owns this artist+title rewrites it, repairing
        # it. read_folder_description absorbs every way the file can fail,
        # including the ``[]`` that used to reach ``.get()`` and raise
        # AttributeError in the middle of a save.
        existing = (read_folder_description(base) or {}).get("deviationid")
        if existing and existing != devid:
            # Collision — same artist + title but different deviationid.
            # DA returns UUID-shaped deviationids, so the first 8 hex chars
            # are usually enough for a human-readable suffix.
            #
            # Usually is not always: two deviations sharing artist, title
            # AND that prefix landed on the same suffixed folder, and the
            # second silently overwrote the first's description.json and
            # image while the index pointed both ids at one folder. Widen
            # the suffix until it actually distinguishes them.
            return artist, title, _unique_suffixed_folder(dest_root, artist, title, str(devid))
    return artist, title, base


def _suffix_candidates(devid: str) -> list[str]:
    """Progressively longer suffixes, shortest first.

    Starting at 8 keeps existing folders exactly where they are: an
    install that already has ``title--abcd1234`` for this deviation still
    resolves to it on the first try.
    """
    stem = devid.split("-", maxsplit=1)[0] if "-" in devid else devid
    lengths = sorted({8, 12, 16, len(stem)})
    out = [stem[:n].lower() for n in lengths if n <= len(stem)]
    # Last resort: the whole id, dashes and all, which is unique by
    # definition.
    flat = devid.replace("-", "").lower()
    if flat not in out:
        out.append(flat)
    return out


def _unique_suffixed_folder(dest_root: Path, artist: str, title: str, devid: str) -> Path:
    """First suffixed folder that is free, or already ours."""
    for short in _suffix_candidates(devid):
        candidate = dest_root / artist / f"{title}--{short}"
        desc = candidate / "description.json"
        if not desc.exists():
            return candidate
        # Same absorb-every-failure read as _resolve_folder: a candidate
        # we cannot read is one we are free to claim.
        owner = read_folder_description(candidate)
        if owner is None or owner.get("deviationid") == devid:
            return candidate
    # Every candidate is taken by a different deviation, which needs the
    # full id to have collided. Fall back to the id itself so two
    # deviations can never share a folder.
    return dest_root / artist / f"{title}--{devid.lower()}"


# Serializes folder reservation in _save_one (see the comment there). Guards
# the resolve→description-write window only, never the image download.
_FOLDER_RESERVE_LOCK = threading.Lock()


def _save_one(  # noqa: PLR0911 — every status has a distinct early return; collapsing them hurts readability
    d: dict[str, object],
    md_by_id: dict[str, dict[str, object]],
    dest_root: Path,
    *,
    fallback_artist: str | None = None,
    image_delay: float = DEFAULT_DELAY_IMAGE,
    jitter_pct: float = 0.0,
    dry_run: bool = False,
) -> tuple[str, str, str, int]:
    """Persist one deviation to disk: metadata JSON + image bytes.

    The single source of truth for the dedup decision is the synced
    index (``index_has(devid)`` → ``"dup"``). On an index miss, disk
    is checked as a fallback to handle the legacy case where content
    exists but the index hasn't been bootstrapped yet — in that case
    the index is backfilled for future runs.

    Args:
        d: The deviation dict from the feed/gallery endpoint.
        md_by_id: Metadata map from ``_fetch_metadata_batch``.
        dest_root: Root destination directory.
        fallback_artist: Used when ``d`` has no ``author.username``
            (rare; the gallery endpoint always populates this).
        image_delay: Sleep after each image download (rate-limit).
        jitter_pct: Multiply ``image_delay`` by ``uniform(1-pct, 1+pct)``.
        dry_run: If True, return what *would* happen without writing
            any files. Status is ``"dry"`` (would-download) or
            ``"dup"`` (already known).

    Returns:
        ``(status, artist, title, size)`` where status is one of:

        * ``"ok"``     — newly saved; size is the image byte count
        * ``"dup"``    — already known (index hit or disk backfill); size 0
        * ``"dry"``    — dry-run hit on unknown deviation; size is the
          estimated image size if available in the feed dict, else 0
        * ``"noimg"``  — deviation has no downloadable content URL; size 0
        * ``"fail:ExceptionClass"`` — image or metadata write raised;
          size 0. The exception class is folded into the status string
          so the caller's totals tally includes the failure mode.

    Image writes are atomic: stage to ``image.<ext>.part``, ``fsync``,
    rename. A crash mid-download leaves a ``.part`` file that the
    dedup check ignores, so the next run redownloads cleanly.
    """
    devid = str(d["deviationid"])
    # Folder reservation must be serialized: _resolve_folder only detects an
    # artist+title collision once the earlier deviation's description.json is
    # on disk. Two colliding deviations resolved concurrently would both get
    # the un-suffixed folder and then race on the same description.json.tmp
    # (the loser's tmp vanishes under it → fail:FileNotFoundError). The lock
    # covers resolve → description write; the slow image download below runs
    # outside it, so page concurrency is preserved.
    with _FOLDER_RESERVE_LOCK:
        artist, title, folder = _resolve_folder(d, dest_root, fallback_artist)

        # Fast path: O(1) index lookup. The index is the source of truth for
        # "already synced". Disk is checked only as a fallback to handle the
        # legacy case where content exists but the index hasn't been bootstrapped.
        if index_has(devid):
            return "dup", artist, title, 0

        # Dry-run short-circuit: report what *would* happen without writing.
        # Size is the feed-provided image_size when available, else 0 — so the
        # caller can sum "would-download N MB" for capacity planning.
        if dry_run:
            content = d.get("content") or {}
            size_hint = int(content.get("filesize") or 0) if isinstance(content, dict) else 0
            return "dry", artist, title, size_hint

        # Disk has it but the index does not — backfill the index for
        # future runs, but only once the folder really holds THIS
        # deviation, complete.
        #
        # read_synced_folder is the definition every caller now shares.
        # This path used to carry its own, which required an image with
        # bytes in it and never opened description.json. A folder whose
        # metadata was empty or truncated was therefore indexed as synced,
        # so every later run answered "dup", the metadata was never
        # re-fetched, and the damage was permanent — and invisible.
        # Matching on deviationid additionally stops an index row that
        # points at another deviation's folder from counting as a hit.
        found = read_synced_folder(folder)
        if found is not None and found.deviationid == str(devid):
            index_add(devid, artist, title, folder, found.image_size)
            return "dup", artist, title, 0

        folder.mkdir(parents=True, exist_ok=True)
        description = {
            "deviationid": d["deviationid"],
            "url": d.get("url"),
            "title": d.get("title"),
            "author_username": d.get("author", {}).get("username"),
            "author_userid": d.get("author", {}).get("userid"),
            "is_mature": d.get("is_mature"),
            "is_favourited": d.get("is_favourited"),
            "published_time": d.get("published_time"),
            "stats": d.get("stats", {}),
            "metadata": md_by_id.get(str(d["deviationid"]), {}),
        }
        try:
            _atomic_write(
                folder / "description.json",
                json.dumps(description, indent=2, ensure_ascii=False),
                0o644,
            )
        except OSError as e:
            # Disk-full / permission-denied / etc. mid-description-write. Don't
            # propagate — _save_page_concurrent's defensive handler would still
            # catch this, but returning here keeps the fail tuple's artist/title
            # populated (the defensive handler can't reconstruct them).
            return f"fail:{type(e).__name__}", artist, title, 0

    content = d.get("content") or d.get("preview") or {}
    src = content.get("src")
    if not src:
        return "noimg", artist, title, 0
    ext = ".jpg"
    m = re.search(r"\.(jpg|jpeg|png|gif|webp)", src.lower())
    if m:
        ext = "." + m.group(1)
    try:
        blob = dacli.http_bytes(src)
        # A 200 with an empty body (CDN hiccup, expired signed URL served
        # as a blank page) must not be committed: the 0-byte file would
        # be indexed as synced and never retried, because index_has()
        # doesn't look at size. Fail instead so the next run re-fetches.
        if not blob:
            return "fail:EmptyBody", artist, title, 0
        # Atomic write: stage to .part, fsync, rename. Crash-safe — a half-
        # written .part on disk after a SIGKILL won't satisfy has_image and
        # gets retried on the next run.
        img_path = folder / f"image{ext}"
        tmp = img_path.with_suffix(img_path.suffix + ".part")
        tmp.write_bytes(blob)
        try:
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
        except OSError:
            pass
        tmp.replace(img_path)
        # Sweep any .part left by an earlier attempt. The extension comes
        # from the first one matched in the CDN URL, and wixmp URLs carry
        # several (".../f/x.png/v1/fill/.../y.jpg"), so a retry can land
        # on a different one — leaving image.jpg.part beside a finished
        # image.png forever. Nothing else removes them: both the dedup
        # check and the rebuild only skip .part files. Fully suppressed,
        # so a cleanup failure can never fail a save that has committed.
        for stale in folder.glob("image.*.part"):
            if stale != tmp:
                with contextlib.suppress(OSError):
                    stale.unlink()
    except Exception as e:
        return f"fail:{type(e).__name__}", artist, title, 0

    # Past this line the bytes are on disk under their final name, so the
    # save has succeeded and nothing below may report otherwise. The
    # index write and the rate-limit sleep used to sit inside the try
    # above, which meant a raise from either — a negative delay reaching
    # time.sleep() was the real one — returned "fail" for a deviation
    # that was already committed. Because the feed checkpoint only
    # advances on a run with zero failures, every page then re-synced
    # forever while the gallery was in fact complete.
    index_add(devid, artist, title, folder, len(blob))
    jitter_sleep(image_delay, jitter_pct)
    return "ok", artist, title, len(blob)


def _fetch_metadata_batch(
    ids: list[str],
    cfg: dict[str, str],
    state: dict[str, object],
    mature: bool,
) -> dict[str, dict[str, object]]:
    """Pages through `/deviation/metadata` in 50-id chunks. Routes through
    `dacli.authed_http_json` so a server-side token revocation between the feed
    page fetch and the metadata fetch is auto-recovered (force refresh +
    retry once) instead of dropping the whole page.
    """
    md_by_id: dict[str, dict[str, object]] = {}
    for chunk_start in range(0, len(ids), METADATA_BATCH_SIZE):
        chunk = ids[chunk_start : chunk_start + METADATA_BATCH_SIZE]
        qs = "&".join(f"deviationids[]={x}" for x in chunk)
        body = dacli.authed_http_json(
            f"{API_BASE}/deviation/metadata?{qs}&mature_content={mature_content_param(mature)}",
            cfg,
            state,
        )
        for m in body.get("metadata", []):
            md_by_id[m["deviationid"]] = m
    return md_by_id


def _delays(cfg: dict[str, object], args: argparse.Namespace) -> tuple[float, float, float]:
    # `is None` rather than truthiness: an explicit `--delay-api 0` (or
    # `--jitter 0`) is a real request to disable the delay, and `or`
    # would silently fall through to the config value instead — leaving
    # a user with `jitter` in config unable to turn it off from the CLI.
    def _pick(flag: object, key: str, default: float) -> float:
        # Tolerant on both sides: a CLI flag is validated by argparse's
        # type=, but a config value or a DA_* env var has never been
        # checked by anything. A bare float() here turned one typo into a
        # nightly traceback — see coerce_number.
        if flag is not None:
            return coerce_number(key, flag, default)
        return coerce_number(key, cfg.get(key, default), default)

    api = _pick(args.delay_api, "delay_api", DEFAULT_DELAY_API)
    img = _pick(args.delay_image, "delay_image", DEFAULT_DELAY_IMAGE)
    jit = _pick(getattr(args, "jitter", None), "jitter", DEFAULT_JITTER)
    jit = max(0.0, min(jit, JITTER_MAX_PCT))
    return api, img, jit


def _concurrency(cfg: dict[str, object], args: argparse.Namespace) -> int:
    """Resolve the per-page download concurrency (CLI flag > config > default).
    Clamped to [1, 16] — anything higher is unfriendly to the CDN and the
    GIL means the speedup tapers off anyway since image bytes are bound
    by network IO.
    """
    # Explicit None check — `or` would treat `0` as unset and fall through
    # to the default; we want `0` to clamp to `1` for sensible behavior.
    raw = getattr(args, "concurrency", None)
    if raw is None:
        raw = cfg.get("concurrency", DEFAULT_CONCURRENCY)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = DEFAULT_CONCURRENCY
    return max(CONCURRENCY_MIN, min(n, CONCURRENCY_MAX))


def _save_page_concurrent(
    results: list[dict[str, object]],
    md: dict[str, dict[str, object]],
    dest_root: Path,
    *,
    fallback_artist: str | None,
    image_delay: float,
    jitter_pct: float,
    concurrency: int,
    dry_run: bool = False,
) -> list[tuple[str, str, str, int]]:
    """Save every deviation on one page using a bounded thread pool.

    Each worker calls ``_save_one`` independently, which means each
    download includes its own ``jitter_sleep`` — preserving the per-image
    rate-limiting behaviour while compressing page-level wall time by
    ~Nx (where N=concurrency).

    Returns ``(status, artist, title, size)`` tuples in the same order
    as ``results``. All SQLite access funnels through the single cached
    connection in ``_index()`` guarded by ``_INDEX_LOCK``, so cross-thread
    cursor state can't race (see the long comment on ``_index()`` for why
    one shared connection beats per-thread conns). Logging via
    ``print(..., flush=True)`` is line-atomic under the GIL, so output
    may interleave but won't corrupt.

    With ``concurrency=1`` this falls through to a sequential map —
    useful for tests that rely on deterministic ordering.

    With ``dry_run=True`` no files are written; status is ``"dry"`` for
    unknown deviations and ``"dup"`` for already-known ones.
    """
    if concurrency <= 1:
        return [
            _save_one(
                d,
                md,
                dest_root,
                fallback_artist=fallback_artist,
                image_delay=image_delay,
                jitter_pct=jitter_pct,
                dry_run=dry_run,
            )
            for d in results
        ]

    futures: dict[concurrent.futures.Future, int] = {}
    out: list[tuple[str, str, str, int] | None] = [None] * len(results)
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        for i, d in enumerate(results):
            fut = pool.submit(
                _save_one,
                d,
                md,
                dest_root,
                fallback_artist=fallback_artist,
                image_delay=image_delay,
                jitter_pct=jitter_pct,
                dry_run=dry_run,
            )
            futures[fut] = i
        try:
            for fut in concurrent.futures.as_completed(futures):
                i = futures[fut]
                try:
                    out[i] = fut.result()
                except Exception as e:
                    # Defensive: _save_one catches its own exceptions
                    # and returns a fail status, so this branch should
                    # not fire — but if it does, report it as a failure
                    # carrying the exception class name, with empty
                    # artist and title.
                    out[i] = (f"fail:{type(e).__name__}", "", "", 0)
        except BaseException:
            # Ctrl-C used to do nothing visible: every task for the page
            # was submitted up front, and the executor's __exit__ waits
            # for all of them without cancelling. On a 50-item page at
            # concurrency 4 with the default 1.5s image delay that is
            # another ~19s of downloading after the interrupt. Cancel
            # what has not started, then let __exit__ wait only for the
            # handful already in flight.
            for fut in futures:
                fut.cancel()
            raise
    # Sanity: every future was submitted and either succeeded or wrote a
    # fail tuple. If a slot is still None something silently dropped a
    # deviation — surface it as a fail rather than shrink the list, so
    # the caller's totals stay aligned with the input page.
    return [r if r is not None else ("fail:DroppedFuture", "", "", 0) for r in out]


def _record_crash(kind: str, started: float, exc: BaseException) -> None:
    """Leave a trace when a walk dies from something it does not handle.

    Without this, ``last_sync`` still describes the previous SUCCESSFUL
    run, so `da diagnose` reports a healthy sync hours after tonight's
    failed one — the exact silent-cron-failure mode the summary exists to
    prevent. The message goes to stderr, which for a scheduled run means
    a log file nobody is watching.

    Best-effort: if recording the failure itself fails, the original
    exception still propagates, which is the more important signal.
    """
    with contextlib.suppress(Exception):
        _record_sync_summary(
            kind,
            started,
            {"ok": 0, "dup": 0, "noimg": 0, "fail": 0, "dry": 0},
            f"failed: {type(exc).__name__}: {exc}"[:200],
        )


def cmd_sync_feed(args: argparse.Namespace) -> None:
    started = time.time()
    try:
        with _cmd_lock("sync"):
            _cmd_sync_feed_impl(args)
    except CommandLockedError as e:
        dacli.log(f"skipping: {e}")
        sys.exit(0)
    except SystemExit:
        raise
    except BaseException as e:
        # Includes KeyboardInterrupt: a run someone cancelled halfway is
        # also not the completed run last_sync would otherwise still
        # describe.
        _record_crash("feed", started, e)
        raise


def _cmd_sync_feed_impl(args: argparse.Namespace) -> None:
    cfg = dacli.load_config()
    state = dacli.load_state()
    # Eagerly resolve the token here (refresh-if-expired) so we fail fast
    # on a missing/bad refresh_token before doing any other setup. The
    # actual API calls go through `authed_http_json` which reads
    # state["access_token"] live (and force-refreshes on 401).
    dacli.access_token(cfg, state)
    dest = _ensure_destination(cfg)
    index_bootstrap_if_empty(dest)
    delay_api, delay_image, jitter_pct = _delays(cfg, args)
    concurrency = _concurrency(cfg, args)
    dry_run = bool(getattr(args, "dry_run", False))
    if dry_run:
        dacli.log("DRY RUN — no files will be written; index untouched", "warn")
    if jitter_pct > 0:
        pct = int(jitter_pct * 100)
        dacli.log(f"jitter on: API ±{pct}% / image ±{pct}% around base")
    if concurrency > 1:
        dacli.log(f"image downloads: {concurrency}-way concurrency")
    limit = max(1, min(int(args.limit), FEED_PAGE_CAP))
    mature = args.mature
    budget = int(args.time_budget)

    last_seen = state.get("last_feed_deviationid")
    totals = {"ok": 0, "dup": 0, "noimg": 0, "fail": 0, "dry": 0}
    started = time.time()
    offset = 0
    new_top_id = None
    stop_reason = "complete"

    while time.time() - started < budget - TIME_BUDGET_MARGIN_S:
        dacli.log(f"[{int(time.time() - started)}s] feed offset={offset}")
        try:
            feed = dacli.authed_http_json(
                f"{API_BASE}/browse/deviantsyouwatch?limit={limit}&offset={offset}"
                f"&mature_content={mature_content_param(mature)}",
                cfg,
                state,
            )
        except urllib.error.HTTPError as e:
            if e.code == 429:
                stop_reason = f"HTTP 429 at offset {offset}"
                break
            raise
        results = feed.get("results", [])
        if offset == 0 and results:
            new_top_id = results[0]["deviationid"]
        if not results:
            stop_reason = "feed empty"
            break
        # Trim to last_seen when caught up
        if last_seen:
            for i, d in enumerate(results):  # type: ignore[var-annotated]
                if d["deviationid"] == last_seen:
                    results = results[:i]
                    stop_reason = "caught up"
                    break

        # Skip the metadata batch + per-deviation save loop entirely if
        # every id on this page is already in the synced index. This
        # turns a no-op page from O(metadata-batch + 50 stat() calls)
        # into O(1 SELECT). Critical for keeping daily sync fast.
        ids = [str(d["deviationid"]) for d in results]
        known = index_filter_known(ids)
        if ids and known == set(ids):
            totals["dup"] = totals.get("dup", 0) + len(ids)
            dacli.log(f"  page all-known ({len(ids)} dups) — skipping metadata fetch")
            # The trim above may have set "caught up" for this very page.
            # Without this check the fast path pages on past the
            # checkpoint, re-walking (and re-downloading from) feed
            # history every run.
            if stop_reason != "complete":
                break
            if not feed.get("has_more"):
                stop_reason = "feed exhausted"
                break
            offset += limit
            # Use a shorter delay on the fast path: we're only making one
            # API call per page (the feed itself), so the rate-limit
            # rationale for the full 5s gap doesn't apply.
            jitter_sleep(min(delay_api, SHORT_FAST_PATH_DELAY_S), jitter_pct)
            continue

        jitter_sleep(delay_api, jitter_pct)
        # Only fetch metadata for the unknown ones to save bandwidth.
        unknown_ids = [i for i in ids if i not in known]
        try:
            md = _fetch_metadata_batch(unknown_ids, cfg, state, mature)
        except urllib.error.HTTPError as e:
            # /deviation/metadata shares the rate limiter with the page
            # fetch above, so it gets the same treatment: stop the walk,
            # record why, and let _record_sync_summary run. Unguarded, a
            # 429 here escaped as a traceback and skipped the summary
            # entirely, so `da diagnose` still described the run before.
            dacli.log(f"HTTP {e.code} from deviation/metadata", "error")
            stop_reason = f"http {e.code} (metadata)"
            break
        jitter_sleep(delay_api, jitter_pct)
        page_results = _save_page_concurrent(
            results,
            md,
            dest,
            fallback_artist=None,
            image_delay=delay_image,
            jitter_pct=jitter_pct,
            concurrency=concurrency,
            dry_run=dry_run,
        )
        for status, artist, title, size in page_results:
            key = status if status in totals else "fail"
            totals[key] = totals.get(key, 0) + 1
            if status == "ok":
                dacli.log(f"  + {artist}/{title[:50]:<50} {size / 1024:.0f} KB")
            elif status == "dry":
                dacli.log(f"  would fetch {artist}/{title[:50]}")
            elif key == "fail":
                # Only "ok" and "dry" used to print, so a failed save left
                # nothing behind but a +1 in the summary's fail= count —
                # no id, no artist, no reason. Warn, not info, so it
                # survives --quiet: an unattended run is exactly where
                # this is the only evidence anything went wrong.
                dacli.log(f"  ! {artist}/{title[:50]} — {status}", "warn")
            elif status == "noimg":
                dacli.log(f"  - {artist}/{title[:50]} — no downloadable image")
        if stop_reason != "complete":
            break
        if not feed.get("has_more"):
            stop_reason = "feed exhausted"
            break
        offset += limit

    # Falling out of the loop rather than breaking out of it means the
    # `while` condition went false, and the only thing in that condition
    # is the clock. Every break above sets its own reason first, so
    # "complete" surviving to here can only mean the budget ran out —
    # a truncated walk. Say so: this string is what `da diagnose` shows
    # and what a scheduled run is judged by.
    if stop_reason == "complete":
        stop_reason = TIME_BUDGET_EXHAUSTED

    # Advance the checkpoint ONLY on a clean, complete pass.
    #
    # `last_feed_deviationid` means "everything newer than this has been
    # synced". Writing it after a run that stopped early (429, time
    # budget, per-item failures) or after --dry-run (which downloaded
    # nothing at all) makes the next run trim at that id and skip the
    # gap forever — feed sync never revisits older items.
    checkpoint_reached = stop_reason in {"caught up", "feed exhausted", "feed empty"}
    if new_top_id and checkpoint_reached and not dry_run and totals["fail"] == 0:
        state["last_feed_deviationid"] = new_top_id
        state["last_feed_sync_at"] = int(time.time())
        # Re-read before writing: this dict was loaded before a
        # potentially hours-long run, and an unlocked command (whoami,
        # search, refresh) may have rotated the refresh_token since.
        # Saving the stale snapshot would revoke the live token.
        fresh = dacli.load_state()
        fresh["last_feed_deviationid"] = new_top_id
        fresh["last_feed_sync_at"] = state["last_feed_sync_at"]
        dacli.save_state(fresh)
        state.update(fresh)
    elif new_top_id:
        skipped = "dry run" if dry_run else f"incomplete run ({stop_reason})"
        dacli.log(f"checkpoint not advanced — {skipped}; next sync will re-check these items")

    dacli.log(
        f"feed sync stopped: {stop_reason}; "
        f"ok={totals['ok']} dup={totals['dup']} noimg={totals['noimg']} "
        f"fail={totals.get('fail', 0)}"
    )
    _record_sync_summary("feed", started, totals, stop_reason, last_offset=offset)


def cmd_sync_artist(args: argparse.Namespace) -> None:
    started = time.time()
    try:
        with _cmd_lock("sync"):
            dacli._cmd_sync_artist_impl(args)
    except CommandLockedError as e:
        dacli.log(f"skipping: {e}")
        sys.exit(0)
    except SystemExit:
        raise
    except BaseException as e:
        _record_crash("artist", started, e)
        raise


def _cmd_sync_artist_impl(args: argparse.Namespace) -> None:
    cfg = dacli.load_config()
    state = dacli.load_state()
    # Same fail-fast pattern as _cmd_sync_feed_impl — see comment there.
    dacli.access_token(cfg, state)
    dest = _ensure_destination(cfg)
    index_bootstrap_if_empty(dest)
    delay_api, delay_image, jitter_pct = _delays(cfg, args)
    concurrency = _concurrency(cfg, args)
    dry_run = bool(getattr(args, "dry_run", False))
    if dry_run:
        dacli.log("DRY RUN — no files will be written; index untouched", "warn")
    limit = max(1, min(int(args.limit), GALLERY_PAGE_CAP))
    mature = args.mature
    budget = int(args.time_budget)
    artist = args.artist
    full_resync = bool(getattr(args, "full", False))
    progress = _gallery_progress(state, artist)
    gallery_known_complete = bool(progress.get("complete"))
    requested_offset = getattr(args, "offset", None)
    if requested_offset is None:
        # No explicit --offset: pick up where the last unfinished walk of
        # this gallery stopped. Without this a truncated backfill restarts
        # at 0, hits the early stop on page 0, and never reaches the rest.
        raw_offset = progress.get("offset", 0)
        offset = int(raw_offset) if isinstance(raw_offset, (int, str)) else 0
        if offset:
            dacli.log(f"resuming {artist} at offset {offset} (previous walk did not finish)")
    else:
        offset = max(0, int(requested_offset))
    started = time.time()
    totals = {"ok": 0, "dup": 0, "noimg": 0, "fail": 0, "dry": 0}
    stop_reason = "complete"

    while time.time() - started < budget - TIME_BUDGET_MARGIN_S:
        dacli.log(f"[{int(time.time() - started)}s] gallery/all offset={offset} artist={artist}")
        try:
            feed = dacli.authed_http_json(
                f"{API_BASE}/gallery/all?username={urllib.parse.quote(artist)}"
                f"&limit={limit}&offset={offset}"
                f"&mature_content={mature_content_param(mature)}",
                cfg,
                state,
            )
        except urllib.error.HTTPError as e:
            dacli.log(f"HTTP {e.code} from gallery/all: {e.read().decode()[:200]}", "error")
            stop_reason = f"http {e.code}"
            break
        results = feed.get("results", [])
        has_more = feed.get("has_more", False)
        if not results:
            stop_reason = "empty page"
            break

        # Early-stop semantics: gallery/all returns reverse-chronological.
        # If any item on this page is already in the index, every PAGE
        # below this one would be all-known too — so we mark caught-up
        # and stop paging after we finish processing this page.
        #
        # But we must process ALL unknowns in this page, not just the
        # prefix before the first known. After self-healing
        # (index_filter_known dropping rows whose folder was deleted),
        # the page can look like [KNOWN, KNOWN, SELF-HEALED, KNOWN] and
        # the SELF-HEALED slot is a real "needs redownload" we must
        # honor — not skip.
        ids = [str(d["deviationid"]) for d in results]
        known = index_filter_known(ids)
        unknown_ids_set = {i for i in ids if i not in known}

        # The early stop rests on "gallery/all is reverse-chronological, so
        # everything below a known item is known too". That is only true
        # once this gallery has been walked to its end at least once.
        # Before that the index holds an arbitrary subset — the newest item
        # from a `sync feed` run, or the first page of a walk the time
        # budget cut short — and stopping here strands every older page
        # permanently, because the next run starts at 0 and stops in the
        # same place.
        page_has_known = bool(known)
        if page_has_known and not full_resync and gallery_known_complete:
            # Set the stop_reason now; we still process this page's
            # unknowns first, then break after saving.
            stop_reason = "caught up"

        if not unknown_ids_set:
            # Nothing to download on this page — fast-path to next loop
            # iteration (or break if caught up). No metadata fetch, no
            # save_page_concurrent.
            totals["dup"] = totals.get("dup", 0) + len(known)
            if stop_reason == "caught up":
                break
            if not has_more:
                stop_reason = "gallery complete"
                break
            offset += limit
            continue

        # Filter results to just the unknowns — _save_one would short-
        # circuit known ids as dup anyway, but skipping them up-front
        # keeps the metadata batch and the worker pool tight.
        results = [d for d in results if str(d["deviationid"]) in unknown_ids_set]
        ids = [i for i in ids if i in unknown_ids_set]
        # Account for the known items we're skipping in this page.
        totals["dup"] = totals.get("dup", 0) + len(known)

        jitter_sleep(delay_api, jitter_pct)
        unknown_ids = ids  # all of `ids` are unknown post-filter
        try:
            md = _fetch_metadata_batch(unknown_ids, cfg, state, mature)
        except urllib.error.HTTPError as e:
            dacli.log(f"HTTP {e.code} from deviation/metadata", "error")
            stop_reason = f"http {e.code} (metadata)"
            break
        jitter_sleep(delay_api, jitter_pct)
        page_results = _save_page_concurrent(
            results,
            md,
            dest,
            fallback_artist=artist,
            image_delay=delay_image,
            jitter_pct=jitter_pct,
            concurrency=concurrency,
            dry_run=dry_run,
        )
        for status, _, title, size in page_results:
            key = status if status in totals else "fail"
            totals[key] = totals.get(key, 0) + 1
            if status == "ok":
                dacli.log(f"  + {title[:60]:<60} {size / 1024:.0f} KB")
            elif key == "fail":
                # See the matching note in the feed walk.
                dacli.log(f"  ! {title[:60]} — {status}", "warn")
            elif status == "noimg":
                dacli.log(f"  - {title[:60]} — no downloadable image")
        if stop_reason == "caught up":
            break
        if not has_more:
            stop_reason = "gallery complete"
            break
        offset += limit

    # See the matching note in the feed walk: every break sets a reason,
    # so "complete" here means the clock ended the loop, not the gallery.
    if stop_reason == "complete":
        stop_reason = TIME_BUDGET_EXHAUSTED

    dacli.log(
        f"artist sync stopped at offset {offset}: {stop_reason}; "
        f"ok={totals['ok']} dup={totals['dup']} noimg={totals['noimg']} "
        f"fail={totals.get('fail', 0)}"
    )
    if stop_reason not in ("gallery complete", "caught up"):
        dacli.log(f"resume: da sync artist {artist} --offset {offset}")
    # last_offset is only meaningful as a resume point for non-terminal stops.
    # "gallery complete" and "caught up" finish the work; the next run starts
    # from offset 0. Recording the offset here would mislead diagnose readers.
    extras: dict[str, object] = {"artist": artist}
    if stop_reason not in ("gallery complete", "caught up"):
        extras["last_offset"] = offset

    # Remember where this gallery stands, so the next run can either trust
    # the early stop or pick up where this one left off. `last_sync` cannot
    # carry this: it holds one record for the whole process, and
    # `sync watched` overwrites it once per artist.
    _record_gallery_progress(
        artist,
        complete=stop_reason in ("gallery complete", "caught up"),
        offset=offset,
    )
    _record_sync_summary("artist", started, totals, stop_reason, **extras)


def _gallery_progress(state: dict[str, object], artist: str) -> dict[str, object]:
    """What we know about our progress through one artist's gallery.

    ``{"complete": bool, "offset": int}``. ``complete`` records that a
    walk once reached the end; until then the early-stop below cannot be
    trusted, because it assumes the index already holds everything older
    than whatever it finds.
    """
    galleries = state.get("galleries")
    if not isinstance(galleries, dict):
        return {}
    entry = galleries.get(artist)
    return entry if isinstance(entry, dict) else {}


def _record_gallery_progress(artist: str, *, complete: bool, offset: int) -> None:
    """Persist where this artist's walk got to.

    Re-read before writing, like the feed checkpoint does: this runs after
    a walk that may have taken hours, and `sync watched` writes one of
    these per artist, so the in-memory snapshot is stale by now.
    """
    fresh = dacli.load_state()
    galleries = fresh.get("galleries")
    if not isinstance(galleries, dict):
        galleries = {}
    if complete:
        galleries[artist] = {"complete": True}
    else:
        galleries[artist] = {"complete": False, "offset": offset}
    fresh["galleries"] = galleries
    dacli.save_state(fresh)


def _list_watched_via_friends(
    token: str, username: str, mature: bool, delay: float = 1.0
) -> list[str]:
    """Authoritative enumeration of who you watch — needs `user` scope.

    Bounded three ways, matching ``_discover_watched_via_feed`` below: an
    empty page ends the walk even if ``has_more`` disagrees, the page count
    is capped, and dedup is a set rather than a scan of the accumulator.
    Nothing here has a time budget to fall back on, so trusting the
    server's ``has_more`` alone would let an unattended run page forever.
    """
    watched: list[str] = []
    seen: set[str] = set()
    offset = 0
    for _ in range(FRIENDS_PAGE_MAX):
        body = dacli.http_json(
            f"{API_BASE}/user/friends/{urllib.parse.quote(username)}"
            f"?limit={FRIENDS_PAGE_CAP}&offset={offset}"
            f"&mature_content={mature_content_param(mature)}",
            token=token,
        )
        results = body.get("results", [])
        if not results:
            break
        for r in results:
            u = r.get("user", {}).get("username")
            if u and u not in seen:
                seen.add(u)
                watched.append(u)
        if not body.get("has_more"):
            break
        offset += FRIENDS_PAGE_CAP
        time.sleep(delay)
    else:
        dacli.log(
            f"/user/friends still reported more after {FRIENDS_PAGE_MAX} pages — "
            f"stopping with {len(watched)} artists. Some may be missing.",
            "warn",
        )
    return watched


def _discover_watched_via_feed(
    token: str, mature: bool, max_deviations: int = 2000, delay: float = 2.0
) -> list[str]:
    """
    Fallback discovery: walk /browse/deviantsyouwatch and collect unique
    author usernames. Works with `browse` scope (no `user` scope required).
    Coverage caveat: artists who haven't posted recently may not appear in
    the most recent N pages of the feed.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    offset = 0
    limit = 50
    while offset < max_deviations:
        body = dacli.http_json(
            f"{API_BASE}/browse/deviantsyouwatch?limit={limit}&offset={offset}"
            f"&mature_content={mature_content_param(mature)}",
            token=token,
        )
        results = body.get("results", [])
        if not results:
            break
        for r in results:
            u = (r.get("author") or {}).get("username")
            if u and u not in seen_set:
                seen.append(u)
                seen_set.add(u)
        if not body.get("has_more"):
            break
        offset += limit
        time.sleep(delay)
    return seen


def cmd_sync_watched(args: argparse.Namespace) -> None:
    started = time.time()
    try:
        with _cmd_lock("sync"):
            _cmd_sync_watched_impl(args)
    except CommandLockedError as e:
        dacli.log(f"skipping: {e}")
        sys.exit(0)
    except SystemExit:
        raise
    except BaseException as e:
        # Includes KeyboardInterrupt: a run someone cancelled halfway is
        # also not the completed run last_sync would otherwise still
        # describe.
        _record_crash("watched", started, e)
        raise


def _cmd_sync_watched_impl(args: argparse.Namespace) -> None:
    cfg = dacli.load_config()
    state = dacli.load_state()
    token = dacli.access_token(cfg, state)
    index_bootstrap_if_empty(_ensure_destination(cfg))
    started = time.time()

    watched: list[str] = []
    via = "friends"

    # Strategy 1: friends-list enumeration (authoritative). Needs username and
    # `user` scope. If --user is provided we skip the whoami call.
    if args.user:
        try:
            watched = _list_watched_via_friends(token, args.user, args.mature)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                dacli.log(
                    f"/user/friends/{args.user} returned 403 — token lacks `user` scope", "warn"
                )
                via = "feed"
            else:
                raise
    elif not args.via_feed:
        try:
            me = dacli.http_json(f"{API_BASE}/user/whoami?mature_content=true", token=token)
            username = me.get("username")
            dacli.log(f"authenticated as @{username}")
            watched = _list_watched_via_friends(token, username, args.mature)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                dacli.log(
                    "token lacks `user` scope — falling back to feed-based artist discovery", "warn"
                )
                dacli.log(
                    '(for full enumeration: `da auth --scope "user browse"` then re-run)', "warn"
                )
                via = "feed"
            else:
                raise
    else:
        via = "feed"

    # Strategy 2: walk the watch feed and collect unique authors.
    if via == "feed":
        dacli.log(f"discovering watched artists via feed walk (up to {args.feed_max} deviations)")
        watched = _discover_watched_via_feed(token, args.mature, max_deviations=args.feed_max)

    dacli.log(f"found {len(watched)} watched users (via {via})")
    if not watched:
        dacli.log("no watched users discovered — exiting", "warn")
        return

    # --time-budget bounds the WHOLE run, not each artist. Handing every
    # artist the full budget — as this did — turns `--time-budget 300`
    # over 200 watched artists into a job that can still be running 16
    # hours later, which for the scheduled use case is the entire point
    # of the flag defeated.
    deadline = started + int(args.time_budget)

    artists_done = 0
    artists_failed: list[str] = []
    remaining_artists = 0

    for i, u in enumerate(watched, 1):
        left = int(deadline - time.time())
        if left < MIN_ARTIST_BUDGET_S:
            remaining_artists = len(watched) - i + 1
            dacli.log(
                f"time budget exhausted after {i - 1}/{len(watched)} artists "
                f"({remaining_artists} not attempted) — re-run to continue",
                "warn",
            )
            break

        dacli.log(f"\n=== [{i}/{len(watched)}] {u} ===")
        sub = argparse.Namespace(
            artist=u,
            mature=args.mature,
            # None, not 0: let each artist resume its own unfinished walk.
            offset=None,
            limit=GALLERY_PAGE_CAP,
            # What is left of the global budget, not the whole of it.
            time_budget=left,
            delay_api=args.delay_api,
            delay_image=args.delay_image,
            jitter=getattr(args, "jitter", None),
            full=getattr(args, "full", False),
            concurrency=getattr(args, "concurrency", None),
            dry_run=getattr(args, "dry_run", False),
        )
        try:
            # Call the unlocked impl: cmd_sync_watched already holds the
            # `sync` lock, and re-entering would fail with CommandLockedError.
            dacli._cmd_sync_artist_impl(sub)
            artists_done += 1
        except SystemExit:
            # A per-artist exit is worth skipping past; a dead
            # refresh_token or corrupt index is not — those fail
            # identically for every remaining artist, and continuing
            # would make a doomed token POST per artist and still exit
            # 0, which is the silent-cron-failure mode this codebase
            # works hard to avoid.
            # Set by access_token when DA rejects the grant. Testing for a
            # *missing* refresh_token could never fire — nothing removes
            # it — so this guard was dead code and the loop went on to
            # make one doomed token POST per remaining artist.
            if dacli.load_state().get("refresh_token_rejected_at"):
                raise
            artists_failed.append(u)
            dacli.log(f"  (artist {u} aborted — continuing with the next)", "warn")
        except Exception as e:
            artists_failed.append(u)
            dacli.log(f"  (artist {u} crashed: {type(e).__name__}: {e} — continuing)", "warn")

    if remaining_artists:
        stop_reason = TIME_BUDGET_EXHAUSTED
    elif artists_failed:
        stop_reason = f"{len(artists_failed)} of {len(watched)} artists failed"
    else:
        stop_reason = WATCHED_ALL_COMPLETE

    _record_sync_summary(
        "watched",
        started,
        {
            "artists_total": len(watched),
            "artists_done": artists_done,
            "artists_failed": len(artists_failed),
            "artists_skipped": remaining_artists,
        },
        stop_reason,
        via=via,
    )

    if artists_failed:
        dacli.log(
            f"{len(artists_failed)} of {len(watched)} artists failed: "
            f"{', '.join(artists_failed[:5])}"
            f"{f' (+{len(artists_failed) - 5} more)' if len(artists_failed) > 5 else ''}",
            "error",
        )
        # Previously this exited 0 whatever happened, so a nightly job
        # whose every artist failed looked identical to a clean run. The
        # documented contract is `da sync ... || notify`, and it has to
        # mean something. Partial failure is a 1 ("needs attention"),
        # total failure a 2 ("broken"), matching `da diagnose`.
        sys.exit(2 if artists_done == 0 else 1)
