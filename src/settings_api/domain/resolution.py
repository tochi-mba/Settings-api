"""Turning three layers into one answer.

An effective value for ``(account, namespace, key)`` comes from three places, and the
whole rule fits in one function::

    catalogue default        what the setting is, anywhere
      -> operator policy     what this deployment narrowed it to, or pinned it at
        -> account value     what this person actually chose

with one exception to "last wins": a **pin beats the account**. It has to. A pin added
after somebody already expressed a preference would otherwise be a pin that does nothing
for exactly the people it was added for, and an operator who capped a value would have
capped it only for accounts that had never touched it.

## Storage is sparse, and that is the point

Only explicit choices are rows. "Unset" is genuinely distinguishable from "set to a value
that happens to equal the default", so changing a catalogue default moves everyone who
never expressed a preference and nobody who did.

This is the fix for a defect user-api actually shipped: a settings update that created the
row wrote the *domain* default for the fields it was not asked to change, so an account on
a deployment configured for a seven-day grace window silently acquired a thirty-day one
the first time it changed something else. With sparse rows that is unrepresentable -- see
ADR-0005.

Note the consequence for :data:`Resolved.set_by_account`: it reports whether a **row
exists**, not whether the value differs from the default. A person who deliberately set
``grace_days`` to 30 on a deployment whose default is 30 has expressed a preference, and
it survives a change of default. That is what they asked for.

## Nothing here reads a database

Every function takes the stored values as a mapping and returns values. That is what
makes the eight combinations of default, policy and account testable as arithmetic, and it
is why this module is in ``domain`` rather than next to the store.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from settings_api.domain.registry import COMMON, live_entries_in

if TYPE_CHECKING:
    from collections.abc import Mapping

    from settings_api.domain.policy import Policy
    from settings_api.domain.types import SettingDef, Value


class Source(StrEnum):
    """Where an effective value came from.

    Reported on every read, because "why is it this" is the question a person asks when a
    setting is not what they expected -- and the three answers need three different
    actions: change it, ask the operator, or it is simply the default.
    """

    DEFAULT = "default"
    """The catalogue's own default. Nobody has said anything about this setting."""

    POLICY = "policy"
    """This deployment's default, or its pin. Ask the operator, not the service."""

    ACCOUNT = "account"
    """This person chose it. Changing it is theirs to do."""


@dataclass(frozen=True, slots=True)
class Resolved:
    """One setting's effective value, and everything a caller needs to act on it."""

    definition: SettingDef
    """The entry **as this deployment has it**, with any policy narrowing applied.

    The narrowed one rather than the catalogue's, so ``describe_settings`` reports the
    bounds a write would actually be checked against. Reporting the catalogue's would
    offer a range and then refuse most of it.
    """

    value: Value
    source: Source
    set_by_account: bool
    """Whether a row exists for this account -- not whether the value differs from the
    default. See the module docstring."""

    pinned: bool

    @property
    def key(self) -> str:
        return self.definition.key

    @property
    def namespace(self) -> str:
        return self.definition.namespace

    @property
    def qualified(self) -> str:
        return self.definition.qualified


def resolve(definition: SettingDef, *, stored: Mapping[str, Value], policy: Policy) -> Resolved:
    """Resolve one setting from the three layers.

    Args:
        definition: the catalogue entry, before policy.
        stored: this account's rows, keyed by ``namespace.key``. Membership is what says a
            row exists -- a value of ``None`` is a real stored value for a nullable
            setting, so ``.get()`` returning ``None`` could not tell the two apart.
        policy: this deployment's narrowing.
    """
    effective = policy.definition_of(definition)
    pinned = policy.is_pinned(definition)
    has_row = effective.qualified in stored

    if pinned:
        return Resolved(
            definition=effective,
            value=policy.pin_on(definition),
            source=Source.POLICY,
            set_by_account=has_row,
            pinned=True,
        )

    if has_row:
        return Resolved(
            definition=effective,
            value=stored[effective.qualified],
            source=Source.ACCOUNT,
            set_by_account=True,
            pinned=False,
        )

    # No row. The value is a default, and which *kind* of default is the difference
    # between "nobody has said anything" and "your operator chose this for you" -- which
    # are different things for a person to be told.
    from_policy = effective.default != definition.default
    return Resolved(
        definition=effective,
        value=effective.default,
        source=Source.POLICY if from_policy else Source.DEFAULT,
        set_by_account=False,
        pinned=False,
    )


def resolve_namespace(
    namespace: str, *, stored: Mapping[str, Value], policy: Policy
) -> dict[str, Resolved]:
    """Resolve every live setting in one namespace, keyed by key.

    Retired entries are absent: a retired key is gone from the request surface, and its
    rows survive only so that reverting the catalogue restores the choice intact.
    """
    return {
        entry.key: resolve(entry, stored=stored, policy=policy)
        for entry in live_entries_in(namespace)
    }


def resolve_for_service(
    namespace: str, *, stored: Mapping[str, Value], policy: Policy
) -> dict[str, Resolved]:
    """Resolve one namespace with ``common`` merged underneath it.

    What a consuming service asks for and gets: its own compartment plus the answers
    everybody needs. The namespace wins on a key collision.

    No collision is currently possible, and the rule is defined anyway -- so that adding a
    key to ``common`` can never silently change what an existing namespace resolves to.
    Defining it the other way round would mean a new ``common`` key could quietly override
    a service's own setting of the same name, which is the kind of change that lands on a
    Friday.
    """
    if namespace == COMMON:
        return resolve_namespace(COMMON, stored=stored, policy=policy)
    merged = resolve_namespace(COMMON, stored=stored, policy=policy)
    merged.update(resolve_namespace(namespace, stored=stored, policy=policy))
    return merged
