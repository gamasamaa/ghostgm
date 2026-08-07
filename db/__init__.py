"""GhostGM store: a queryable projection of the transcripts and the boards.

Imports nothing from `ghost`, `visual` or `prep`, and nothing imports this yet.
`ghost.py` stays runnable and phase 0 stays completable with this directory
deleted.
"""

from db.store import (
    DEFAULT_DB_PATH,
    SCHEMA_VERSION,
    SchemaTooNew,
    connect,
    migrate,
    open_db,
    schema_version,
    table_names,
    transaction,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "SCHEMA_VERSION",
    "SchemaTooNew",
    "connect",
    "migrate",
    "open_db",
    "schema_version",
    "table_names",
    "transaction",
]
