from __future__ import annotations

import threading

import pytest

from app.db import Database

INSERT = (
    "INSERT INTO app_passwords (username, label, secret_hash, created_at)"
    " VALUES (?, ?, 'hash', 'now')"
)


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "concurrency.db"))
    yield database
    database.close()


def run_threads(target, count: int) -> list[BaseException]:
    errors: list[BaseException] = []

    def wrapper(n: int) -> None:
        try:
            target(n)
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=wrapper, args=(n,)) for n in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a cursor() block deadlocked"
    return errors


def count_rows(db: Database) -> int:
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM app_passwords")
        return cur.fetchone()["n"]


def test_concurrent_writers_keep_every_row(db):
    """The connection is shared, so transactions must not interleave."""

    def insert_many(n: int) -> None:
        for i in range(50):
            with db.cursor() as cur:
                cur.execute(INSERT, (f"user{n}", f"client{i}"))

    assert run_threads(insert_many, 8) == []
    assert count_rows(db) == 8 * 50


def test_a_failing_transaction_only_rolls_back_its_own_work(db):
    """One thread's rollback must not discard another's committed insert."""

    def insert_or_fail(n: int) -> None:
        for i in range(50):
            if i % 2:
                with pytest.raises(RuntimeError), db.cursor() as cur:
                    cur.execute(INSERT, (f"user{n}", f"doomed{i}"))
                    raise RuntimeError("boom")
            else:
                with db.cursor() as cur:
                    cur.execute(INSERT, (f"user{n}", f"kept{i}"))

    assert run_threads(insert_or_fail, 8) == []
    assert count_rows(db) == 8 * 25
