"""Index management commands.

Names the test suite patches are read through the ``dacli`` package at
call time; see ADR 0007.
"""

import argparse
import sys

import dacli

from ..index import _INDEX_LOCK


# --------------------------------------------------------------------------
# Index management commands
# --------------------------------------------------------------------------
def cmd_index_rebuild(args: argparse.Namespace) -> None:
    """Walk the destination and rebuild the synced-index from disk."""
    cfg = dacli.load_config()
    # create=False: creating the destination first would turn an unmounted
    # drive into an empty directory, which is the one case
    # index_rebuild_from_disk refuses to act on.
    dest = dacli._ensure_destination(cfg, create=False)
    dacli.log(f"rebuilding synced-index from {dest} ...")
    before = dacli.index_count()
    n = dacli.index_rebuild_from_disk(dest)
    if n == 0 and before:
        # index_rebuild_from_disk declined rather than wiping. Say why, and
        # exit non-zero: "done: 0 deviations indexed" reads like success,
        # and a scheduled repair job would never notice.
        dacli.log(
            f"found no synced deviations under {dest}, so the index was left "
            f"untouched ({before:,} rows). If that is an external drive, mount "
            f"it and retry; to wipe deliberately, delete {dacli.INDEX_PATH}.",
            "error",
        )
        sys.exit(2)
    dacli.log(f"  done: {n:,} deviations indexed")


def cmd_index_show(args: argparse.Namespace) -> None:
    """Print summary stats for the synced-index."""
    if not dacli.INDEX_PATH.exists():
        dacli.log("(no index yet — run a sync or `da index rebuild`)", "warn")
        return
    total = dacli.index_count()
    size = dacli.INDEX_PATH.stat().st_size
    print(f"index file:  {dacli.INDEX_PATH}")
    print(f"total rows:  {total:,}")
    print(f"db size:     {size / 1024:.1f} KB")
    with _INDEX_LOCK:
        rows = (
            dacli._index()
            .execute(
                "SELECT artist, COUNT(*) AS n FROM synced GROUP BY artist ORDER BY n DESC LIMIT 10"
            )
            .fetchall()
        )
    if rows:
        print("\ntop 10 artists by indexed deviations:")
        for artist, n in rows:
            print(f"  {n:>5}  {artist}")
