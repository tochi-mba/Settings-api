"""What the change log promises. The adapter is :mod:`settings_api.events.sql_log`.

The log answers "what did you change, and when did I tell you that?" -- which is the
question a person asks when a service behaves in a way they did not expect. It is not an
audit log in keyring's sense: there is no privileged actor here and nothing to hold to
account. It is the person's own history of their own decisions.

Four properties are worth stating before the methods.

**An event outlives the setting it describes.** There are no foreign keys. Resetting a
setting removes the row, and the event saying it was reset is the entire record that it
ever existed -- a foreign key would delete that record along with the thing it recorded.

**No event ever carries a value.** ``detail`` holds which keys were touched and how many:
metadata about the change, never the change. A log that recorded old values would be a
second copy of the person's decisions, surviving the row it describes, in a table nobody
thinks of as holding data. user-api makes recording old values an opt-in setting because
its values are facts a person volunteered; here there is no such setting and no such
option, because a settings value is short, guessable and repeatedly the same -- so a log
of them is a log of the person's posture over time, which is exactly what
``forget_settings`` is supposed to destroy.

**The write method is synchronous and takes a live connection.** Unusual for a port in
this codebase, and the entire reason this one is shaped like this: an event written in its
own transaction is an event that can be absent when the write it describes succeeded, or
present when that write rolled back. A log that is usually right is not a log.

**The log is capped and trimmed in the same transaction that appends.** A caller that
appended and then trimmed would leave a window in which the log is over its cap, and two
concurrent appends would both read the same count and both decline to trim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime


class Action(StrEnum):
    """What happened. One value per way the stored settings can change."""

    SET = "set"
    """One setting was given a value."""

    RESET = "reset"
    """One setting was returned to its resolved default. Not an erasure: the row goes and
    this event stays, so the record that it happened survives."""

    RESET_NAMESPACE = "reset_namespace"
    """Every setting in one namespace was reset."""

    UPDATE = "update"
    """A whole document was applied, all or nothing."""

    IMPORT = "import"
    """A previously exported document was applied. Distinct from :attr:`UPDATE` because
    'I restored a backup' and 'I changed three things' are different answers to "what did
    I do in March", and the distinction costs one enum value."""

    FORGET = "forget"
    """Everything was destroyed. The one event that does not survive its own operation:
    ``forget_settings`` deletes the log too, because "delete everything you know about me"
    has one honest meaning."""

    PURGE_RETIRED = "purge_retired"
    """The sweeper destroyed rows for a key that left the catalogue.

    Logged even though it changes no effective value -- reads have ignored those rows
    since the moment the key was retired -- because the person did once express that
    preference and is entitled to know the record of it is gone."""


@dataclass(frozen=True, slots=True)
class Event:
    """One recorded change."""

    sequence: int
    account_id: str
    at: datetime
    action: Action
    revision: int
    """What the account's revision became. An event whose revision equals the previous
    one cannot exist: a write that changes nothing bumps nothing and logs nothing."""

    actor: str
    """The verified audience of the token that did it, or ``service:<name>``. Derived by
    the server from the token presented, never claimed by the writer."""

    namespace: str | None = None
    key: str | None = None
    service: str | None = None
    detail: dict[str, object] | None = field(default=None, repr=False)
    """Which keys were touched and how many. **Never a value.**

    ``repr=False`` even so. This is the one attribute that holds anything shaped like
    request content, and a repr ends up in test output, in a debugger, and -- the one that
    matters -- in an exception's context when something upstream logs the object it was
    working on.
    """


@runtime_checkable
class EventLog(Protocol):
    """The append-only record of what changed."""

    # One parameter per column. Grouping them into an object would add a type whose only
    # job is to be constructed on one line and unpacked on the next.
    def append_in(  # noqa: PLR0913
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        at: datetime,
        action: Action,
        revision: int,
        actor: str,
        cap: int,
        namespace: str | None = None,
        key: str | None = None,
        service: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        """Append one event **inside a transaction the caller already holds**.

        Args:
            connection: the open transaction to write into.
            account_id: whose log.
            at: the injected clock's reading, not the wall clock's.
            action: what happened.
            revision: what the account's revision became in this same transaction.
            actor: the audience of the token that did it -- verified, not claimed.
            cap: the most events one account keeps. Trimmed here, in this transaction.
            namespace: which namespace, when the event is about one.
            key: which setting, when the event is about one.
            service: the calling service, when a service was mediating.
            detail: which keys were touched and how many. **Never a value.**
        """
        ...

    def delete_for_account_in(self, connection: sqlite3.Connection, *, account_id: str) -> int:
        """Remove an account's entire log. Returns how many events went.

        Only ``forget_settings`` reaches this.
        """
        ...

    async def read(
        self, account_id: str, *, limit: int, before_sequence: int | None = None
    ) -> list[Event]:
        """One page of an account's log, newest first.

        Paged by ``sequence`` rather than by timestamp, because the clock is injectable
        and two events in one tick share a timestamp -- which would make the boundary
        between two pages non-deterministic in exactly the tests that care about it.
        """
        ...

    async def count_for_account(self, account_id: str) -> int:
        """How many events this account has."""
        ...
