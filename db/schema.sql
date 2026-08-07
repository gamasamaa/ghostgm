-- GhostGM store, schema v1.
--
-- Everything here is a projection. `transcripts/*.jsonl` remain the source of
-- truth for the experiment; this file describes an index over them plus the
-- board state that was tracked beside them. The database can be deleted at any
-- time and rebuilt by re-ingesting the transcripts, so nothing in here is
-- allowed to hold a fact that exists nowhere else.
--
-- Applied by db.store.migrate() against a connection, guarded by
-- PRAGMA user_version. Every statement is IF NOT EXISTS so re-applying it to an
-- already-migrated database is a no-op.

-- One row per transcript file. The columns after `session_id` are all
-- summaries of that file's turns, recomputed on ingest rather than maintained
-- incrementally — a projection has no business drifting from its source.
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    model           TEXT,               -- model of the most recent turn
    started_at      TEXT,               -- ts of the first turn, ISO 8601
    ended_at        TEXT,               -- ts of the last turn seen
    turn_count      INTEGER NOT NULL DEFAULT 0,
    transcript_path TEXT,               -- where it was read from, for re-ingest
    ingested_at     TEXT NOT NULL
);

-- One row per transcript record, field for field. `model` is repeated per turn
-- because the transcript states it per turn: a session that changed models
-- mid-run is a fact worth keeping rather than flattening.
CREATE TABLE IF NOT EXISTS turns (
    session_id    TEXT    NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    turn          INTEGER NOT NULL,
    ts            TEXT,
    model         TEXT,
    "user"        TEXT    NOT NULL,     -- what was sent, unstyled, as stored
    assistant     TEXT    NOT NULL,     -- what came back, unstyled, as stored
    input_tokens  INTEGER,
    output_tokens INTEGER,
    total_tokens  INTEGER,
    latency_ms    REAL,
    ttft_ms       REAL,                 -- null when the stream produced no text
    PRIMARY KEY (session_id, turn)
);

-- Full board after a turn resolved, as GameState JSON. Boards are a few
-- kilobytes at most, so storing the whole thing per turn costs nothing and
-- removes any dependence on replaying events correctly to read state back.
CREATE TABLE IF NOT EXISTS board_snapshots (
    session_id  TEXT    NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    turn        INTEGER NOT NULL,
    state       TEXT    NOT NULL,       -- JSON
    recorded_at TEXT    NOT NULL,
    PRIMARY KEY (session_id, turn)
);

-- The individual changes that a snapshot is the sum of, each with the narration
-- fragment that justified it.
--
-- `extractor` is the point of this table. Several extractors may read the same
-- turn and disagree, and all their readings live here side by side under their
-- own names ('regex@1', a model, 'human' for hand-labelled ground truth). That
-- is what makes one readable against another instead of eyeballed. It also
-- means (session_id, turn) is deliberately not unique here.
--
-- No foreign key to `turns`: board state can be recorded live, before the
-- transcript it belongs to has ever been ingested.
CREATE TABLE IF NOT EXISTS state_events (
    id         INTEGER PRIMARY KEY,
    session_id TEXT    NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    turn       INTEGER NOT NULL,
    seq        INTEGER NOT NULL,        -- order within the turn, as narrated
    token_id   TEXT    NOT NULL,
    kind       TEXT    NOT NULL,        -- 'hp' | 'move' | 'status' | ...
    delta      INTEGER,                 -- signed; hp changes and the like
    detail     TEXT,                    -- JSON for changes a number can't carry
    phrase     TEXT,                    -- the fragment that justified it
    extractor  TEXT    NOT NULL,        -- provenance; see above
    recorded_at TEXT   NOT NULL,
    UNIQUE (session_id, turn, extractor, seq)
);

CREATE INDEX IF NOT EXISTS idx_state_events_turn
    ON state_events (session_id, turn);

CREATE INDEX IF NOT EXISTS idx_state_events_extractor
    ON state_events (extractor);
