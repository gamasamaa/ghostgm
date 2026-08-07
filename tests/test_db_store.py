"""The store's foundations: connect, migrate, and the guarantees built on them.

Nothing here touches the network, the model, or the real database file.
"""

import sqlite3

import pytest

from db import store

EXPECTED_TABLES = ["board_snapshots", "sessions", "state_events", "turns"]


@pytest.fixture
def db(tmp_path):
    conn = store.open_db(tmp_path / "test.db")
    yield conn
    conn.close()


def add_session(conn, session_id="abc123"):
    conn.execute(
        "INSERT INTO sessions (session_id, turn_count, ingested_at) "
        "VALUES (?, 0, '2026-08-07T00:00:00')", (session_id,))
    return session_id


# --- migration ------------------------------------------------------------


def test_open_db_creates_the_file_and_every_table(tmp_path):
    path = tmp_path / "nested" / "dir" / "test.db"
    conn = store.open_db(path)

    assert path.exists(), "parent directories should be created, not demanded"
    assert store.table_names(conn) == EXPECTED_TABLES
    assert store.schema_version(conn) == store.SCHEMA_VERSION
    conn.close()


def test_reopening_is_a_no_op_and_keeps_the_data(tmp_path):
    path = tmp_path / "test.db"
    first = store.open_db(path)
    add_session(first, "keepme")
    first.close()

    second = store.connect(path)
    assert store.migrate(second) is False, "an up-to-date file needs no work"

    rows = second.execute("SELECT session_id FROM sessions").fetchall()
    assert [r["session_id"] for r in rows] == ["keepme"]
    second.close()


def test_migrate_reports_whether_it_applied_anything(tmp_path):
    conn = store.connect(tmp_path / "test.db")
    assert store.migrate(conn) is True, "a fresh file starts at v0"
    assert store.migrate(conn) is False
    conn.close()


def test_a_partial_migration_is_finished_by_the_next_one(tmp_path):
    """A crash mid-schema leaves v0, so the next open picks it back up."""
    path = tmp_path / "test.db"
    conn = store.connect(path)
    conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, "
                 "turn_count INTEGER NOT NULL DEFAULT 0, ingested_at TEXT NOT NULL)")
    assert store.schema_version(conn) == 0

    assert store.migrate(conn) is True
    assert store.table_names(conn) == EXPECTED_TABLES
    assert store.schema_version(conn) == store.SCHEMA_VERSION
    conn.close()


def test_a_newer_schema_is_refused_rather_than_guessed_at(tmp_path):
    path = tmp_path / "test.db"
    conn = store.open_db(path)
    conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 1}")
    conn.close()

    # Matching the message, not just the type: the other SchemaTooNew branch
    # ("no upgrade path") would otherwise let a broken version check pass here.
    with pytest.raises(store.SchemaTooNew, match="package speaks"):
        store.open_db(path)


def test_a_failed_open_does_not_leak_the_connection(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    conn = store.connect(path)
    conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 1}")
    conn.close()

    closed = []
    real_connect = store.connect
    monkeypatch.setattr(store, "connect", lambda p=path: _spy(real_connect(p), closed))

    with pytest.raises(store.SchemaTooNew):
        store.open_db(path)
    assert closed == [True]


class _CloseSpy:
    """Connection proxy that records close(). sqlite3.Connection won't let its
    own attributes be reassigned, so the spy has to sit in front of it."""

    def __init__(self, conn, closed):
        self._conn = conn
        self._closed = closed

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        self._closed.append(True)
        self._conn.close()


def _spy(conn, closed):
    return _CloseSpy(conn, closed)


# --- pragmas --------------------------------------------------------------


