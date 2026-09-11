"""Where a request becomes a change.

Every rule that is about *this request* rather than about storage or about HTTP is here,
and they run in a fixed order that is worth stating once, because the order is what
decides which of several true things a caller is told:

1. **Is there such a namespace?** A typo is told it is a typo, not told it lacks
   permission -- otherwise a caller goes looking for a permissions problem it does not
   have. Safe to answer: the catalogue is identical in every deployment of a build and is
   published in full by ``describe_settings``.
2. **Does this token grant it?** A fact about the caller's own token, so being specific
   leaks nothing.
3. **Is there such a setting?**
4. **May this caller write it at all?** Pinned by policy is a 409 that says so; owner-only
   is a 403. Both come before the value is looked at, because "you cannot change this"
   is more useful than "that value is out of range" when both are true.
5. **Is the value allowed?** Shape, then credentials, then size -- see
   :mod:`settings_api.domain.values`.

Only then does anything reach the store, where the whole set of changes is applied as one
transaction.

## What this layer does not do

It does not know what a status code is; it raises domain errors. It does not read a clock;
one is injected. And it never logs a value -- every log record here names the namespace,
the key, the revision and a count, which is the rule
:mod:`settings_api.core.logging` exists to catch the exception to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from settings_api.core.logging import get_logger
from settings_api.domain import values as value_rules
from settings_api.domain.errors import (
    SettingNotWritableError,
    SettingPinnedError,
    UnknownSettingsError,
)
from settings_api.domain.namespaces import require_granted, require_known
from settings_api.domain.registry import (
    NAMESPACES,
    definition_for,
    live_entries_in,
)
from settings_api.domain.resolution import Resolved, resolve, resolve_for_service, resolve_namespace
from settings_api.events.log import Action
from settings_api.settings.store import AccountState, Change

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from settings_api.auth.tokens import Identity
    from settings_api.core.clock import Clock
    from settings_api.core.config import Settings
    from settings_api.domain.policy import Policy
    from settings_api.domain.types import SettingDef, Value
    from settings_api.events.log import Event, EventLog
    from settings_api.settings.store import Applied, SettingsStore

logger = get_logger(__name__)

EXPORT_VERSION = 1
"""The shape of an exported document. Bumped only if the shape changes incompatibly.

Present from the first release rather than added later, because a document with no version
is one an importer has to guess about -- and the guess is made years afterwards by
somebody holding a file and no context.
"""

DESCRIBE_HINT = "see describe_settings for every setting this build has"


@dataclass(frozen=True, slots=True)
class Document:
    """A resolved settings document, and the revision it was read at."""

    namespaces: dict[str, dict[str, Resolved]]
    revision: int
    etag: str

    def values(self) -> dict[str, dict[str, Value]]:
        """Just the values, in the shape a caller reading settings wants."""
        return {
            namespace: {key: item.value for key, item in entries.items()}
            for namespace, entries in self.namespaces.items()
        }


@dataclass(frozen=True, slots=True)
class Written:
    """What a write did, for the response and for the caller's cache."""

    revision: int
    etag: str
    changed: tuple[str, ...]

    @property
    def any_change(self) -> bool:
        return bool(self.changed)


@dataclass(frozen=True, slots=True)
class Export:
    """A portable copy of everything this person has chosen.

    Sparse, like storage: only what somebody actually chose. An export that filled in
    every default would be a document that froze this deployment's defaults into somebody
    else's deployment on import -- so restoring a backup after a default changed would
    silently pin the old value, for every setting, with no way to tell which ones the
    person had meant.
    """

    version: int
    exported_at: str
    revision: int
    settings: dict[str, dict[str, Value]]


