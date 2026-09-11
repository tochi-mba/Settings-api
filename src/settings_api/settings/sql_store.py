"""One account's choices, as rows, with the revision that tracks them.

Every interesting property of this service lives in :meth:`SqlSettingsStore.apply`, and
all of them come from the same thing: the whole operation is one callable submitted to the
one thread that may touch the connection, so there is no point inside it at which another
caller can be interleaved.

**The revision cannot go stale.** It is read, compared against ``If-Match``, and bumped in
the same transaction. The version that reads it in one call and writes in another is the
classic lost update: two writers read 7, both write, and one of them believes it was at 8
when it was at 9.

**Idempotency is decided against what is actually stored**, inside that transaction, not
against what the caller last saw. A ``PUT`` of the same document twice changes something
once. Two assistants that independently decide the same thing produce one entry in the
person's history rather than two.

**All-or-nothing is free rather than arranged.** The changes are applied in a loop and
anything that raises rolls the whole transaction back, so a document write that refuses
its last key has not written its first.

**There is no profile column** (ADR-0002). The primary key is
``(account_id, namespace, key)``, so a per-profile value is unrepresentable rather than
discouraged. Nothing below takes a profile, because there is nowhere to put one.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from settings_api.domain.errors import RevisionMismatchError
from settings_api.events.log import Action
from settings_api.settings.store import AccountState, Applied, Change, StoredSetting
from settings_api.storage.times import from_column, to_column

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import datetime

    from settings_api.domain.types import Value
    from settings_api.events.log import EventLog
    from settings_api.storage.database import Database

SETTING_COLUMNS = "namespace, key, value_json, set_at, set_by"

UPSERT = (
    "INSERT INTO settings (account_id, namespace, key, value_json, set_at, set_by) "
    "VALUES (?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (account_id, namespace, key) DO UPDATE SET "
    "value_json = excluded.value_json, set_at = excluded.set_at, set_by = excluded.set_by"
)
"""One statement for "set it, whether or not it is already there".

An ``INSERT``-then-``UPDATE``-on-failure would work too and would be two statements whose
interleaving this design has already made impossible -- but it would also make the
row-exists case a branch nothing can reach from outside a transaction, which is the kind
of line the coverage gate ends up arguing about.
"""


class SqlSettingsStore:
    """The SQLite adapter for :class:`~settings_api.settings.store.SettingsStore`."""

    def __init__(self, *, database: Database, events: EventLog) -> None:
        self._db = database
        self._events = events

    async def read(self, account_id: str) -> AccountState:
        """Everything stored for one account, in one submitted callable.

        One callable rather than two awaits, so the revision and the rows are read from
        the same instant. Two separate reads could straddle a write and report a revision
        that does not describe the rows beside it -- which is the ETag promising a caller
        that a document it has not got is the one it is holding.
        """
        return await self._db.run(lambda connection: _read_state(connection, account_id))

    # Matching the port, one parameter per independent decision. See
    # :class:`settings_api.settings.store.SettingsStore`.
    async def apply(  # noqa: PLR0913
        self,
        account_id: str,
        *,
        changes: Sequence[Change],
        now: datetime,
        action: Action,
        actor: str,
        service: str | None,
        event_cap: int,
        if_revision: int | None = None,
    ) -> Applied:
        def work(connection: sqlite3.Connection) -> Applied:
            _ensure_account(connection, account_id, now=now)
            state = _read_state(connection, account_id)

            if if_revision is not None and if_revision != state.revision:
                msg = (
                    f"these settings are at revision {state.revision}; "
                    f"the request expected {if_revision}"
                )
                raise RevisionMismatchError(msg)

            changed = _apply_changes(
                connection, account_id, changes=changes, state=state, now=now, actor=actor
            )
            if not changed:
                # Nothing moved, so nothing is recorded and the revision is left where it
                # is. This is what makes an identical repeated PUT idempotent rather than
                # merely harmless, and there is a test that asserts one event for two
                # identical writes.
                return Applied(revision=state.revision, changed=())

            revision = _bump(connection, account_id, now=now)
            self._events.append_in(
                connection,
                account_id=account_id,
                at=now,
                action=action,
                revision=revision,
                actor=actor,
                service=service,
                cap=event_cap,
                namespace=_common_namespace(changed),
                key=_common_key(changed),
                # Names and a count. Never a value -- see settings_api.events.log.
                detail={"changed": list(changed), "count": len(changed)},
            )
            return Applied(revision=revision, changed=changed)

        return await self._db.transact(work)

    async def forget(self, account_id: str) -> int:
        """Delete the settings, the account row and the log, as one transaction.

        The settings rows would cascade from the account row on their own; they are
        deleted explicitly anyway, so that the count returned is a count of rows this
        service knows it removed rather than a count of rows it believes SQLite removed.
        The cascade is verified separately -- ``PRAGMA foreign_keys`` is a pragma that can
        succeed and do nothing, which is why
        :func:`settings_api.storage.database.require_foreign_keys` reads it back.
        """

        def work(connection: sqlite3.Connection) -> int:
            removed = connection.execute(
                "DELETE FROM settings WHERE account_id = ?", (account_id,)
            ).rowcount
            self._events.delete_for_account_in(connection, account_id=account_id)
            connection.execute("DELETE FROM accounts WHERE account_id = ?", (account_id,))
            return removed

        return await self._db.transact(work)

    async def purge_setting(
        self, namespace: str, key: str, *, now: datetime, event_cap: int
    ) -> int:
        """Destroy every account's rows for one retired key."""

        def work(connection: sqlite3.Connection) -> int:
            affected = [
                row["account_id"]
                for row in connection.execute(
                    "SELECT account_id, (SELECT revision FROM accounts a "
                    "WHERE a.account_id = s.account_id) AS revision "
                    "FROM settings s WHERE namespace = ? AND key = ?",
                    (namespace, key),
                ).fetchall()
            ]
            if not affected:
                return 0

            removed = connection.execute(
                "DELETE FROM settings WHERE namespace = ? AND key = ?", (namespace, key)
            ).rowcount
            for account_id in affected:
                self._events.append_in(
                    connection,
                    account_id=account_id,
                    at=now,
                    action=Action.PURGE_RETIRED,
                    revision=_revision_of(connection, account_id),
                    actor="sweeper",
                    service=None,
                    cap=event_cap,
                    namespace=namespace,
                    key=key,
                    detail={"changed": [f"{namespace}.{key}"], "count": 1},
                )
            return removed

        return await self._db.transact(work)


