"""Connections and migration for the GhostGM store.

Standard library only, and this package imports nothing from `ghost`, `visual`
or `prep` — the store is a thing they can be pointed at later, not a dependency
any of them has now.

The database is a projection of `transcripts/*.jsonl` (see schema.sql). Deleting
the file loses nothing that a re-ingest can't rebuild, which is what keeps the
phase 0 baseline safe: the experiment's data is still plain text on disk.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Bumped when schema.sql changes in a way an existing file can't just absorb.
# Stored in the database as PRAGMA user_version, which sqlite keeps in the file
# header for exactly this purpose — no metadata table of our own required.
SCHEMA_VERSION = 1

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# Resolved against the repo rather than the working directory, so `python -m db`
# behaves the same from anywhere. data/ is gitignored: it is derived, not source.
DEFAULT_DB_PATH = REPO_ROOT / "data" / "ghostgm.db"

# How long a writer will wait on a lock before giving up, in milliseconds. The
# CLI, the test suite and a running server can all hold the file at once.
BUSY_TIMEOUT_MS = 5000


class SchemaTooNew(RuntimeError):
    """The file was written by a newer db/ than this one, and isn't readable.

    Downgrading a schema is guesswork, so this refuses rather than risking a
    silent misread of someone else's data.
    """


def connect(path=DEFAULT_DB_PATH):
    """Open a connection with this project's pragmas applied. No migration.

    `path` may be ':memory:' for a throwaway database. Parent directories are
    created for real paths, so a fresh clone needs no setup step.
    """
    if path != ":memory:":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path = str(path)

    # isolation_level=None turns off the driver's implicit transactions, so the
    # only BEGIN/COMMIT in this package are the ones written down in
    # `transaction()`. Statements outside one autocommit, which is what a
    # read-mostly store wants.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row

    # Both pragmas are per-connection and have to be re-applied every time;
    # foreign_keys especially, since sqlite silently ignores REFERENCES without
    # it and the schema's ON DELETE CASCADE would quietly do nothing.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")

    # WAL is a property of the file, not the connection, so this only does work
    # the first time. It lets readers run while an ingest writes. Some network
    # filesystems refuse it; the store is perfectly correct without it, so a
    # refusal is not worth failing over.
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass

    return conn


def schema_version(conn):
    """The version stamped in the file. 0 means nothing has been applied yet."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn):
    """Bring `conn` up to SCHEMA_VERSION. Safe to call on every open.

    Returns True when it applied anything, False when the file was already
    current — worth knowing in a CLI, and it makes "opening twice is a no-op"
    a claim a test can actually check.
    """
    found = schema_version(conn)
    if found == SCHEMA_VERSION:
        return False
    if found > SCHEMA_VERSION:
        raise SchemaTooNew(
            f"database is at schema v{found}, this db/ package speaks "
            f"v{SCHEMA_VERSION}. Upgrade the code, or delete the file and "
            f"re-ingest — it is a projection, nothing original is lost.")

    # v0 is a fresh (or pre-versioning) file: schema.sql is all IF NOT EXISTS,
    # so applying it is safe either way. Once there is a v2, this is where the
    # step-by-step upgrades go, keyed off `found`.
    if found != 0:
        raise SchemaTooNew(
            f"no upgrade path from schema v{found} to v{SCHEMA_VERSION}")

    # executescript() commits any transaction open when it starts, so it can't
    # run inside `transaction()` — the COMMIT at the end of the block would
    # find nothing active and raise. It doesn't need one: every statement in
    # schema.sql is IF NOT EXISTS and the stamp below is the last thing to
    # land, so a run that dies halfway leaves the file at v0 and the next
    # migrate() simply finishes the job.
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    # No parameter binding: sqlite rejects a placeholder in a PRAGMA. The value
    # is a module constant, never caller input.
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return True


def open_db(path=DEFAULT_DB_PATH):
    """The normal entry point: a connected, migrated database."""
    conn = connect(path)
    try:
        migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn):
    """Run a block as one transaction, rolling the whole thing back on error.

    An ingest that dies halfway must not leave a session row describing turns
    that were never written.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def table_names(conn):
    """Every table in the file, for tests and for `db stats`."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [row["name"] for row in rows]