class SettingsService:
    """Reads and writes one person's settings, through the store and the catalogue."""

    def __init__(
        self,
        *,
        store: SettingsStore,
        events: EventLog,
        policy: Policy,
        clock: Clock,
        config: Settings,
    ) -> None:
        self._store = store
        self._events = events
        self._policy = policy
        self._clock = clock
        self._config = config

    # -- Reads -------------------------------------------------------------------------

    async def get_all(self, identity: Identity) -> Document:
        """Every namespace this token grants, resolved.

        Never raises for an account that has never written anything: an account that has
        expressed no preference has the default preference. There is deliberately no 404
        anywhere on this path.
        """
        state = await self._store.read(identity.account_id)
        namespaces = {
            namespace: resolve_namespace(namespace, stored=state.values, policy=self._policy)
            for namespace in sorted(identity.namespaces)
        }
        return self._document(identity, state, namespaces)

    async def get_namespace(self, identity: Identity, namespace: str) -> Document:
        """One namespace, resolved.

        Raises:
            UnknownNamespaceError: no such namespace in this build.
            NamespaceNotGrantedError: the catalogue has it and this token does not grant
                it.
        """
        self._permit(identity, namespace)
        state = await self._store.read(identity.account_id)
        resolved = resolve_namespace(namespace, stored=state.values, policy=self._policy)
        return self._document(identity, state, {namespace: resolved})

    async def get_setting(self, identity: Identity, namespace: str, key: str) -> Resolved:
        """One setting, with where its value came from and whether it is pinned.

        Raises:
            UnknownNamespaceError, NamespaceNotGrantedError, UnknownSettingError.
        """
        definition = self._definition(identity, namespace, key)
        state = await self._store.read(identity.account_id)
        return resolve(definition, stored=state.values, policy=self._policy)

    async def describe(self, identity: Identity) -> Document:
        """The catalogue, as this deployment has it, with each setting's current value.

        The same shape as :meth:`get_all` -- the difference is entirely in what the API
        layer renders from it, because a :class:`~settings_api.domain.resolution.Resolved`
        already carries the narrowed definition, the value, the source and the pin.
        """
        return await self.get_all(identity)

    async def resolve_for(self, identity: Identity, namespace: str) -> Document:
        """One namespace with ``common`` merged underneath, for a consuming service.

        Raises:
            UnknownNamespaceError, NamespaceNotGrantedError.
        """
        self._permit(identity, namespace)
        state = await self._store.read(identity.account_id)
        resolved = resolve_for_service(namespace, stored=state.values, policy=self._policy)
        return self._document(identity, state, {namespace: resolved})

    async def export(self, identity: Identity) -> Export:
        """Everything this person has chosen, as a portable document."""
        state = await self._store.read(identity.account_id)
        settings: dict[str, dict[str, Value]] = {}
        for row in state.rows.values():
            if row.namespace not in identity.namespaces:
                # An export is bounded by the token, like every other read. A
                # `settings.search` token exports the search preferences and nothing else,
                # which is the only answer consistent with what that token can read.
                continue
            if not self._is_live(row.namespace, row.key):
                # A retired key's rows survive so that reverting the catalogue restores
                # them; they are not part of what this person can see or restore today.
                continue
            settings.setdefault(row.namespace, {})[row.key] = row.value

        return Export(
            version=EXPORT_VERSION,
            exported_at=self._clock.now().isoformat(timespec="microseconds"),
            revision=state.revision,
            settings=settings,
        )

    async def read_events(
        self, identity: Identity, *, limit: int, before: int | None
    ) -> list[Event]:
        """This person's own history of their own decisions, newest first."""
        return await self._events.read(identity.account_id, limit=limit, before_sequence=before)

    # -- Writes ------------------------------------------------------------------------

    async def set_setting(
        self,
        identity: Identity,
        namespace: str,
        key: str,
        value: Value,
        *,
        if_revision: int | None = None,
    ) -> Written:
        """Set one setting.

        Raises:
            UnknownNamespaceError, NamespaceNotGrantedError, UnknownSettingError: the
                setting cannot be addressed by this caller.
            SettingPinnedError: operator policy fixes this value. A 409 that says so, and
                never a silent no-op.
            SettingNotWritableError: an ``owner_writable_only`` setting written by a
                service rather than by the person.
            InvalidSettingValueError, CredentialRefusedError: the value is refused.
            RevisionMismatchError: ``if_revision`` no longer matches.
        """
        definition = self._definition(identity, namespace, key)
        change = self._change_for(identity, definition, value)
        return await self._apply(identity, [change], action=Action.SET, if_revision=if_revision)

    async def update(
        self,
        identity: Identity,
        document: Mapping[str, Mapping[str, Value]],
        *,
        if_revision: int | None = None,
        action: Action = Action.UPDATE,
    ) -> Written:
        """Apply a whole document, all of it or none of it.

        **This merges rather than replaces**, and the route description says so where a
        caller will read it. A ``PUT`` that replaced would mean an assistant sending two
        keys silently discarded every other preference the person had expressed, and the
        only warning would be the word ``PUT``. Clearing is what ``reset_setting`` and
        ``reset_namespace`` are for, and they say what they do in their names.

        Every key is checked before anything is written, and the whole set is one
        transaction, so a document whose last key is refused has not written its first.

        Raises:
            UnknownSettingsError: the document names settings that do not exist, naming
                all of them at once and pointing at ``describe_settings``. Never silently
                dropped: a caller that believes it wrote a setting it did not is worse off
                than one that was refused.
            Every error :meth:`set_setting` raises.
        """
        changes = self._changes_for(identity, document)
        return await self._apply(identity, changes, action=action, if_revision=if_revision)

    async def import_document(
        self,
        identity: Identity,
        document: Mapping[str, Mapping[str, Value]],
        *,
        if_revision: int | None = None,
    ) -> Written:
        """Apply a previously exported document.

        The same validation and the same one transaction as :meth:`update`. It is a
        separate operation only so the event log can tell "I restored a backup" from "I
        changed three things", which are different answers to "what did I do in March".
        """
        return await self.update(identity, document, if_revision=if_revision, action=Action.IMPORT)

    async def reset_setting(
        self, identity: Identity, namespace: str, key: str, *, if_revision: int | None = None
    ) -> Written:
        """Return one setting to its resolved default.

        Not an erasure. The row goes and the event saying it was reset stays, which is the
        whole record that the person ever expressed that preference.
        """
        definition = self._definition(identity, namespace, key)
        self._check_writable(identity, definition)
        change = Change(namespace=definition.namespace, key=definition.key, reset=True)
        return await self._apply(identity, [change], action=Action.RESET, if_revision=if_revision)

    async def reset_namespace(
        self, identity: Identity, namespace: str, *, if_revision: int | None = None
    ) -> Written:
        """Return every setting in one namespace to its resolved default.

        Pinned and owner-only settings in the namespace are skipped rather than refused.
        A caller asking to clear a namespace is not asking about any particular key in it,
        and refusing the whole request because one of its settings is pinned by policy
        would leave the caller with no way to clear the rest.
        """
        self._permit(identity, namespace)
        changes = [
            Change(namespace=namespace, key=entry.key, reset=True)
            for entry in live_entries_in(namespace)
            if self._may_write(identity, entry)
        ]
        return await self._apply(
            identity, changes, action=Action.RESET_NAMESPACE, if_revision=if_revision
        )

    async def forget(self, identity: Identity) -> int:
        """Destroy everything stored for this account. Returns how many rows went.

        The caller is :mod:`settings_api.settings.erasure`, which follows this with a
        truncating checkpoint. A ``DELETE`` alone leaves the bytes in the ``-wal`` file,
        where ``grep`` finds them.
        """
        removed = await self._store.forget(identity.account_id)
        logger.info("settings_forgotten", removed=removed)
        return removed

    # -- Internals ---------------------------------------------------------------------

    def _document(
        self,
        identity: Identity,
        state: AccountState,
        namespaces: dict[str, dict[str, Resolved]],
    ) -> Document:
        return Document(
            namespaces=namespaces,
            revision=state.revision,
            etag=state.etag(identity.account_id),
        )

    def _permit(self, identity: Identity, namespace: str) -> None:
        """Check a namespace exists and that this token grants it, in that order."""
        require_known(namespace, known=NAMESPACES)
        require_granted(namespace, granted=identity.namespaces)

    def _definition(self, identity: Identity, namespace: str, key: str) -> SettingDef:
        """The catalogue entry a request names, once the caller is entitled to it."""
        self._permit(identity, namespace)
        return definition_for(namespace, key)

    def _is_live(self, namespace: str, key: str) -> bool:
        """Whether a stored row's key is still in the catalogue."""
        return any(entry.key == key for entry in live_entries_in(namespace))

    def _may_write(self, identity: Identity, definition: SettingDef) -> bool:
        """Whether this caller could write this setting, without raising about it."""
        if self._policy.is_pinned(definition):
            return False
        return not (definition.owner_writable_only and identity.service is not None)

    def _check_writable(self, identity: Identity, definition: SettingDef) -> None:
        """Refuse a write the caller is not entitled to make at all.

        Before the value is looked at, because "this is pinned" and "only you can change
        this" are both more useful than "that value is out of range" when more than one is
        true.

        Raises:
            SettingPinnedError: operator policy fixes the value.
            SettingNotWritableError: the setting is the person's own to change and a
                service is asking.
        """
        if self._policy.is_pinned(definition):
            msg = (
                f"{definition.qualified} is pinned by this deployment's policy and "
                "cannot be changed here"
            )
            raise SettingPinnedError(msg)

        if definition.owner_writable_only and identity.service is not None:
            msg = (
                f"{definition.qualified} can only be changed by the person themselves, "
                "with a token minted for settings"
            )
            raise SettingNotWritableError(msg)

    def _change_for(self, identity: Identity, definition: SettingDef, value: Value) -> Change:
        """Turn one requested value into a change, or refuse it."""
        self._check_writable(identity, definition)
        effective = self._policy.definition_of(definition)
        checked = value_rules.validate(effective, value, max_bytes=self._config.max_value_bytes)
        return Change(namespace=definition.namespace, key=definition.key, value=checked)

    def _changes_for(
        self, identity: Identity, document: Mapping[str, Mapping[str, Value]]
    ) -> list[Change]:
        """Turn a whole document into changes, refusing every unknown key at once.

        Unknown keys are collected rather than raised on the first one, because a caller
        fixing a document wants the whole list -- one round trip per typo is how an
        assistant ends up in a loop.
        """
        unknown: list[str] = []
        changes: list[Change] = []

        for namespace, entries in document.items():
            self._permit(identity, namespace)
            known = {entry.key: entry for entry in live_entries_in(namespace)}
            for key, value in entries.items():
                definition = known.get(key)
                if definition is None:
                    unknown.append(f"{namespace}.{key}")
                    continue
                changes.append(self._change_for(identity, definition, value))

        if unknown:
            msg = f"no such setting: {', '.join(sorted(unknown))}; {DESCRIBE_HINT}"
            raise UnknownSettingsError(msg)
        return changes

    async def _apply(
        self,
        identity: Identity,
        changes: Sequence[Change],
        *,
        action: Action,
        if_revision: int | None,
    ) -> Written:
        """Hand a validated set of changes to the store and report what happened."""
        applied = await self._store.apply(
            identity.account_id,
            changes=changes,
            now=self._clock.now(),
            action=action,
            actor=identity.actor,
            service=identity.service,
            event_cap=self._config.max_events,
            if_revision=if_revision,
        )
        self._log(action, applied)
        return Written(
            revision=applied.revision,
            etag=f'"{identity.account_id}.{applied.revision}"',
            changed=applied.changed,
        )

    def _log(self, action: Action, applied: Applied) -> None:
        """Record what changed by name and by count, and never by value."""
        logger.info(
            "settings_applied",
            action=action.value,
            revision=applied.revision,
            changed_count=len(applied.changed),
            changed_keys=list(applied.changed),
        )
