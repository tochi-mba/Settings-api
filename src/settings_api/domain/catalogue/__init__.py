"""The catalogue: every setting this build knows about, assembled and checked at import.

One module per namespace, each a tuple of frozen :class:`~settings_api.domain.types.SettingDef`
entries. This module puts them together and refuses a build whose catalogue is malformed.

## Why the check happens at import

A badly shaped entry -- a default its own bounds reject, an enum with no choices, a
``USE_DEFAULT`` entry that never said what it is safe to fall back to -- is a mistake made
while editing a table, and the useful moment to hear about it is the moment the process
starts, not the first time somebody reads that namespace. So every entry is checked here,
and a process holding a broken catalogue does not start.

## Why this package may import almost nothing

An import-linter contract restricts every module under this package to
:mod:`settings_api.domain.types`. The catalogue is a table, and a table is something a
person can read in one sitting and check against what the services actually do. A
catalogue module that could import the store is a catalogue entry that could have
behaviour, and thirty settings with behaviour are no longer reviewable as a table.

That restriction is why the lookups that raise this package's domain errors live next
door in :mod:`settings_api.domain.registry` rather than here, and why
:meth:`~settings_api.domain.types.SettingDef.validate` raises a plain ``ValueError``.

## Adding a setting

One entry in one of these modules, then ``make catalogue`` to regenerate
``docs/catalogue.md``. No migration, no schema change, and no deploy of six services --
that is the property the whole design is arranged around, and it is worth protecting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from settings_api.domain.catalogue import (
    common,
    keyring,
    media,
    persona,
    search,
    spotify,
    user,
)

if TYPE_CHECKING:
    from settings_api.domain.types import SettingDef

COMMON = common.NAMESPACE
"""The namespace every service may read. Named here so callers need not spell it."""

_MODULES = (common, keyring, user, persona, media, spotify, search)
"""Every namespace module, in the order the documentation presents them.

``common`` first because everything else is read alongside it; then the two services whose
settings already exist behind a port, then the rest. The order is documentation rather
than behaviour -- nothing resolves differently because of it.
"""


def _assemble() -> dict[str, tuple[SettingDef, ...]]:
    """Build the catalogue and refuse it if anything about it is wrong.

    Raises:
        ValueError: naming the entry and the rule it broke. Raised at import, so a build
            with a malformed catalogue fails to start rather than failing per request.
    """
    catalogue: dict[str, tuple[SettingDef, ...]] = {}
    for module in _MODULES:
        namespace = module.NAMESPACE
        if namespace in catalogue:
            msg = f"two catalogue modules both define the {namespace!r} namespace"
            raise ValueError(msg)

        entries = module.SETTINGS
        seen: set[str] = set()
        for entry in entries:
            if entry.namespace != namespace:
                msg = f"{entry.qualified} is declared in the {namespace!r} module"
                raise ValueError(msg)
            if entry.key in seen:
                msg = f"{entry.qualified} is declared twice"
                raise ValueError(msg)
            seen.add(entry.key)
            entry.check()

        catalogue[namespace] = entries
    return catalogue


CATALOGUE: dict[str, tuple[SettingDef, ...]] = _assemble()
"""Every namespace, in documentation order, each holding its entries in declared order."""

NAMESPACES: frozenset[str] = frozenset(CATALOGUE)
"""Every namespace name this build knows."""

BY_QUALIFIED: dict[str, SettingDef] = {
    entry.qualified: entry for entries in CATALOGUE.values() for entry in entries
}
"""Every entry by ``namespace.key``, which is how a setting is named outside a URL."""
