# 0005. SQLite for the synced-deviation index, not JSON

## Status

Accepted (2026-04-26). Diverges from ADR 0004 on purpose — different
access pattern, different right answer.

## Context

da-cli needs a "have I already downloaded this deviation?" lookup.
Without one, every daily sync re-walks the entire feed/gallery
to re-discover what's already on disk. With one, the daily sync
cost is O(new content), not O(all stories).

Candidates for the index storage:

- **JSON file** (`index.json`) — load on startup, scan for membership.
- **`set` of deviationids in memory** — same as JSON file but no
  persistence.
- **SQLite database** — stdlib (`sqlite3`), real primary keys, real
  indexes, real SELECT WHERE.
- **On-disk stat per deviation** (the v0.1.x approach) — call
  `Path(description.json).exists()` for every candidate. Works for
  small libraries; O(library-size) per sync.

A power user with 100k saved deviations:

- JSON: 100k-element list, ~5 MB on disk, every membership check is
  O(N) without an in-memory `set` (and the in-memory set costs ~10 MB
  of RAM, always resident).
- stat-per-deviation: 100k syscalls per sync, ~30 seconds of pure
  disk-IO overhead.
- SQLite: O(log N) per lookup via the PK index, ~10 MB on disk, lazy
  loaded, sub-millisecond per query.

## Decision

**SQLite (WAL mode, mode 0600) at `~/.local/state/da-cli/index.db`.**

Schema (one table, one secondary index):

```sql
CREATE TABLE synced (
    deviationid TEXT PRIMARY KEY,
    artist      TEXT NOT NULL,
    title       TEXT NOT NULL,
    path        TEXT NOT NULL,
    image_size  INTEGER,
    synced_at   INTEGER NOT NULL
);
CREATE INDEX idx_artist_synced_at ON synced (artist, synced_at DESC);
```

## Consequences

**Positive:**

- O(log N) membership test (`SELECT 1 WHERE deviationid = ?`).
- Bulk filtering in one query (`WHERE deviationid IN (?, ?, ?, ...)`).
- Power-user case (100k rows) is sub-millisecond per query.
- SQLite is in the stdlib; no new runtime dep (per ADR 0001).
- WAL mode tolerates cross-process access; the manual sync and the
  launchd sync can overlap without corrupting the file.
- The `(artist, synced_at DESC)` index powers `da index show`'s
  "top 10 artists" report in O(log N + 10).
- Self-healing: stale rows (folder deleted out from under the index)
  are dropped on the next membership check.

**Negative:**

- The file is binary; can't `cat` it for debugging. Workaround:
  `sqlite3 ~/.local/state/da-cli/index.db "SELECT * FROM synced LIMIT 10"`.
- SQLite has a per-connection cost (~1 ms to open). Mitigated by
  caching one connection per process in `_INDEX_CONN`.
- `PRAGMA cache_size = -65536` (64 MiB) is configurable but the
  default of 2 MiB works fine for small libraries.
- One concurrency constraint: all SQLite access goes through a single
  `_INDEX_LOCK` (threading.Lock) because Python's `sqlite3` has
  subtle cursor-state quirks across threads even with
  `check_same_thread=False`. Lock hold time is sub-millisecond;
  image downloads dominate wall time by 4-5 orders of magnitude.

## Alternatives considered

- **JSON file**: rejected for the 100k-row case.
- **`set` in memory**: rejected for the persistence requirement.
- **stat-per-deviation**: rejected for the O(N) cost per sync.
