"""Numbered SQL files, applied in order, recorded as they go.

Idempotent so it is safe on every start; ordered by number so a tenth migration does not
sort between the first and the second; and transactional so a broken one leaves the
schema exactly as it was rather than half-changed with nothing to say so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from settings_api.storage.database import Database
from settings_api.storage.migrator import MIGRATIONS_DIR, Migration, discover, migrate
from settings_api.storage.times import from_column
from tests.fakes.clock import EPOCH


def fresh(tmp_path: Path) -> Database:
    return Database(tmp_path / "m.db")


async def versions(database: Database) -> list[int]:
    rows = await database.fetch_all("SELECT version FROM schema_version ORDER BY version")
    return [row["version"] for row in rows]


class TestApplying:
    async def test_a_fresh_database_gets_every_migration(self, tmp_path: Path) -> None:
        db = fresh(tmp_path)
        try:
            applied = migrate(db, now=EPOCH)
            assert applied == len(discover())
            assert await versions(db) == [migration.version for migration in discover()]
        finally:
            await db.aclose()

    async def test_running_again_applies_nothing(self, tmp_path: Path) -> None:
        db = fresh(tmp_path)
        try:
            migrate(db, now=EPOCH)
            assert migrate(db, now=EPOCH) == 0
        finally:
            await db.aclose()

    async def test_applied_at_is_the_injected_clock_and_round_trips(self, tmp_path: Path) -> None:
        db = fresh(tmp_path)
        try:
            migrate(db, now=EPOCH)
            row = await db.fetch_one("SELECT applied_at FROM schema_version WHERE version = 1")
            assert row is not None
            assert from_column(row["applied_at"]) == EPOCH
        finally:
            await db.aclose()


class TestOrdering:
    def test_migrations_are_ordered_by_number_not_by_name(self, tmp_path: Path) -> None:
        # As strings, "0010_" sorts before "0002_"; as numbers it does not.
        (tmp_path / "0010_tenth.sql").write_text("CREATE TABLE tenth (x INTEGER);")
        (tmp_path / "0002_second.sql").write_text("CREATE TABLE second (x INTEGER);")
        (tmp_path / "0001_first.sql").write_text("CREATE TABLE first (x INTEGER);")

        found = discover(tmp_path)
        assert [migration.version for migration in found] == [1, 2, 10]
        assert [migration.name for migration in found] == [
            "0001_first.sql",
            "0002_second.sql",
            "0010_tenth.sql",
        ]

    def test_a_migration_knows_its_version_and_name(self) -> None:
        migration = Migration(MIGRATIONS_DIR / "0001_initial.sql")
        assert migration.version == 1
        assert migration.name == "0001_initial.sql"


class TestABrokenMigration:
    async def test_it_leaves_the_schema_and_the_version_table_untouched(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / "migrations"
        directory.mkdir()
        (directory / "0001_good.sql").write_text("CREATE TABLE good (x INTEGER);")
        (directory / "0002_broken.sql").write_text(
            "CREATE TABLE half (x INTEGER);\nTHIS IS NOT SQL;"
        )
        db = fresh(tmp_path)
        try:
            with pytest.raises(Exception, match="syntax error"):
                migrate(db, now=EPOCH, directory=directory)

            # The good one committed, the broken one rolled back entirely: no `half`
            # table, and no version row claiming it was applied.
            tables = {
                row["name"]
                for row in await db.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert "good" in tables
            assert "half" not in tables
            assert await versions(db) == [1]
        finally:
            await db.aclose()

    async def test_it_does_not_leave_a_transaction_open(self, tmp_path: Path) -> None:
        directory = tmp_path / "migrations"
        directory.mkdir()
        (directory / "0001_broken.sql").write_text("NOT SQL;")
        db = fresh(tmp_path)
        try:
            with pytest.raises(Exception, match="syntax error"):
                migrate(db, now=EPOCH, directory=directory)
            # A script that fails partway leaves its transaction OPEN unless somebody rolls
            # it back. If this write raised "cannot start a transaction within a
            # transaction", the rollback in _apply was missing.
            (directory / "0001_broken.sql").write_text("CREATE TABLE fixed (x INTEGER);")
            assert migrate(db, now=EPOCH, directory=directory) == 1
        finally:
            await db.aclose()
