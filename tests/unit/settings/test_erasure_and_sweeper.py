"""Erasure finishes with a truncating checkpoint; the sweeper waits out a retention window.

The byte-scan proof that erasure actually removes the value from the file lives in
``tests/integration/test_erasure.py``, because it is about the file rather than the code.
This file pins the two components' own decisions: what they call, in what order, and --
for the sweeper -- the date arithmetic that decides when a retired key's rows finally go.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from settings_api.auth.tokens import Identity
from settings_api.domain.policy import NO_POLICY
from settings_api.domain.registry import NAMESPACES, definition_for
from settings_api.domain.types import SettingDef
from settings_api.events.log import Action
from settings_api.events.sql_log import SqlEventLog
from settings_api.settings import sweeper as sweeper_module
from settings_api.settings.erasure import Erasure
from settings_api.settings.service import SettingsService
from settings_api.settings.sql_store import SqlSettingsStore
from settings_api.settings.store import Change
from settings_api.settings.sweeper import RetiredSweeper
from settings_api.storage.database import Database
from tests.conftest import build_settings
from tests.fakes.clock import FakeClock

A = "account-a"
OWNER = Identity(account_id=A, audience="settings", namespaces=frozenset(NAMESPACES))
MARKET = definition_for("spotify", "default_market")


class TestErasure:
    async def test_forget_destroys_the_rows_and_returns_the_count(
        self, database: Database, store: SqlSettingsStore, events: SqlEventLog, tmp_path: Path
    ) -> None:
        clock = FakeClock()
        service = SettingsService(
            store=store,
            events=events,
            policy=NO_POLICY,
            clock=clock,
            config=build_settings(tmp_path),
        )
        await service.set_setting(OWNER, "spotify", "default_market", "GB")
        await service.set_setting(OWNER, "common", "timezone", "Europe/Lisbon")
        erasure = Erasure(database=database, service=service)

        removed = await erasure.forget(OWNER)

        assert removed == 2
        assert (await store.read(A)).exists is False
        assert await events.count_for_account(A) == 0
        # The checkpoint ran: the write-ahead log has been truncated to nothing.
        wal = database.path.with_name(database.path.name + "-wal")
        assert wal.stat().st_size == 0


def retired(days_ago: int, clock: FakeClock) -> SettingDef:
    """A copy of a real entry, retired ``days_ago`` days before the clock's today."""
    date = (clock.now() - timedelta(days=days_ago)).date().isoformat()
    return replace(MARKET, key="old_market", retired_at=date)


def sweeper(
    store: SqlSettingsStore, database: Database, clock: FakeClock, days: int = 90
) -> RetiredSweeper:
    return RetiredSweeper(
        store=store, database=database, clock=clock, retention_days=days, event_cap=100
    )


class TestWhatIsDue:
    def test_nothing_is_due_in_the_live_catalogue(
        self, store: SqlSettingsStore, database: Database
    ) -> None:
        assert sweeper(store, database, FakeClock()).due() == []

    def test_a_key_retired_yesterday_is_not_due_with_a_ninety_day_window(
        self, store: SqlSettingsStore, database: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr(sweeper_module, "every_entry", lambda: iter([retired(1, clock)]))
        assert sweeper(store, database, clock).due() == []

    def test_it_becomes_due_once_the_clock_passes_the_window(
        self, store: SqlSettingsStore, database: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        entry = retired(1, clock)
        monkeypatch.setattr(sweeper_module, "every_entry", lambda: iter([entry]))
        swept = sweeper(store, database, clock)
        clock.advance(timedelta(days=89))
        assert swept.due() == []
        clock.advance(timedelta(days=1))
        # Retired 91 days before today: strictly past the 90-day cutoff.
        assert swept.due() == [entry]

    def test_the_boundary_is_a_date_comparison_not_an_instant(
        self, store: SqlSettingsStore, database: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # retired_at carries no time of day. Treating it as midnight and subtracting would
        # make the window end at a different moment depending on when the process runs.
        # The fake clock starts at noon, so eleven hours later is still the same date and
        # one more hour is the next.
        clock = FakeClock()
        entry = retired(90, clock)  # exactly the window ago: not yet, whatever the hour
        monkeypatch.setattr(sweeper_module, "every_entry", lambda: iter([entry]))
        swept = sweeper(store, database, clock)
        clock.advance(timedelta(hours=11))
        assert swept.due() == []
        clock.advance(timedelta(hours=1))
        assert swept.due() == [entry]

    def test_a_zero_day_window_purges_the_day_after_retirement(
        self, store: SqlSettingsStore, database: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        entry = retired(1, clock)
        monkeypatch.setattr(sweeper_module, "every_entry", lambda: iter([entry]))
        assert sweeper(store, database, clock, days=0).due() == [entry]


class TestSweeping:
    async def test_nothing_due_means_nothing_removed_and_no_checkpoint(
        self, store: SqlSettingsStore, database: Database
    ) -> None:
        clock = FakeClock()
        await store.apply(
            A,
            changes=[Change(namespace="spotify", key="default_market", value="GB")],
            now=clock.now(),
            action=Action.SET,
            actor="settings",
            service=None,
            event_cap=100,
        )
        wal = database.path.with_name(database.path.name + "-wal")
        before = wal.stat().st_size
        assert await sweeper(store, database, clock).sweep_once() == 0
        # No checkpoint on an idle tick: a checkpoint every hour on a quiet service is a
        # write amplifier for nothing.
        assert wal.stat().st_size == before

    async def test_a_due_key_is_purged_across_accounts_and_the_wal_is_truncated(
        self,
        store: SqlSettingsStore,
        database: Database,
        events: SqlEventLog,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FakeClock()
        for account in (A, "account-b"):
            await store.apply(
                account,
                changes=[Change(namespace="spotify", key="old_market", value="GB")],
                now=clock.now(),
                action=Action.SET,
                actor="settings",
                service=None,
                event_cap=100,
            )
        entry = retired(1, clock)
        monkeypatch.setattr(sweeper_module, "every_entry", lambda: iter([entry]))
        clock.advance(timedelta(days=200))

        removed = await sweeper(store, database, clock).sweep_once()

        assert removed == 2
        assert "spotify.old_market" not in (await store.read(A)).rows
        assert (await events.read("account-b", limit=1))[0].action is Action.PURGE_RETIRED
        wal = database.path.with_name(database.path.name + "-wal")
        assert wal.stat().st_size == 0
