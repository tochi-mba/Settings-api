"""Erasure removes the bytes from the file, not just the row from the table.

This is the one test here that a unit test cannot replace, because it is about the file
rather than about the code. It scans the bytes of the database and its ``-wal`` for a
sentinel before and after ``DELETE /v1/settings``. It is written this way on purpose, so
that it would survive a change of mechanism to ``VACUUM`` without being rewritten.

The second test proves the choice of ``TRUNCATE`` is load-bearing: the same deletion
followed by a ``FULL`` checkpoint leaves the sentinel in the log.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI
from httpx import AsyncClient

from settings_api.domain.secrets import looks_like_a_credential
from settings_api.storage.database import Database
from settings_api.storage.migrator import migrate
from tests.conftest import auth, container_of, set_setting, token
from tests.fakes.clock import EPOCH

SENTINEL = "Pacific/Chatham"
"""A real IANA zone nobody sets by accident: legal for common.timezone, and distinctive
enough that finding it in a binary file is not a coincidence."""


def bytes_of(path: Path) -> bytes:
    data = path.read_bytes()
    wal = path.with_name(path.name + "-wal")
    if wal.exists():
        data += wal.read_bytes()
    return data


def test_the_sentinel_is_a_value_the_service_accepts() -> None:
    # If a tuning change made the detector refuse this, the test below would fail with a
    # 422 that reads like the behaviour under test failing. This is what points at the
    # real cause.
    assert looks_like_a_credential(SENTINEL) is None


async def test_forget_removes_the_value_from_the_database_and_its_wal(
    client: AsyncClient, app: FastAPI
) -> None:
    path = container_of(app).database.path
    owner = token()

    await set_setting(client, owner, "common", "timezone", SENTINEL)
    assert SENTINEL.encode() in bytes_of(path), (
        "the sentinel must be in the file for this test to prove anything"
    )

    response = await client.delete("/v1/settings", headers=auth(owner))
    assert response.status_code == 200

    assert SENTINEL.encode() not in bytes_of(path)
    assert (await client.get("/v1/settings/events", headers=auth(owner))).json()["events"] == []
    assert (await client.get("/v1/settings", headers=auth(owner))).headers[
        "ETag"
    ] == '"account-a.0"'


async def test_a_full_checkpoint_would_not_have_been_enough(tmp_path: Path) -> None:
    # DELETE plus a FULL checkpoint, on a second database, by hand: the value survives in
    # the -wal. That is what makes TRUNCATE the choice rather than a preference.
    database = Database(tmp_path / "full.db")
    try:
        migrate(database, now=EPOCH)
        await database.execute(
            "INSERT INTO accounts (account_id, revision, created_at, updated_at) "
            "VALUES ('a', 1, 't', 't')"
        )
        await database.execute(
            "INSERT INTO settings (account_id, namespace, key, value_json, set_at, set_by) "
            "VALUES ('a', 'common', 'timezone', ?, 't', 'settings')",
            (f'"{SENTINEL}"',),
        )
        await database.execute("DELETE FROM settings WHERE account_id = 'a'")
        await database.run(lambda connection: connection.execute("PRAGMA wal_checkpoint(FULL)"))

        assert SENTINEL.encode() in bytes_of(database.path)

        await database.checkpoint_truncate()
        assert SENTINEL.encode() not in bytes_of(database.path)
    finally:
        await database.aclose()


async def test_a_bare_delete_is_not_close(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "bare.db", isolation_level=None)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE t (v TEXT)")
        connection.execute("INSERT INTO t VALUES (?)", (SENTINEL,))
        connection.execute("DELETE FROM t")
        assert SENTINEL.encode() in bytes_of(tmp_path / "bare.db")
    finally:
        connection.close()
