"""One account's choices as rows, with the revision that tracks them.

Everything interesting is inside one transaction on one thread: the revision is read,
compared against ``If-Match`` and bumped in the same callable, so "read then write" cannot
go stale; idempotency is decided against what is actually stored; and all-or-nothing is
free, because anything that raises rolls the whole callable back.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from settings_api.domain.errors import RevisionMismatchError
from settings_api.events.log import Action
from settings_api.events.sql_log import SqlEventLog
from settings_api.settings.sql_store import SqlSettingsStore
from settings_api.settings.store import AccountState, Applied, Change, SettingsStore, StoredSetting
from settings_api.storage.database import Database
from tests.fakes.clock import EPOCH, FakeClock

A = "account-a"
B = "account-b"


def market(value: object = "GB") -> Change:
    return Change(namespace="spotify", key="default_market", value=value, profile="personal")  # type: ignore[arg-type]


def zone(value: str = "Europe/Lisbon") -> Change:
    return Change(namespace="common", key="timezone", value=value)


async def apply(
    store: SqlSettingsStore,
    *changes: Change,
    account_id: str = A,
    clock: FakeClock | None = None,
    action: Action = Action.SET,
    if_revision: int | None = None,
    actor: str = "settings",
    service: str | None = None,
) -> Applied:
    return await store.apply(
        account_id,
        changes=list(changes),
        now=(clock or FakeClock()).now(),
        action=action,
        actor=actor,
        service=service,
        event_cap=100,
        if_revision=if_revision,
    )


class TestReadingNothing:
    async def test_an_unknown_account_reads_as_defaults_not_as_an_error(
        self, store: SqlSettingsStore
    ) -> None:
        state = await store.read("nobody")
        assert state == AccountState(exists=False, revision=0, rows={})
        assert state.etag("nobody") == '"nobody.0"'


class TestTheFirstWrite:
    async def test_creates_the_account_row_without_a_separate_create_step(
        self, store: SqlSettingsStore
    ) -> None:
        # user-api shipped without this and every first write failed a foreign key -- and
        # an assistant will never call an endpoint it was not told about.
        applied = await apply(store, market())
        assert applied == Applied(revision=1, changed=("spotify.default_market",))
        state = await store.read(A)
        assert state.exists is True
        assert state.revision == 1

    async def test_the_row_carries_provenance_and_the_injected_clock(
        self, store: SqlSettingsStore
    ) -> None:
        clock = FakeClock()
        await apply(
            store, market(), clock=clock, actor="service:spotify-api", service="spotify-api"
        )
        row = (await store.read(A)).rows[("personal", "spotify.default_market")]
        assert row == StoredSetting(
            namespace="spotify",
            key="default_market",
            value="GB",
            set_at=EPOCH,
            set_by="service:spotify-api",
            profile="personal",
        )
        assert row.qualified == "spotify.default_market"
        assert "GB" not in repr(row)


class TestIdempotency:
    async def test_an_identical_write_changes_nothing(
        self, store: SqlSettingsStore, events: SqlEventLog
    ) -> None:
        first = await apply(store, market())
        second = await apply(store, market())

        assert second.revision == first.revision == 1
        assert second.changed == ()
        assert second.any_change is False
        # ONE event for two writes. Two assistants that independently decide the same
        # thing produce one entry in the person's history rather than two.
        assert await events.count_for_account(A) == 1

    async def test_a_different_value_is_a_change(self, store: SqlSettingsStore) -> None:
        await apply(store, market("GB"))
        applied = await apply(store, market("PT"))
        assert applied.revision == 2
        assert applied.changed == ("spotify.default_market",)

    async def test_equality_is_by_value_not_by_spelling(self, store: SqlSettingsStore) -> None:
        providers = Change(namespace="search", key="disabled_providers", value=["a", "b"])
        await apply(store, providers)
        # The same list is the same decision however the caller's encoder ordered its JSON.
        again = await apply(
            store, Change(namespace="search", key="disabled_providers", value=["a", "b"])
        )
        assert again.any_change is False


class TestResets:
    async def test_a_reset_removes_the_row_and_counts_as_a_change(
        self, store: SqlSettingsStore
    ) -> None:
        await apply(store, market())
        applied = await apply(
            store,
            Change(namespace="spotify", key="default_market", reset=True, profile="personal"),
            action=Action.RESET,
        )
        assert applied.revision == 2
        assert applied.changed == ("spotify.default_market",)
        assert ("personal", "spotify.default_market") not in (await store.read(A)).rows

    async def test_resetting_something_never_set_is_a_silent_no_op(
        self, store: SqlSettingsStore, events: SqlEventLog
    ) -> None:
        applied = await apply(
            store, Change(namespace="spotify", key="default_market", reset=True, profile="personal")
        )
        assert applied.any_change is False
        assert await events.count_for_account(A) == 0
        # The account row was still created, which is harmless and simpler than a branch.
        assert (await store.read(A)).exists is True

    async def test_the_reset_event_outlives_the_row(
        self, store: SqlSettingsStore, events: SqlEventLog
    ) -> None:
        await apply(store, market())
        await apply(
            store,
            Change(namespace="spotify", key="default_market", reset=True, profile="personal"),
            action=Action.RESET,
        )
        page = await events.read(A, limit=10)
        assert [event.action for event in page] == [Action.RESET, Action.SET]


class TestDocuments:
    async def test_several_changes_are_one_revision_and_one_event(
        self, store: SqlSettingsStore, events: SqlEventLog
    ) -> None:
        applied = await apply(store, market(), zone(), action=Action.UPDATE)
        assert applied.revision == 1
        assert set(applied.changed) == {"spotify.default_market", "common.timezone"}
        (event,) = await events.read(A, limit=10)
        assert event.action is Action.UPDATE
        assert event.detail == {
            "changed": ["spotify.default_market", "common.timezone"],
            "count": 2,
        }

    async def test_an_event_spanning_two_namespaces_names_neither_in_its_columns(
        self, store: SqlSettingsStore, events: SqlEventLog
    ) -> None:
        # Naming the first of two would read as though only that one changed. The full
        # list is in detail; the columns serve the common one-setting case.
        await apply(store, market(), zone())
        (event,) = await events.read(A, limit=10)
        assert (event.namespace, event.key) == (None, None)

    async def test_an_event_within_one_namespace_names_it_but_not_a_key(
        self, store: SqlSettingsStore, events: SqlEventLog
    ) -> None:
        await apply(
            store,
            market(),
            Change(
                namespace="spotify",
                key="max_batch_size",
                value=10,
                profile="personal",
            ),
        )
        (event,) = await events.read(A, limit=10)
        assert (event.namespace, event.key) == ("spotify", None)

    async def test_a_single_change_names_both(
        self, store: SqlSettingsStore, events: SqlEventLog
    ) -> None:
        await apply(store, market())
        (event,) = await events.read(A, limit=10)
        assert (event.namespace, event.key) == ("spotify", "default_market")

    async def test_only_the_changes_that_changed_are_reported(
        self, store: SqlSettingsStore
    ) -> None:
        await apply(store, market())
        applied = await apply(store, market(), zone())
        assert applied.changed == ("common.timezone",)
        assert applied.revision == 2


class TestOptimisticConcurrency:
    async def test_a_matching_revision_proceeds(self, store: SqlSettingsStore) -> None:
        await apply(store, market())
        applied = await apply(store, zone(), if_revision=1)
        assert applied.revision == 2

    async def test_a_stale_revision_is_refused_and_writes_nothing(
        self, store: SqlSettingsStore
    ) -> None:
        await apply(store, market())
        with pytest.raises(RevisionMismatchError, match="at revision 1; the request expected 0"):
            await apply(store, zone(), if_revision=0)
        state = await store.read(A)
        assert state.revision == 1
        assert ("*", "common.timezone") not in state.rows

    async def test_revision_zero_matches_an_account_that_has_never_written(
        self, store: SqlSettingsStore
    ) -> None:
        applied = await apply(store, market(), if_revision=0)
        assert applied.revision == 1

    async def test_concurrent_writes_never_lose_an_update(self, store: SqlSettingsStore) -> None:
        # Fifty concurrent writers, each a distinct value: fifty bumps, no two sharing one.
        await asyncio.gather(
            *(
                apply(store, Change(namespace="user", key="grace_days", value=index))
                for index in range(50)
            )
        )
        assert (await store.read(A)).revision == 50


class TestIsolation:
    async def test_accounts_do_not_see_each_other(self, store: SqlSettingsStore) -> None:
        await apply(store, market("GB"), account_id=A)
        await apply(store, market("PT"), account_id=B)
        assert (await store.read(A)).rows[("personal", "spotify.default_market")].value == "GB"
        assert (await store.read(B)).rows[("personal", "spotify.default_market")].value == "PT"
        assert (await store.read(A)).revision == 1


class TestNullIsStored:
    async def test_a_stored_none_is_a_row(self, store: SqlSettingsStore) -> None:
        await apply(store, market(None))
        state = await store.read(A)
        # Membership, not None-ness, is what says a row exists.
        assert ("personal", "spotify.default_market") in state.values
        assert state.values[("personal", "spotify.default_market")] is None


class TestForget:
    async def test_everything_goes_and_the_count_is_returned(
        self, store: SqlSettingsStore, events: SqlEventLog, database: Database
    ) -> None:
        await apply(store, market(), zone())
        await apply(store, market("PT"))
        removed = await store.forget(A)
        assert removed == 2
        state = await store.read(A)
        assert state == AccountState(exists=False, revision=0, rows={})
        assert await events.count_for_account(A) == 0
        assert await database.count("SELECT count(*) AS total FROM accounts") == 0

    async def test_forgetting_leaves_other_accounts_alone(self, store: SqlSettingsStore) -> None:
        await apply(store, market(), account_id=A)
        await apply(store, market(), account_id=B)
        await store.forget(A)
        assert (await store.read(B)).exists is True

    async def test_forgetting_nobody_removes_nothing(self, store: SqlSettingsStore) -> None:
        assert await store.forget("nobody") == 0


class TestPurgeSetting:
    async def test_rows_go_across_every_account_with_one_event_each_and_no_bump(
        self, store: SqlSettingsStore, events: SqlEventLog
    ) -> None:
        clock = FakeClock()
        await apply(store, market(), zone(), account_id=A, clock=clock)  # revision 1
        await apply(store, market(), account_id=B, clock=clock)  # revision 1
        clock.advance(timedelta(days=100))

        removed = await store.purge_setting(
            "spotify", "default_market", now=clock.now(), event_cap=100
        )

        assert removed == 2
        for account in (A, B):
            state = await store.read(account)
            assert ("personal", "spotify.default_market") not in state.rows
            # Recorded WITHOUT bumping: those rows were already ignored on every read, so
            # no resolved value changed and invalidating every cache would be a lie.
            assert state.revision == 1
            newest = (await events.read(account, limit=1))[0]
            assert newest.action is Action.PURGE_RETIRED
            assert newest.revision == 1
            assert newest.actor == "sweeper"
            assert (newest.namespace, newest.key) == ("spotify", "default_market")
        assert ("*", "common.timezone") in (await store.read(A)).rows

    async def test_nothing_to_purge_is_zero_and_no_events(
        self, store: SqlSettingsStore, events: SqlEventLog
    ) -> None:
        await apply(store, zone())
        assert await store.purge_setting("spotify", "default_market", now=EPOCH, event_cap=100) == 0
        assert await events.count_for_account(A) == 1


class TestTheShape:
    def test_the_adapter_satisfies_the_port(self, store: SqlSettingsStore) -> None:
        assert isinstance(store, SettingsStore)

    def test_change_qualified(self) -> None:
        assert market().qualified == "spotify.default_market"
        assert "GB" not in repr(market())

    def test_state_values_reads_through_to_rows(self) -> None:
        row = StoredSetting(namespace="a", key="b", value=1, set_at=EPOCH, set_by="x")
        assert AccountState(exists=True, revision=3, rows={("*", "a.b"): row}).values == {
            ("*", "a.b"): 1
        }
