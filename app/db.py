"""Tiny SQLite layer. One table, no ORM, no migration framework."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_passwords (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    secret_hash  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_app_passwords_username
    ON app_passwords (username, revoked_at);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        # A single shared connection keeps :memory: databases usable in tests and
        # is plenty for a community-sized deployment. SQLite serialises writes.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        """Run a transaction. Serialised: the connection carries only one.

        The event loop and the worker threads that verify app passwords share
        this connection, and a connection has a single implicit transaction. Two
        callers interleaving inside it means one's ``commit()`` lands the other's
        half-finished statements, and one's ``rollback()`` discards them. The
        lock keeps each block alone in its transaction.

        It is not reentrant, deliberately: nesting ``cursor()`` blocks would
        deadlock rather than quietly commit an outer caller's partial work. Keep
        the body short and do slow work (argon2) outside it.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def close(self) -> None:
        self._conn.close()
