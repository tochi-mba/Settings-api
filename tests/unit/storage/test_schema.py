"""The checked-in schema snapshot matches the migrations, and makes the promises it claims.

The snapshot exists so a migration's effect is reviewable as a diff. A stale one is
invisible in review -- nobody diffs prose against DDL -- so this test regenerates it and
compares, and CI fails on a difference.

The structural assertions are invariant 6 checked against the file rather than against a
comment: the profile column is the exclusive-scope sentinel, and the primary key is
``(account_id, profile, namespace, key)``.
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
    assert generated == SNAPSHOT.read_text(encoding="utf-8"), (
        "storage/schema.sql is out of step with the migrations: run `make schema` and "
        "commit the diff alongside the migration that changed it"
    )


class TestThePromisesTheSchemaMakes:
    snapshot = SNAPSHOT.read_text(encoding="utf-8")

    def test_the_settings_primary_key_includes_the_profile_column(self) -> None:
        assert "PRIMARY KEY (account_id, profile, namespace, key)" in self.snapshot
        columns = re.findall(r"^\s+([a-z_]+)\s+(?:TEXT|INTEGER)", self.snapshot, re.MULTILINE)
        assert "profile" in columns

    def test_the_account_sentinel_cannot_collide_with_a_keyring_name(self) -> None:
        # `*` is reserved for account-scoped rows. keyring's profile-name pattern refuses
        # it, so a stored account row can never be confused with a real profile.
        assert "CHECK (length(profile) BETWEEN 1 AND 64)" in self.snapshot

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
        # SQLite quotes identifiers that collide with keywords; `settings` comes out as
        # `"settings"` in the dump. The name is still the table's, quotes or not.
        tables = [
            name
            for name in re.findall(r'CREATE TABLE "?(\w+)"?', self.snapshot)
            if name != "sqlite_sequence"
        ]
        assert set(tables) == {"accounts", "schema_version", "settings", "settings_events"}
        assert self.snapshot.count(") STRICT") == len(tables)
