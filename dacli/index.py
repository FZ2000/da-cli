"""The synced-deviation index (SQLite).

Extracted verbatim from the single-file module; see ADR 0007.

``INDEX_PATH`` is read as ``dacli.INDEX_PATH`` because the autouse test
fixture repoints it at a temp directory; importing the value would pin
the real ~/.local/state/da-cli/index.db.

``_BOOTSTRAP_CHECKED_THIS_PROCESS`` lives on the package for the same
reason — the fixture resets it as ``dacli._BOOTSTRAP_CHECKED_THIS_PROCESS``,
and a ``global`` rebind here would be invisible to it.

``_INDEX_CONN`` deliberately stays a plain module global. Nothing
outside this module touches it: the test fixture calls ``_index_close()``
rather than patching the connection, precisely because restoring a
stale connection object breaks the concurrency tests.
"""

import atexit
import contextlib
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

import dacli

# --------------------------------------------------------------------------
# Synced-deviation index (SQLite)
#
# Why SQLite, not JSON: this index can grow to 100k+ rows for power users.
# We need O(1) membership tests (`SELECT 1 WHERE deviationid = ?`) and
# bulk filtering (`WHERE deviationid IN (?, ?, ?, ...)`) without loading
# the whole set into memory. SQLite is in the stdlib, so it doesn't break
# the no-runtime-deps constraint.
#
# Why this matters: without an index, every sync pays for the full feed
# / gallery walk even when nothing has changed — O(all stories). With it,
# sync_artist early-stops at the first known deviationid (the gallery is
# returned in reverse-chronological order), making subsequent runs
# O(new content) instead of O(gallery size).
# --------------------------------------------------------------------------
_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS synced (
    deviationid TEXT PRIMARY KEY,
    artist      TEXT NOT NULL,
    title       TEXT NOT NULL,
    path        TEXT NOT NULL,
    image_size  INTEGER,
    synced_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artist_synced_at ON synced (artist, synced_at DESC);
"""

_INDEX_CONN: sqlite3.Connection | None = None
# All SQLite access goes through this lock. SQLite ops are microseconds;
# image downloads in worker threads are seconds. So the lock contention
# is negligible (<1ms total per page) while making the synced-index
# perfectly thread-safe regardless of how many download workers race.
_INDEX_LOCK = threading.Lock()


def _index() -> sqlite3.Connection:
    """Open (and cache) the synced index. Caller must hold `_INDEX_LOCK`
    for any execute. WAL + autocommit + busy_timeout are belt-and-
    suspenders against any cross-process or cross-thread contention.

    Why one cached connection + global lock instead of per-thread conns:
    Python's sqlite3 has subtle cursor-state quirks across threads even
    with `check_same_thread=False`. A single conn + lock is the cleanest
    correctness story and the overhead is negligible (image-download
    latency dominates by 4-5 orders of magnitude).
    """
    global _INDEX_CONN  # noqa: PLW0603 — module-level cache is intentional
    if _INDEX_CONN is not None:
        return _INDEX_CONN
    dacli.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(dacli.INDEX_PATH, check_same_thread=False, isolation_level=None)
    # WAL + busy_timeout is the baseline. cache_size / mmap_size / temp_store
    # turn the common COUNT/GROUP BY queries into in-memory ops for the
    # 100k-row power-user case; synchronous=NORMAL is safe under WAL and
    # halves fsync cost. See SQLite docs <https://sqlite.org/pragma.html>.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -65536")  # 64 MiB
    conn.execute("PRAGMA mmap_size = 268435456")  # 256 MiB
    conn.executescript(_INDEX_SCHEMA)
    with contextlib.suppress(OSError):
        os.chmod(dacli.INDEX_PATH, 0o600)
    _INDEX_CONN = conn
    atexit.register(_index_close)
    return conn


def _index_close() -> None:
    global _INDEX_CONN  # noqa: PLW0603 — module-level cache is intentional
    if _INDEX_CONN is not None:
        with contextlib.suppress(sqlite3.Error):
            _INDEX_CONN.close()
        _INDEX_CONN = None


def read_folder_description(folder: Path) -> dict[str, object] | None:
    """``description.json`` parsed as an object, or None if it is not one.

    None covers every way the file can fail to describe a deviation:
    missing, unreadable, invalid JSON, or valid JSON of the wrong shape.
    That last one is not hypothetical — a file holding ``[]`` parses
    fine, and calling ``.get()`` on the result raised ``AttributeError``
    in the middle of a save, which no caller expected or caught.
    """
    try:
        desc = json.loads((folder / "description.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return desc if isinstance(desc, dict) else None


class SyncedFolder(NamedTuple):
    """What a complete deviation folder holds. See read_synced_folder."""

    deviationid: str
    image_size: int
    description: dict[str, object]


def read_synced_folder(folder: Path) -> SyncedFolder | None:
    """The folder's contents if it holds a complete deviation, else None.

    The single definition of "synced", shared by every caller that needs
    to answer that question. There used to be three, and they disagreed:

    * ``_save_one``'s backfill wanted an image with bytes in it, and
      never opened ``description.json``
    * ``_folder_is_complete`` wanted only that both names existed
    * ``index_rebuild_from_disk`` wanted the JSON to parse, name a
      deviationid, and have a non-empty image

    A folder whose metadata was empty or truncated — which is what a
    power cut mid-write leaves — was therefore synced to the first two
    and not to the third. The consequence was silent and permanent: the
    backfill indexed it, so every later run said "dup", the metadata was
    never re-fetched, and a rebuild dropped the row only for the next
    sync to put it straight back.

    Complete means all four, in order of cost:

    1. ``description.json`` is readable
    2. it parses, as an object
    3. it names a ``deviationid`` — the field every caller keys on
    4. a non-``.part`` ``image.*`` exists and is non-empty

    ``.part`` is skipped for the same reason ``_save_one`` stages to one:
    a crash mid-download must not look finished. A 0-byte image is a
    failed download, and a ``stat`` that raises means a dangling symlink
    or an entry removed mid-scan — neither is synced, and neither should
    end the caller's walk.
    """
    desc = read_folder_description(folder)
    if desc is None:
        return None
    devid = desc.get("deviationid")
    if not devid:
        return None
    for image in folder.glob("image.*"):
        if image.suffix == ".part":
            continue
        try:
            size = image.stat().st_size
        except OSError:
            continue
        if size > 0:
            return SyncedFolder(str(devid), size, desc)
    return None


def _folder_is_complete(folder: Path) -> bool:
    """Is the content behind an index row still on disk?

    Deliberately cheaper than ``read_synced_folder``: this runs for every
    row of every page, under ``_INDEX_LOCK``, so a JSON parse here is
    paid by all 16 download workers. Parsing to confirm the folder names
    *this* deviation measured 2.6x slower per lookup on a real 15,699-row
    gallery, and bought nothing — the index is keyed by deviationid and
    paths are unique per deviation, so a row pointing at a folder that
    belongs to someone else needs a corrupt index, not a corrupt folder.
    ``_save_one``'s backfill is where a folder's ownership is actually in
    question, and it does the full check.

    What this does check, beyond the names existing, is that the image
    has bytes. A 0-byte file and a dangling symlink both leave a
    directory entry, and both used to count as synced — so the documented
    repair (delete the bad image, re-run) silently did nothing.
    """
    if not (folder / "description.json").exists():
        return False
    for image in folder.glob("image.*"):
        if image.suffix == ".part":
            continue
        try:
            if image.stat().st_size > 0:
                return True
        except OSError:
            # Dangling symlink, or removed between the glob and the stat.
            continue
    return False


def index_has(devid: str) -> bool:
    """O(1): is this deviation already saved AND still on disk?

    Self-healing: if the index claims a row but the indexed folder is
    gone (operator deleted, external drive remounted at different path),
    the stale row is dropped here so the caller redownloads. Without
    this, `rm -rf gallery/some-artist` would leave the index lying
    forever and sync would say "all dup, caught up" without restoring
    anything. The existence check is a single stat() — sub-millisecond,
    and only happens on rows we'd otherwise call dup.
    """
    with _INDEX_LOCK:
        row = (
            _index()
            .execute("SELECT path FROM synced WHERE deviationid = ? LIMIT 1", (devid,))
            .fetchone()
        )
        if row is None:
            return False
        if _folder_is_complete(Path(row[0])):
            return True
        # Stale row — the content is gone. Drop and report as not-known.
        _index().execute("DELETE FROM synced WHERE deviationid = ?", (devid,))
        return False


def index_filter_known(devids: list[str]) -> set[str]:
    """Return the subset of devids that are already in the index AND
    still on disk. Self-healing — see `index_has` for rationale.

    Drops stale rows in bulk: one DELETE for any rows whose path is
    gone. Cost is O(N stats) per page where N is the page size (24-50);
    indistinguishable from previous performance for the common case
    where nothing has been deleted out from under the index.
    """
    if not devids:
        return set()
    placeholders = ",".join("?" * len(devids))
    with _INDEX_LOCK:
        rows = (
            _index()
            .execute(
                f"SELECT deviationid, path FROM synced WHERE deviationid IN ({placeholders})",  # noqa: S608
                devids,
            )
            .fetchall()
        )
        live: set[str] = set()
        stale: list[str] = []
        for d, p in rows:
            # Same completeness test as index_has. These two answer the
            # same question — "is this still on disk?" — and disagreeing
            # meant a deviation could be repaired on the per-item path but
            # skipped on the bulk one, depending only on page composition.
            if _folder_is_complete(Path(p)):
                live.add(d)
            else:
                stale.append(d)
        if stale:
            ph = ",".join("?" * len(stale))
            _index().execute(
                f"DELETE FROM synced WHERE deviationid IN ({ph})",  # noqa: S608
                stale,
            )
    return live


def index_add(devid: str, artist: str, title: str, path: Path, image_size: int) -> None:
    """Upsert one deviation. Safe to call repeatedly with the same id.
    Thread-safe via _INDEX_LOCK — a pool of download workers all queue
    here at the end of each download. Lock hold time is sub-millisecond
    (one INSERT in autocommit mode); the parallelism we care about is
    the seconds-long network IO above this layer.
    """
    row = (
        devid,
        artist,
        title,
        str(path),
        image_size,
        int(time.time()),
    )
    with _INDEX_LOCK:
        _index().execute(
            "INSERT OR REPLACE INTO synced "
            "(deviationid, artist, title, path, image_size, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            row,
        )


def index_count(artist: str | None = None) -> int:
    """Row count, optionally scoped to one artist. Thread-safe."""
    with _INDEX_LOCK:
        if artist is None:
            row = _index().execute("SELECT COUNT(*) FROM synced").fetchone()
        else:
            row = (
                _index()
                .execute("SELECT COUNT(*) FROM synced WHERE artist = ?", (artist,))
                .fetchone()
            )
    return int(row[0]) if row else 0


def _would_wipe(found: int) -> bool:
    """Would repopulating with `found` rows empty a populated index?

    `dest.exists()` alone was not enough to protect against this: an
    unmounted external drive usually leaves its mount point behind, and
    anything that creates the directory first turns "not mounted" into
    "empty". Finding nothing on disk while the index is full describes
    exactly that, and never describes a situation where wiping is what
    the operator wanted.
    """
    return not found and bool(index_count())


def index_rebuild_from_disk(dest: Path) -> int:
    """
    Walk `dest` and rebuild the synced-index AUTHORITATIVELY: drop the
    existing index contents, then repopulate from any folder containing
    both description.json and a non-.part image.*. Returns the number
    of rows inserted.

    Authoritative (not incremental): if a folder was deleted from disk
    the corresponding index row is removed, not left as stale. This is
    what `da index rebuild` is for — to bring the index back in sync
    with reality after operator deletions, partial restores, or moves.

    Safety: when `dest` is missing (external drive unplugged, mount
    point not yet ready, typo in config), do NOT wipe — return 0 and
    leave the index alone. Operator probably wants to remount, not
    discover that all 14k rows just got nuked.
    """
    if not dest.exists():
        return 0
    rows: list[tuple[str, str, str, str, int, int]] = []
    now = int(time.time())
    for desc_path in dest.glob("*/*/description.json"):
        folder = desc_path.parent
        # The shared definition — see read_synced_folder. It also keeps a
        # single bad entry from ending the walk: this loop used to stat()
        # outside the try, so one dangling image symlink aborted the whole
        # rebuild and reached the operator as "mount your external drive".
        found = read_synced_folder(folder)
        if found is None:
            continue
        desc = found.description
        artist = str(desc.get("author_username") or folder.parent.name)
        title = str(desc.get("title") or folder.name)
        rows.append((found.deviationid, artist, title, str(folder), found.image_size, now))

    # Second half of the "don't wipe" guard above. `dest.exists()` alone
    # is not enough: an unmounted external drive usually leaves its mount
    # point behind, and anything that creates the directory first turns
    # "not mounted" into "empty". Finding nothing on disk while the index
    # is full describes exactly that, and never describes a situation
    # where wiping is what the operator wanted.
    # Missing and empty are the same situation; both must leave the index
    # alone. Returning 0 keeps this a library call — the command layer is
    # what tells the operator, and what sets the exit code.
    if _would_wipe(len(rows)):
        return 0

    # Drop+repopulate inside one lock acquisition. SQLite's autocommit
    # mode means each statement is its own transaction, but holding the
    # _INDEX_LOCK across both prevents any concurrent index_add worker
    # from interleaving an INSERT between our DELETE and executemany.
    with _INDEX_LOCK:
        conn = _index()
        conn.execute("DELETE FROM synced")
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO synced "
                "(deviationid, artist, title, path, image_size, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
    return len(rows)


# Seed value; the re-export binds it into the dacli namespace, and the
# autouse test fixture resets it there as dacli._BOOTSTRAP_CHECKED_THIS_PROCESS.
_BOOTSTRAP_CHECKED_THIS_PROCESS = False


def index_bootstrap_if_empty(dest: Path) -> int:
    """
    Lazy migration: if the index is empty but the destination has content,
    populate the index from disk. Returns the number of rows imported.

    Memoized for the lifetime of the process — once we've checked once,
    don't re-walk the destination on subsequent calls. Without this,
    `cmd_sync_watched` (which loops `_cmd_sync_artist_impl` per watched
    user) would do an N*O(disk-walk) bootstrap check before any of the
    artists had managed to populate the index.
    """
    if dacli._BOOTSTRAP_CHECKED_THIS_PROCESS:
        return 0
    dacli._BOOTSTRAP_CHECKED_THIS_PROCESS = True
    # Health-check the index. A corrupt SQLite file lets `connect()` succeed
    # but every query raises DatabaseError. Without this check the operator
    # gets a worker-pool stack trace mid-page; with it, they get a single
    # actionable line. The check is cheap (one COUNT) and runs once per
    # process so it doesn't add hot-path overhead.
    try:
        existing = index_count()
    except sqlite3.DatabaseError as e:
        dacli.log(
            f"synced-index is corrupt ({e}). "
            f"Delete {dacli.INDEX_PATH} and run `da index rebuild` to regenerate.",
            "error",
        )
        sys.exit(2)
    if existing > 0:
        return 0
    if not dest.exists():
        return 0
    dacli.log("synced-index is empty; bootstrapping from existing destination...")
    n = index_rebuild_from_disk(dest)
    dacli.log(f"  imported {n:,} existing deviations into the index")
    return n
