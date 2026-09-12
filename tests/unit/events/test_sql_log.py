"""The change log, as rows: appended in the caller's transaction, capped in the same one.

Ordered and paged by ``sequence`` and never by ``at``: the clock is injected, so two
events written in one tick carry the same stamp, and a page boundary falling between them
would drop one or return it twice -- intermittently, and only under the fixed clock the
tests use, which is the worst place to find out.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from settings_api.events.log import Action, Event, EventLog
from settings_api.events.sql_log import SqlEventLog
from settings_api.storage.database import Database
from tests.fakes.clock import EPOCH, FakeClock

ACCOUNT = "account-a"


async def append(
    database: Database,
    events: SqlEventLog,
    *,
    cap: int = 100,
    count: int = 1,
    account_id: str = ACCOUNT,
    clock: FakeClock | None = None,
    **fields: object,
) -> None:
    clock = clock or FakeClock()

    def work(connection: sqlite3.Connection) -> None:
        for index in range(count):
            events.append_in(
                connection,
                account_id=account_id,
                at=clock.now(),
                action=Action.SET,
                revision=index + 1,
                actor="settings",
                cap=cap,
                **fields,  # type: ignore[arg-type]
            )

    await database.transact(work)


class TestAppendAndRead:
    async def test_an_event_reads_back_with_every_column(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(
            database,
            events,
            namespace="spotify",
            key="default_market",
            service="spotify-api",
            detail={"changed": ["spotify.default_market"], "count": 1},
        )
        (event,) = await events.read(ACCOUNT, limit=10)
        assert event == Event(
            sequence=1,
            account_id=ACCOUNT,
            at=EPOCH,
            action=Action.SET,
            revision=1,
            actor="settings",
            namespace="spotify",
            key="default_market",
            service="spotify-api",
            detail={"changed": ["spotify.default_market"], "count": 1},
        )

    async def test_absent_optionals_read_back_as_none(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(database, events)
        (event,) = await events.read(ACCOUNT, limit=10)
        assert (event.namespace, event.key, event.service, event.detail) == (None, None, None, None)

    async def test_the_repr_hides_the_detail(self, database: Database, events: SqlEventLog) -> None:
        # detail is the one field shaped like request content, and a repr ends up in an
        # exception's context when something upstream logs the object it was working on.
        await append(database, events, detail={"changed": ["x.y"], "count": 1})
        (event,) = await events.read(ACCOUNT, limit=10)
        assert "changed" not in repr(event)

    async def test_count_for_account(self, database: Database, events: SqlEventLog) -> None:
        await append(database, events, count=3)
        await append(database, events, count=2, account_id="other")
        assert await events.count_for_account(ACCOUNT) == 3
        assert await events.count_for_account("nobody") == 0


class TestOrdering:
    async def test_newest_first_by_sequence_even_at_one_instant(
        self, database: Database, events: SqlEventLog
    ) -> None:
        # Every one of these carries the same timestamp. Ordered by `at`, their relative
        # order would be whatever SQLite happened to choose.
        await append(database, events, count=5)
        page = await events.read(ACCOUNT, limit=10)
        assert [event.sequence for event in page] == [5, 4, 3, 2, 1]
        assert len({event.at for event in page}) == 1

    async def test_before_is_exclusive(self, database: Database, events: SqlEventLog) -> None:
        await append(database, events, count=5)
        page = await events.read(ACCOUNT, limit=2, before_sequence=4)
        assert [event.sequence for event in page] == [3, 2]

    async def test_paging_walks_the_whole_log_without_gaps_or_repeats(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(database, events, count=7)
        seen: list[int] = []
        before: int | None = None
        while True:
            page = await events.read(ACCOUNT, limit=3, before_sequence=before)
            if not page:
                break
            seen.extend(event.sequence for event in page)
            before = page[-1].sequence
        assert seen == [7, 6, 5, 4, 3, 2, 1]

    async def test_accounts_do_not_see_each_others_events(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(database, events, count=2)
        await append(database, events, count=2, account_id="other")
        assert all(event.account_id == ACCOUNT for event in await events.read(ACCOUNT, limit=10))


class TestTheCap:
    async def test_the_log_is_trimmed_to_the_cap_in_the_appending_transaction(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(database, events, cap=5, count=9)
        page = await events.read(ACCOUNT, limit=100)
        # Exactly the cap survive, and they are the newest.
        assert [event.sequence for event in page] == [9, 8, 7, 6, 5]

    async def test_under_the_cap_nothing_is_deleted(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(database, events, cap=5, count=3)
        assert await events.count_for_account(ACCOUNT) == 3

    async def test_the_cap_is_per_account(self, database: Database, events: SqlEventLog) -> None:
        await append(database, events, cap=2, count=3)
        await append(database, events, cap=2, count=3, account_id="other")
        assert await events.count_for_account(ACCOUNT) == 2
        assert await events.count_for_account("other") == 2


class TestDeletion:
    async def test_delete_for_account_returns_the_count_and_leaves_others(
        self, database: Database, events: SqlEventLog
    ) -> None:
        await append(database, events, count=3)
        await append(database, events, count=1, account_id="other")

        def work(connection: sqlite3.Connection) -> int:
            return events.delete_for_account_in(connection, account_id=ACCOUNT)

        assert await database.transact(work) == 3
        assert await events.count_for_account(ACCOUNT) == 0
        assert await events.count_for_account("other") == 1

    async def test_an_event_outlives_the_setting_row_it_describes(
        self, database: Database, events: SqlEventLog, clock: FakeClock
    ) -> None:
        # No foreign key, on purpose: the event saying a setting was reset is the whole
        # record that it ever existed.
        await database.execute(
            "INSERT INTO accounts (account_id, revision, created_at, updated_at) "
            "VALUES (?, 1, 't', 't')",
            (ACCOUNT,),
        )
        await database.execute(
            "INSERT INTO settings (account_id, namespace, key, value_json, set_at, set_by) "
            "VALUES (?, 'spotify', 'default_market', '\"GB\"', 't', 'settings')",
            (ACCOUNT,),
        )
        await append(database, events, namespace="spotify", key="default_market")
        await database.execute("DELETE FROM accounts WHERE account_id = ?", (ACCOUNT,))
        assert await database.count("SELECT count(*) AS total FROM settings") == 0
        assert await events.count_for_account(ACCOUNT) == 1
        clock.advance(timedelta(days=1))


class TestTheShape:
    def test_the_adapter_satisfies_the_port(self, events: SqlEventLog) -> None:
        assert isinstance(events, EventLog)

    def test_every_action_has_a_stable_wire_value(self) -> None:
        assert {action.value for action in Action} == {
            "set",
            "reset",
            "reset_namespace",
            "update",
            "import",
            "forget",
            "purge_retired",
        }
