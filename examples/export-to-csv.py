#!/usr/bin/env python3
"""examples/export-to-csv.py

Read the synced-deviation SQLite index and emit a CSV of every saved
deviation. Useful for building a spreadsheet of "what I've collected",
feeding a separate indexer, or sending a list to a friend.

No third-party deps — uses the stdlib `csv` and `sqlite3` modules
directly. The index file lives at $XDG_STATE_HOME/da-cli/index.db by
default; override by setting XDG_STATE_HOME.

Usage:
    python3 examples/export-to-csv.py > /tmp/da-index.csv
    # or
    XDG_STATE_HOME=/custom/state python3 examples/export-to-csv.py out.csv
"""

from __future__ import annotations

import csv
import os
import sqlite3
import sys
from pathlib import Path


def main() -> None:
    state_dir = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "da-cli"
    index_path = state_dir / "index.db"
    if not index_path.exists():
        sys.stderr.write(f"error: no index at {index_path} — run `da sync` first.\n")
        sys.exit(1)

    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    out = out_path.open("w", newline="") if out_path else sys.stdout

    try:
        conn = sqlite3.connect(index_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT deviationid, artist, title, path, image_size, synced_at "
            "FROM synced ORDER BY artist, synced_at"
        ).fetchall()
        writer = csv.writer(out)
        writer.writerow(["deviationid", "artist", "title", "path", "image_size", "synced_at"])
        writer.writerows([tuple(r) for r in rows])
        conn.close()
    finally:
        if out_path:
            out.close()

    sys.stderr.write(f"wrote {len(rows)} rows\n")


if __name__ == "__main__":
    main()
