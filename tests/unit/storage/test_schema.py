"""The checked-in schema snapshot matches the migrations, and makes the promises it claims.

The snapshot exists so a migration's effect is reviewable as a diff. A stale one is
invisible in review -- nobody diffs prose against DDL -- so this test regenerates it and
compares, and CI fails on a difference.

The structural assertions are invariant 6 checked against the file rather than against a
comment: no profile column anywhere, and the primary key is exactly the three columns
that make a per-profile value unrepresentable.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from scripts.dump_schema import SNAPSHOT, dump  # noqa: E402 -- after the path is set


def test_the_snapshot_matches_what_the_migrations_produce() -> None:
    generated = asyncio.run(dump())
    assert generated == SNAPSHOT.read_text(), (
        "storage/schema.sql is out of step with the migrations: run `make schema` and "
        "commit the diff alongside the migration that changed it"
    )


class TestThePromisesTheSchemaMakes:
    snapshot = SNAPSHOT.read_text()

    def test_there_is_no_profile_column_anywhere(self) -> None:
        # ADR-0002, checked against the DDL. A per-profile setting is not discouraged; it
        # is unrepresentable.
        columns = re.findall(r"^\s+([a-z_]+)\s+(?:TEXT|INTEGER)", self.snapshot, re.MULTILINE)
        assert "profile" not in columns
        assert "profile_id" not in columns

    def test_the_settings_primary_key_is_account_namespace_key(self) -> None:
        assert "PRIMARY KEY (account_id, namespace, key)" in self.snapshot

    def test_settings_cascade_from_accounts(self) -> None:
        assert "REFERENCES accounts (account_id) ON DELETE CASCADE" in self.snapshot

    def test_events_have_no_foreign_key_to_accounts(self) -> None:
        events = self.snapshot[self.snapshot.index("CREATE TABLE settings_events") :]
        events = events[: events.index("STRICT")]
        # An event has to outlive the thing it describes.
        assert "REFERENCES" not in events

    def test_the_revision_is_constrained_non_negative(self) -> None:
        assert "CHECK (revision >= 0)" in self.snapshot

    def test_every_table_is_strict(self) -> None:
        # sqlite_sequence is SQLite's own AUTOINCREMENT bookkeeping and is not ours to
        # declare STRICT.
        tables = [
            name
            for name in re.findall(r"CREATE TABLE (\w+)", self.snapshot)
            if name != "sqlite_sequence"
        ]
        assert set(tables) == {"accounts", "schema_version", "settings", "settings_events"}
        assert self.snapshot.count(") STRICT") == len(tables)
