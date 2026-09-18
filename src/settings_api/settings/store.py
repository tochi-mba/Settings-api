"""What a settings store promises. The adapter is :mod:`settings_api.settings.sql_store`.

A port rather than a class because the storage decision here is genuinely open: SQLite is
right for one box serving a household, and a deployment serving an organisation would want
something else. Everything above this line is written against the port, so that change is
an adapter rather than a rewrite.

Four properties belong here rather than in the adapter, because they are promises rather
than implementation:

**Scope is exclusive, not overlaid.** Each catalogue entry is either account-scoped
(stored under the ``*`` sentinel) or profile-scoped (stored under a keyring profile name).
The same key is never resolved from both levels -- see ADR-0002 as amended. The store
does not know which is which; it writes the ``profile`` the service hands it.

**Storage is sparse.** A row exists only where somebody expressed a preference, so
:attr:`AccountState.values` contains only what was chosen and never a default that was
merely current at the time. See ADR-0005 for the defect that argument comes from.

**A write that changes nothing changes nothing.** No revision bump, no event, and an
identical ``PUT`` repeated is genuinely idempotent rather than merely harmless. Two
assistants that both decide to set the same value do not produce two entries in a person's
history of their own decisions.

**Everything in one :meth:`SettingsStore.apply` call is one transaction.** A document-
shaped write is all-or-nothing: one refused key rolls back every accepted one in the same
document. A partially applied settings change is worse than a rejected one, because the
caller believes it either succeeded or failed and it did neither.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from settings_api.domain.types import ACCOUNT_PROFILE

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from settings_api.domain.types import Value
    from settings_api.events.log import Action

MAX_PROFILES_PER_ACCOUNT = 32
"""How many distinct keyring profile names one account may have rows for.

Account-scoped rows (``*``) do not count. Without a cap a caller could invent profile
names and grow the table without bound; keyring's own profile ceiling is lower than this,
so a legitimate household never hits it.
"""


@dataclass(frozen=True, slots=True)
class StoredSetting:
    """One row: a value somebody chose, and the provenance of the choosing."""

    namespace: str
    key: str
    value: Value = field(repr=False)
    """``repr=False``: this is a person's own decision, and a repr ends up in test output,
    in a debugger, and in an exception's context when something upstream logs the object
    it was working on."""

    set_at: datetime
    set_by: str
    """The verified audience, or ``service:<name>``. Derived, never claimed."""

    profile: str = ACCOUNT_PROFILE
    """``*`` for an account-scoped row, or the keyring profile name for a profile-scoped one."""

    @property
    def qualified(self) -> str:
        return f"{self.namespace}.{self.key}"

    @property
    def row_key(self) -> tuple[str, str]:
        """How this row is keyed in :attr:`AccountState.rows`: ``(profile, namespace.key)``."""
        return (self.profile, self.qualified)


@dataclass(frozen=True, slots=True)
class AccountState:
    """Everything stored for one account, as of one consistent read."""

    exists: bool
    """Whether this account has ever written anything.

    Never the difference between working and 404. An account that has expressed no
    preference has the default preference, and every read path answers accordingly --
    which is why ``get_settings`` never 404s.
    """

    revision: int
    rows: Mapping[tuple[str, str], StoredSetting]
    """Every stored row, keyed by ``(profile, namespace.key)``."""

    @property
    def values(self) -> Mapping[tuple[str, str], Value]:
        """The stored values, keyed the same way as :attr:`rows`.

        Membership is what says a row exists -- a value of ``None`` is a real stored value
        for a nullable setting, so a mapping that omitted nulls would make "set to null"
        and "never set" indistinguishable.
        """
        return {key: row.value for key, row in self.rows.items()}

    def etag(self, account_id: str) -> str:
        """The entity tag for this state: ``"<account_id>.<revision>"``.

        Quoted, because RFC 9110 says an entity tag is a quoted string and a client that
        echoes an unquoted one back in ``If-None-Match`` will not match anything.
        """
        return f'"{account_id}.{self.revision}"'


@dataclass(frozen=True, slots=True)
class Change:
    """One setting to write, or one to remove."""

    namespace: str
    key: str
    value: Value = field(default=None, repr=False)
    reset: bool = False
    """When true, the row is removed and ``value`` is ignored."""

    profile: str = ACCOUNT_PROFILE
    """Which profile column this change addresses. ``*`` for account-scoped settings."""

    @property
    def qualified(self) -> str:
        return f"{self.namespace}.{self.key}"

    @property
    def row_key(self) -> tuple[str, str]:
        return (self.profile, self.qualified)


@dataclass(frozen=True, slots=True)
class Applied:
    """What one :meth:`SettingsStore.apply` actually did."""

    revision: int
    """The account's revision afterwards -- unchanged when nothing changed."""

    changed: tuple[str, ...]
    """The qualified names that actually changed. Empty for an idempotent repeat."""

    @property
    def any_change(self) -> bool:
        return bool(self.changed)


@runtime_checkable
class SettingsStore(Protocol):
    """One account's stored choices, and the revision that tracks them."""

    async def read(self, account_id: str) -> AccountState:
        """Everything stored for this account, in one consistent read.

        Never ``None`` and never raises for an unknown account: an account nobody has
        written to is :attr:`AccountState.exists` false, revision zero and no rows, which
        every read path renders as "the defaults".
        """
        ...

    # Account, clock, provenance, the cap, the changes, and the concurrency token. Every
    # one is independent and an options object would only be unpacked again here.
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
        """Apply every change as one transaction, or none of them.

        The account row is created here if it does not exist. That is deliberate and it
        fixes a defect user-api shipped: with no create step in the write path, every
        first write failed a foreign key -- and an assistant will never call an endpoint
        it was not told about.

        A change whose value already equals what is stored is not a change. If nothing
        changes, the revision is not bumped and no event is appended, which is what makes
        an identical repeated ``PUT`` genuinely idempotent.

        Args:
            changes: the settings to set or reset. Validation happened above this line;
                the store writes what it is given.
            now: the injected clock's reading.
            action: what to record in the event log.
            actor: the verified audience, or ``service:<name>``.
            service: the calling service, when one was mediating.
            event_cap: the most events this account keeps, trimmed in this transaction.
            if_revision: refuse unless the account is at this revision. ``None`` means the
                caller is not doing optimistic concurrency.

        Raises:
            RevisionMismatchError: ``if_revision`` was given and does not match. Checked
                inside the transaction, so "read the revision, then write" cannot go stale
                between the check and the write.
            InvalidSettingValueError: applying the change would put this account over
                :data:`MAX_PROFILES_PER_ACCOUNT` distinct profile names.
        """
        ...

    async def forget(self, account_id: str) -> int:
        """Destroy everything stored for one account. Returns how many rows went.

        The settings, the account row and the event log. Erasure is finished by
        :mod:`settings_api.settings.erasure`, which follows this with a truncating
        checkpoint -- a ``DELETE`` alone leaves the bytes in the ``-wal`` file.
        """
        ...

    async def purge_setting(
        self, namespace: str, key: str, *, now: datetime, event_cap: int
    ) -> int:
        """Destroy every account's rows for one retired key. Returns how many went.

        Not account-scoped, because a retirement is a fact about the catalogue and applies
        across every account at once. Each affected account gets one event saying its
        stored choice was destroyed, at the account's **current** revision and without
        bumping it: those rows had been ignored on every read since the key was retired,
        so no resolved value changed and invalidating every cached copy would be a lie.
        """
        ...