def test_foreign_keys_are_enforced(db):
    """Without the pragma sqlite ignores REFERENCES entirely, and the schema's
    cascades would silently do nothing."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO turns (session_id, turn, \"user\", assistant) "
            "VALUES ('ghost', 1, 'hi', 'there')")


def test_deleting_a_session_takes_its_rows_with_it(db):
    session_id = add_session(db)
    db.execute("INSERT INTO turns (session_id, turn, \"user\", assistant) "
               "VALUES (?, 1, 'hi', 'there')", (session_id,))
    db.execute("INSERT INTO board_snapshots (session_id, turn, state, recorded_at) "
               "VALUES (?, 1, '{}', '2026-08-07T00:00:00')", (session_id,))
    db.execute(
        "INSERT INTO state_events (session_id, turn, seq, token_id, kind, "
        "extractor, recorded_at) VALUES (?, 1, 0, 'bram', 'hp', 'regex@1', "
        "'2026-08-07T00:00:00')", (session_id,))

    db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    for table in ("turns", "board_snapshots", "state_events"):
        count = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        assert count == 0, f"{table} rows outlived their session"


def test_rows_come_back_as_mappings(db):
    add_session(db, "readable")
    row = db.execute("SELECT session_id, turn_count FROM sessions").fetchone()
    assert row["session_id"] == "readable"
    assert row["turn_count"] == 0


def test_connect_supports_an_in_memory_database():
    conn = store.connect(":memory:")
    store.migrate(conn)
    assert store.table_names(conn) == EXPECTED_TABLES
    conn.close()


# --- transactions ---------------------------------------------------------


def test_transaction_commits_on_success(db):
    with store.transaction(db):
        add_session(db, "committed")
    assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def test_transaction_rolls_the_whole_block_back(db):
    """A half-written ingest must leave no session describing turns that
    were never stored."""
    with pytest.raises(ValueError):
        with store.transaction(db):
            add_session(db, "doomed")
            raise ValueError("ingest died halfway")

    assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_statements_outside_a_transaction_autocommit(tmp_path):
    """isolation_level=None means no driver-managed transaction is holding
    writes back — another connection sees them immediately."""
    path = tmp_path / "test.db"
    writer = store.open_db(path)
    add_session(writer, "visible")

    reader = store.connect(path)
    assert reader.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    reader.close()
    writer.close()


# --- schema shape ---------------------------------------------------------


def test_several_extractors_can_disagree_about_the_same_turn(db):
    """The whole point of state_events.extractor: competing readings of one
    turn coexist, so they can be scored against each other."""
    session_id = add_session(db)
    for extractor in ("regex@1", "model@flash", "human"):
        db.execute(
            "INSERT INTO state_events (session_id, turn, seq, token_id, kind, "
            "delta, phrase, extractor, recorded_at) VALUES (?, 1, 0, 'bram', "
            "'hp', -9, 'Bram takes 9 damage', ?, '2026-08-07T00:00:00')",
            (session_id, extractor))

    count = db.execute("SELECT count(*) FROM state_events WHERE turn = 1").fetchone()[0]
    assert count == 3


def test_one_extractor_cannot_write_the_same_event_twice(db):
    """Re-running an extractor over a turn must not stack duplicate deltas."""
    session_id = add_session(db)
    insert = ("INSERT INTO state_events (session_id, turn, seq, token_id, kind, "
              "extractor, recorded_at) VALUES (?, 1, 0, 'bram', 'hp', 'regex@1', "
              "'2026-08-07T00:00:00')")
    db.execute(insert, (session_id,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, (session_id,))


def test_a_session_cannot_hold_two_records_for_one_turn(db):
    session_id = add_session(db)
    insert = ("INSERT INTO turns (session_id, turn, \"user\", assistant) "
              "VALUES (?, 1, 'hi', 'there')")
    db.execute(insert, (session_id,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, (session_id,))


def test_turns_carry_every_field_the_transcript_records(db):
    """The transcript record is the contract; a column missing here means an
    ingest would silently drop data."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(turns)")}
    assert {"session_id", "turn", "ts", "model", "user", "assistant",
            "input_tokens", "output_tokens", "total_tokens", "latency_ms",
            "ttft_ms"} <= columns


def test_ttft_may_be_null_because_a_silent_stream_produces_none(db):
    session_id = add_session(db)
    db.execute("INSERT INTO turns (session_id, turn, \"user\", assistant, ttft_ms) "
               "VALUES (?, 1, 'hi', '', NULL)", (session_id,))
    assert db.execute("SELECT ttft_ms FROM turns").fetchone()["ttft_ms"] is None
