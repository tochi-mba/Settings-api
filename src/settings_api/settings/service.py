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
    InvalidSettingValueError,
    SettingNotWritableError,
    SettingPinnedError,
    SettingScopeError,
    UnknownSettingsError,
)
from settings_api.domain.namespaces import require_granted, require_known
from settings_api.domain.registry import (
    BY_QUALIFIED,
    NAMESPACES,
    definition_for,
    live_entries_in,
)
from settings_api.domain.resolution import Resolved, resolve, resolve_for_service, resolve_namespace
from settings_api.domain.types import ACCOUNT_PROFILE, PROFILE_NAME_PATTERN, SettingScope
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

EXPORT_VERSION = 2
"""The shape of an exported document. Bumped only if the shape changes incompatibly.

Version 1 was a flat namespace-to-keys map (every setting treated as account-scoped).
Version 2 splits account-scoped choices from per-profile ones. Importers accept both.

Present from the first release rather than added later, because a document with no version
is one an importer has to guess about -- and the guess is made years afterwards by
somebody holding a file and no context.
"""

SUPPORTED_EXPORT_VERSIONS = frozenset({1, 2})
V1_PROFILE_FALLBACK = "personal"
"""Where version-1 exports of now-profile-scoped keys land.

``common.default_profile`` defaults to ``personal``, so restoring a backup taken before
scopes existed puts those choices on the profile the person already had if they never
named another.
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
    profiles: dict[str, dict[str, dict[str, Value]]]


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

    async def get_all(self, identity: Identity, *, profile: str | None = None) -> Document:
        """Every namespace this token grants, resolved.

        Never raises for an account that has never written anything: an account that has
        expressed no preference has the default preference. There is deliberately no 404
        anywhere on this path.

        ``profile`` selects which profile-scoped rows to resolve. Account-scoped settings
        are always included. Omitting it leaves profile-scoped settings at their default.
        """
        profile = _normalise_profile(profile)
        state = await self._store.read(identity.account_id)
        stored = self._stored_for(state, profile)
        namespaces = {
            namespace: resolve_namespace(namespace, stored=stored, policy=self._policy)
            for namespace in sorted(identity.namespaces)
        }
        return self._document(identity, state, namespaces)

    async def get_namespace(
        self, identity: Identity, namespace: str, *, profile: str | None = None
    ) -> Document:
        """One namespace, resolved.

        Raises:
            UnknownNamespaceError: no such namespace in this build.
            NamespaceNotGrantedError: the catalogue has it and this token does not grant
                it.
        """
        profile = _normalise_profile(profile)
        self._permit(identity, namespace)
        state = await self._store.read(identity.account_id)
        resolved = resolve_namespace(
            namespace, stored=self._stored_for(state, profile), policy=self._policy
        )
        return self._document(identity, state, {namespace: resolved})

    async def get_setting(
        self, identity: Identity, namespace: str, key: str, *, profile: str | None = None
    ) -> Resolved:
        """One setting, with where its value came from and whether it is pinned.

        Raises:
            UnknownNamespaceError, NamespaceNotGrantedError, UnknownSettingError.
        """
        profile = _normalise_profile(profile)
        definition = self._definition(identity, namespace, key)
        state = await self._store.read(identity.account_id)
        return resolve(definition, stored=self._stored_for(state, profile), policy=self._policy)

    async def describe(self, identity: Identity, *, profile: str | None = None) -> Document:
        """The catalogue, as this deployment has it, with each setting's current value.

        The same shape as :meth:`get_all` -- the difference is entirely in what the API
        layer renders from it, because a :class:`~settings_api.domain.resolution.Resolved`
        already carries the narrowed definition, the value, the source and the pin.
        """
        return await self.get_all(identity, profile=profile)

    async def resolve_for(
        self, identity: Identity, namespace: str, *, profile: str | None = None
    ) -> Document:
        """One namespace with ``common`` merged underneath, for a consuming service.

        Raises:
            UnknownNamespaceError, NamespaceNotGrantedError.
        """
        profile = _normalise_profile(profile)
        self._permit(identity, namespace)
        state = await self._store.read(identity.account_id)
        resolved = resolve_for_service(
            namespace, stored=self._stored_for(state, profile), policy=self._policy
        )
        return self._document(identity, state, {namespace: resolved})

    async def export(self, identity: Identity) -> Export:
        """Everything this person has chosen, as a portable document."""
        state = await self._store.read(identity.account_id)
        settings: dict[str, dict[str, Value]] = {}
        profiles: dict[str, dict[str, dict[str, Value]]] = {}
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
            definition = BY_QUALIFIED[row.qualified]
            if definition.scope is SettingScope.ACCOUNT:
                if row.profile == ACCOUNT_PROFILE:
                    settings.setdefault(row.namespace, {})[row.key] = row.value
                continue
            if row.profile == ACCOUNT_PROFILE:
                # Orphaned account-level row for a key that is now profile-scoped. Not
                # what a restore would write, and not what a read would find.
                continue
            profiles.setdefault(row.profile, {}).setdefault(row.namespace, {})[row.key] = row.value

        return Export(
            version=EXPORT_VERSION,
            exported_at=self._clock.now().isoformat(timespec="microseconds"),
            revision=state.revision,
            settings=settings,
            profiles=profiles,
        )

    async def read_events(
        self, identity: Identity, *, limit: int, before: int | None
    ) -> list[Event]:
        """This person's own history of their own decisions, newest first."""
        return await self._events.read(identity.account_id, limit=limit, before_sequence=before)

    # -- Writes ------------------------------------------------------------------------

    # Identity, namespace, key, value, which profile, and the concurrency token.
    async def set_setting(  # noqa: PLR0913
        self,
        identity: Identity,
        namespace: str,
        key: str,
        value: Value,
        *,
        profile: str | None = None,
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
            SettingScopeError: a profile-scoped setting written without a profile.
            InvalidSettingValueError, CredentialRefusedError: the value is refused.
            RevisionMismatchError: ``if_revision`` no longer matches.
        """
        profile = _normalise_profile(profile)
        definition = self._definition(identity, namespace, key)
        change = self._change_for(identity, definition, value, profile=profile)
        return await self._apply(identity, [change], action=Action.SET, if_revision=if_revision)

    async def update(
        self,
        identity: Identity,
        document: Mapping[str, Mapping[str, Value]],
        *,
        profile: str | None = None,
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

        ``profile`` applies to profile-scoped keys in the document. Account-scoped keys
        always write to the account, even when a profile is named.

        Raises:
            UnknownSettingsError: the document names settings that do not exist, naming
                all of them at once and pointing at ``describe_settings``. Never silently
                dropped: a caller that believes it wrote a setting it did not is worse off
                than one that was refused.
            Every error :meth:`set_setting` raises.
        """
        profile = _normalise_profile(profile)
        changes = self._changes_for(identity, document, profile=profile)
        return await self._apply(identity, changes, action=action, if_revision=if_revision)

    async def import_document(
        self,
        identity: Identity,
        document: Mapping[str, Mapping[str, Value]],
        *,
        profiles: Mapping[str, Mapping[str, Mapping[str, Value]]] | None = None,
        version: int = EXPORT_VERSION,
        if_revision: int | None = None,
    ) -> Written:
        """Apply a previously exported document.

        The same validation and the same one transaction as :meth:`update`. It is a
        separate operation only so the event log can tell "I restored a backup" from "I
        changed three things", which are different answers to "what did I do in March".
        """
        if version not in SUPPORTED_EXPORT_VERSIONS:
            known = ", ".join(str(item) for item in sorted(SUPPORTED_EXPORT_VERSIONS))
            msg = f"this build reads export formats {known}; the document says {version}"
            raise InvalidSettingValueError(msg)
        if version == 1:
            account_doc, profile_doc = self._split_legacy_document(identity, document)
            changes = self._changes_for(identity, account_doc, profile=None)
            if profile_doc:
                changes.extend(
                    self._changes_for(identity, profile_doc, profile=V1_PROFILE_FALLBACK)
                )
            return await self._apply(
                identity, changes, action=Action.IMPORT, if_revision=if_revision
            )
        changes = self._changes_for(identity, document, profile=None)
        for name, nested in (profiles or {}).items():
            named = _normalise_profile(name)
            changes.extend(self._changes_for(identity, nested, profile=named))
        return await self._apply(identity, changes, action=Action.IMPORT, if_revision=if_revision)

    async def reset_setting(
        self,
        identity: Identity,
        namespace: str,
        key: str,
        *,
        profile: str | None = None,
        if_revision: int | None = None,
    ) -> Written:
        """Return one setting to its resolved default.

        Not an erasure. The row goes and the event saying it was reset stays, which is the
        whole record that the person ever expressed that preference.
        """
        profile = _normalise_profile(profile)
        definition = self._definition(identity, namespace, key)
        self._check_writable(identity, definition)
        change = Change(
            namespace=definition.namespace,
            key=definition.key,
            reset=True,
            profile=_storage_profile(definition, profile),
        )
        return await self._apply(identity, [change], action=Action.RESET, if_revision=if_revision)

    async def reset_namespace(
        self,
        identity: Identity,
        namespace: str,
        *,
        profile: str | None = None,
        if_revision: int | None = None,
    ) -> Written:
        """Return every setting in one namespace to its resolved default.

        Pinned and owner-only settings in the namespace are skipped rather than refused.
        A caller asking to clear a namespace is not asking about any particular key in it,
        and refusing the whole request because one of its settings is pinned by policy
        would leave the caller with no way to clear the rest.

        Without ``profile``, account-scoped keys in the namespace are cleared.
        With ``profile``, profile-scoped keys for that profile are cleared.
        """
        profile = _normalise_profile(profile)
        self._permit(identity, namespace)
        if profile is None:
            entries = [
                entry for entry in live_entries_in(namespace) if entry.scope is SettingScope.ACCOUNT
            ]
            storage = ACCOUNT_PROFILE
        else:
            entries = [
                entry for entry in live_entries_in(namespace) if entry.scope is SettingScope.PROFILE
            ]
            storage = profile
        changes = [
            Change(namespace=namespace, key=entry.key, reset=True, profile=storage)
            for entry in entries
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

    def _change_for(
        self,
        identity: Identity,
        definition: SettingDef,
        value: Value,
        *,
        profile: str | None,
    ) -> Change:
        """Turn one requested value into a change, or refuse it."""
        self._check_writable(identity, definition)
        effective = self._policy.definition_of(definition)
        checked = value_rules.validate(effective, value, max_bytes=self._config.max_value_bytes)
        return Change(
            namespace=definition.namespace,
            key=definition.key,
            value=checked,
            profile=_storage_profile(definition, profile),
        )

    def _changes_for(
        self,
        identity: Identity,
        document: Mapping[str, Mapping[str, Value]],
        *,
        profile: str | None,
    ) -> list[Change]:
        """Turn a whole document into changes, refusing every unknown key at once.

        Unknown keys are collected rather than raised on the first one, because a caller
        fixing a document wants the whole list -- one round trip per typo is how an
        assistant ends up in a loop.
        """
        unknown: list[str] = []
        pending: list[tuple[SettingDef, Value]] = []

        for namespace, entries in document.items():
            self._permit(identity, namespace)
            known = {entry.key: entry for entry in live_entries_in(namespace)}
            for key, value in entries.items():
                definition = known.get(key)
                if definition is None:
                    unknown.append(f"{namespace}.{key}")
                    continue
                pending.append((definition, value))

        if unknown:
            msg = f"no such setting: {', '.join(sorted(unknown))}; {DESCRIBE_HINT}"
            raise UnknownSettingsError(msg)
        _require_profile_for([definition for definition, _value in pending], profile)
        return [
            self._change_for(identity, definition, value, profile=profile)
            for definition, value in pending
        ]

    def _split_legacy_document(
        self, identity: Identity, document: Mapping[str, Mapping[str, Value]]
    ) -> tuple[dict[str, dict[str, Value]], dict[str, dict[str, Value]]]:
        """Split a version-1 export into account-scoped and profile-scoped maps."""
        account: dict[str, dict[str, Value]] = {}
        profiled: dict[str, dict[str, Value]] = {}
        unknown: list[str] = []
        for namespace, entries in document.items():
            self._permit(identity, namespace)
            known = {entry.key: entry for entry in live_entries_in(namespace)}
            for key, value in entries.items():
                definition = known.get(key)
                if definition is None:
                    unknown.append(f"{namespace}.{key}")
                    continue
                target = profiled if definition.scope is SettingScope.PROFILE else account
                target.setdefault(namespace, {})[key] = value
        if unknown:
            msg = f"no such setting: {', '.join(sorted(unknown))}; {DESCRIBE_HINT}"
            raise UnknownSettingsError(msg)
        return account, profiled

    def _stored_for(self, state: AccountState, profile: str | None) -> dict[str, Value]:
        """The mapping :mod:`settings_api.domain.resolution` wants for this profile.

        Account-scoped rows (``*``) are always included. Profile-scoped rows are included
        only when they match ``profile``. An orphaned ``*`` row for a profile-scoped key
        is ignored -- exclusive scopes, not overlay.
        """
        stored: dict[str, Value] = {}
        for (row_profile, qualified), row in state.rows.items():
            definition = BY_QUALIFIED.get(qualified)
            if definition is None:
                continue
            if definition.scope is SettingScope.ACCOUNT:
                if row_profile == ACCOUNT_PROFILE:
                    stored[qualified] = row.value
            elif profile is not None and row_profile == profile:
                stored[qualified] = row.value
        return stored

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


def _normalise_profile(profile: str | None) -> str | None:
    """Accept a keyring profile name, or nothing. Refuse the sentinel and any other shape."""
    if profile is None or profile == "":
        return None
    if not PROFILE_NAME_PATTERN.fullmatch(profile):
        msg = (
            "profile must be a keyring profile name (lowercase letters, digits, '.', '_' "
            "or '-'); '*' is reserved for account-scoped rows"
        )
        raise InvalidSettingValueError(msg)
    return profile


def _storage_profile(definition: SettingDef, profile: str | None) -> str:
    """Which profile column a write for this entry addresses."""
    if definition.scope is SettingScope.ACCOUNT:
        return ACCOUNT_PROFILE
    if profile is None:
        msg = (
            f"{definition.qualified} is profile-scoped; pass ?profile= with a "
            "keyring profile name such as personal or work"
        )
        raise SettingScopeError(msg)
    return profile


def _require_profile_for(definitions: Sequence[SettingDef], profile: str | None) -> None:
    """Refuse a document that names profile-scoped keys without saying which profile."""
    if profile is not None:
        return
    missing = [
        definition.qualified
        for definition in definitions
        if definition.scope is SettingScope.PROFILE
    ]
    if not missing:
        return
    listed = ", ".join(sorted(missing))
    verb = "is" if len(missing) == 1 else "are"
    msg = (
        f"{listed} {verb} profile-scoped; pass ?profile= with a keyring profile name "
        "such as personal or work"
    )
    raise SettingScopeError(msg)