def _read_state(connection: sqlite3.Connection, account_id: str) -> AccountState:
    """The account row and every setting it has, from one instant."""
    account = connection.execute(
        "SELECT revision FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    rows = connection.execute(
        f"SELECT {SETTING_COLUMNS} FROM settings WHERE account_id = ?",  # noqa: S608
        (account_id,),
    ).fetchall()

    stored = {}
    for row in rows:
        setting = StoredSetting(
            namespace=row["namespace"],
            key=row["key"],
            value=_decode(row["value_json"]),
            set_at=from_column(row["set_at"]),
            set_by=row["set_by"],
        )
        stored[setting.qualified] = setting

    return AccountState(
        exists=account is not None,
        revision=0 if account is None else int(account["revision"]),
        rows=stored,
    )


def _ensure_account(connection: sqlite3.Connection, account_id: str, *, now: datetime) -> None:
    """Create the account row if this is the first write.

    ``INSERT OR IGNORE`` rather than a read followed by an insert: inside this transaction
    the read could not go stale, but the two-statement version has a branch that only the
    first write in an account's life can take, and one that only every subsequent write
    can -- which is two paths to cover for a statement SQLite already expresses.
    """
    stamp = to_column(now)
    connection.execute(
        "INSERT OR IGNORE INTO accounts (account_id, revision, created_at, updated_at) "
        "VALUES (?, 0, ?, ?)",
        (account_id, stamp, stamp),
    )


# Six parameters, all independent: where to write, what to write, what is already
# there, when, and on whose authority. An options object would be constructed on one line
# and unpacked on the next.
def _apply_changes(  # noqa: PLR0913
    connection: sqlite3.Connection,
    account_id: str,
    *,
    changes: Sequence[Change],
    state: AccountState,
    now: datetime,
    actor: str,
) -> tuple[str, ...]:
    """Write every change that is actually a change, and report which those were."""
    stamp = to_column(now)
    changed: list[str] = []

    for change in changes:
        existing = state.rows.get(change.qualified)
        if change.reset:
            if existing is None:
                # Resetting something that was never set is a no-op, not an error. A
                # caller clearing a namespace does not want to know which of its keys
                # happened to have rows.
                continue
            connection.execute(
                "DELETE FROM settings WHERE account_id = ? AND namespace = ? AND key = ?",
                (account_id, change.namespace, change.key),
            )
            changed.append(change.qualified)
            continue

        if existing is not None and existing.value == change.value:
            # Deliberately compared as values rather than as encoded JSON. `True` and
            # `true` are the same decision however they were spelled on the way in, and a
            # caller whose encoder orders a list differently has not changed anything.
            continue

        connection.execute(
            UPSERT,
            (
                account_id,
                change.namespace,
                change.key,
                json.dumps(change.value, ensure_ascii=False, allow_nan=False, sort_keys=True),
                stamp,
                actor,
            ),
        )
        changed.append(change.qualified)

    return tuple(changed)


def _bump(connection: sqlite3.Connection, account_id: str, *, now: datetime) -> int:
    """Move the revision on by one and return what it became.

    ``RETURNING`` so the new value comes back from the statement that set it. Reading it
    afterwards would be a second statement inside the same transaction -- correct here,
    and one more thing that stops being correct if anybody ever moves this out of one.
    """
    row = connection.execute(
        "UPDATE accounts SET revision = revision + 1, updated_at = ? "
        "WHERE account_id = ? RETURNING revision",
        (to_column(now), account_id),
    ).fetchone()
    return int(row["revision"])


def _revision_of(connection: sqlite3.Connection, account_id: str) -> int:
    """The account's current revision, for an event that records without bumping."""
    row = connection.execute(
        "SELECT revision FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    return 0 if row is None else int(row["revision"])


def _decode(raw: str) -> Value:
    """Parse a stored value. Annotated so the shape the column was written with is stated."""
    value: Value = json.loads(raw)
    return value


def _common_namespace(changed: tuple[str, ...]) -> str | None:
    """The namespace an event is about, when every change shares one.

    ``None`` when a document touched more than one, because an event that named the first
    of three namespaces would read as though only that one changed. The full list is in
    ``detail``; these columns exist so the common case -- one setting -- is queryable
    without parsing JSON.
    """
    namespaces = {qualified.split(".", 1)[0] for qualified in changed}
    return namespaces.pop() if len(namespaces) == 1 else None


def _common_key(changed: tuple[str, ...]) -> str | None:
    """The key an event is about, when it is about exactly one."""
    if len(changed) != 1:
        return None
    return changed[0].split(".", 1)[1]
