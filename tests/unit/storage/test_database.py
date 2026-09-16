"""One SQLite connection, one thread, and the pragmas read back rather than trusted.

The pragma that lies is ``foreign_keys``: it is per-connection, off by default, and a
silent no-op inside an open transaction. Here that would be an erasure that reports
success and leaves every settings row behind, which is why :func:`require_foreign_keys`
reads it back and why the cascade is proven with real rows rather than assumed.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from settings_api.storage.database import (
    DATABASE_FILE_MODE,
    Database,
    StorageError,
    make_private,
    require_foreign_keys,
)
from settings_api.storage.migrator import migrate
from tests.fakes.clock import EPOCH
from tests.support.filemode import assert_mode


class TestThePragmasAreInForce:
    async def test_the_journal_is_wal(self, database: Database) -> None:
        row = await database.fetch_one("PRAGMA journal_mode")
        assert row is not None
        assert row[0] == "wal"

    async def test_foreign_keys_are_actually_on(self, database: Database) -> None:
        row = await database.fetch_one("PRAGMA foreign_keys")
        assert row is not None
        assert row[0] == 1

    async def test_the_cascade_really_cascades(self, database: Database) -> None:
        # Not "the pragma reports on": rows. A foreign_keys pragma that succeeded and did
        # nothing would leave this settings row behind, and this is the test that would
        # notice.
        await database.execute(
            "INSERT INTO accounts (account_id, revision, created_at, updated_at) "
            "VALUES ('a', 0, 't', 't')"
        )
        await database.execute(
            "INSERT INTO settings (account_id, namespace, key, value_json, set_at, set_by) "
            "VALUES ('a', 'spotify', 'default_market', '\"GB\"', 't', 'settings')"
        )
        await database.execute("DELETE FROM accounts WHERE account_id = 'a'")
        assert await database.count("SELECT count(*) AS total FROM settings") == 0

    async def test_secure_delete_and_synchronous_are_set(self, database: Database) -> None:
        secure = await database.fetch_one("PRAGMA secure_delete")
        synchronous = await database.fetch_one("PRAGMA synchronous")
        assert secure is not None
        assert secure[0] == 1
        assert synchronous is not None
        assert synchronous[0] == 1  # NORMAL


class TestRequireForeignKeys:
    def test_a_connection_with_them_off_is_refused(self, tmp_path: Path) -> None:
        connection = sqlite3.connect(tmp_path / "bare.db")
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            with pytest.raises(StorageError, match="foreign keys are not enabled"):
                require_foreign_keys(connection)
        finally:
            connection.close()

    def test_a_connection_with_them_on_passes(self, tmp_path: Path) -> None:
        connection = sqlite3.connect(tmp_path / "bare.db")
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            require_foreign_keys(connection)
        finally:
            connection.close()


class TestTheFileMode:
    async def test_a_fresh_database_is_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "fresh.db"
        db = Database(path)
        try:
            assert_mode(path, DATABASE_FILE_MODE)
        finally:
            await db.aclose()

    async def test_the_mode_is_reapplied_on_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "again.db"
        first = Database(path)
        await first.aclose()
        path.chmod(0o644)  # somebody loosened it between runs

        second = Database(path)
        try:
            assert_mode(path, DATABASE_FILE_MODE)
        finally:
            await second.aclose()

    async def test_the_wal_sidecar_is_owner_only_too(self, tmp_path: Path) -> None:
        # The -wal holds the same data the file does, and the erasure tests read it.
        path = tmp_path / "wal.db"
        db = Database(path)
        try:
            migrate(db, now=EPOCH)
            await db.execute(
                "INSERT INTO accounts (account_id, revision, created_at, updated_at) "
                "VALUES ('a', 0, 't', 't')"
            )
            wal = path.with_name(path.name + "-wal")
            assert wal.exists()
            make_private(path)
            assert_mode(wal, DATABASE_FILE_MODE)
        finally:
            await db.aclose()

    def test_make_private_skips_a_sidecar_that_does_not_exist(self, tmp_path: Path) -> None:
        path = tmp_path / "lonely.db"
        path.write_bytes(b"")
        make_private(path)
        assert_mode(path, DATABASE_FILE_MODE)
        assert not path.with_name(path.name + "-wal").exists()

    def test_the_parent_directory_is_created_owner_only(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "er" / "settings.db"
        db = Database(nested)
        try:
            assert nested.exists()
            assert_mode(nested.parent, 0o700)
        finally:
            asyncio.run(db.aclose())


class TestRunningWork:
    async def test_run_sync_and_run_see_the_same_connection(self, database: Database) -> None:
        database.run_sync(lambda connection: connection.execute("CREATE TABLE t (x INTEGER)"))
        await database.execute("INSERT INTO t (x) VALUES (1)")
        assert await database.count("SELECT count(*) AS total FROM t") == 1

    async def test_fetch_one_returns_none_for_no_rows(self, database: Database) -> None:
        assert await database.fetch_one("SELECT 1 WHERE 0") is None

    async def test_fetch_all_returns_every_row(self, database: Database) -> None:
        database.run_sync(lambda connection: connection.execute("CREATE TABLE t (x INTEGER)"))
        for value in (1, 2, 3):
            await database.execute("INSERT INTO t (x) VALUES (?)", (value,))
        rows = await database.fetch_all("SELECT x FROM t ORDER BY x")
        assert [row["x"] for row in rows] == [1, 2, 3]

    async def test_execute_reports_rows_affected(self, database: Database) -> None:
        database.run_sync(lambda connection: connection.execute("CREATE TABLE t (x INTEGER)"))
        await database.execute("INSERT INTO t (x) VALUES (1), (2)")
        assert await database.execute("DELETE FROM t") == 2

    async def test_the_path_is_reported(self, tmp_path: Path, database: Database) -> None:
        assert database.path == tmp_path / "settings.db"


class TestTransactions:
    async def test_a_failing_transaction_rolls_back_everything_it_did(
        self, database: Database
    ) -> None:
        database.run_sync(lambda connection: connection.execute("CREATE TABLE t (x INTEGER)"))

        def work(connection: sqlite3.Connection) -> None:
            connection.execute("INSERT INTO t (x) VALUES (1)")
            msg = "refused halfway"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="refused halfway"):
            await database.transact(work)
        assert await database.count("SELECT count(*) AS total FROM t") == 0

    async def test_a_failing_transaction_does_not_hold_the_lock(self, database: Database) -> None:
        database.run_sync(lambda connection: connection.execute("CREATE TABLE t (x INTEGER)"))

        def work(connection: sqlite3.Connection) -> None:
            connection.execute("INSERT INTO t (x) VALUES (1)")
            msg = "no"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="no"):
            await database.transact(work)
        # A rollback that left the transaction open would make this raise "database is
        # locked" or "cannot start a transaction within a transaction".
        await database.execute("INSERT INTO t (x) VALUES (2)")
        assert await database.count("SELECT count(*) AS total FROM t") == 1

    async def test_concurrent_writes_are_serialised_with_no_lost_update(
        self, database: Database
    ) -> None:
        database.run_sync(lambda connection: connection.execute("CREATE TABLE counter (n INTEGER)"))
        await database.execute("INSERT INTO counter (n) VALUES (0)")

        def increment(connection: sqlite3.Connection) -> None:
            (current,) = connection.execute("SELECT n FROM counter").fetchone()
            connection.execute("UPDATE counter SET n = ?", (current + 1,))

        # Read-then-write is the classic lost update. It cannot happen here because every
        # transaction is one callable on the one thread that may touch the connection.
        await asyncio.gather(*(database.transact(increment) for _ in range(50)))
        row = await database.fetch_one("SELECT n FROM counter")
        assert row is not None
        assert row["n"] == 50


class TestCheckpoint:
    async def test_a_truncating_checkpoint_empties_the_wal(self, database: Database) -> None:
        await database.execute(
            "INSERT INTO accounts (account_id, revision, created_at, updated_at) "
            "VALUES ('a', 0, 't', 't')"
        )
        wal = database.path.with_name(database.path.name + "-wal")
        assert wal.stat().st_size > 0

        await database.checkpoint_truncate()

        # TRUNCATE is the only checkpoint mode that then empties the file rather than
        # leaving its pages to be overwritten eventually.
        assert wal.stat().st_size == 0


class TestClosing:
    async def test_close_is_safe_to_call_twice(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "close.db")
        await db.aclose()
        await db.aclose()

    async def test_nothing_runs_after_close(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "closed.db")
        await db.aclose()
        # The executor is shut down, so nothing can even be queued -- which is the stronger
        # guarantee: no thread exists that could touch the closed connection.
        with pytest.raises(RuntimeError, match="after shutdown"):
            db.run_sync(lambda connection: connection.execute("SELECT 1"))
