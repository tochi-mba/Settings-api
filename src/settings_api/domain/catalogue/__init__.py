"""The catalogue: every setting this build knows about, assembled and checked at import.

One module per namespace, each a tuple of frozen :class:`~settings_api.domain.types.SettingDef`
entries. This module puts them together and refuses a build whose catalogue is malformed.

## Why the check happens at import

A badly shaped entry -- a default its own bounds reject, an enum with no choices, a
``USE_DEFAULT`` entry that never said what it is safe to fall back to -- is a mistake made
while editing a table, and the useful moment to hear about it is the moment the process
starts, not the first time somebody reads that namespace. So every entry is checked here,
and a process holding a broken catalogue does not start.

## Why namespaces are discovered rather than listed

``_MODULES`` holds the namespaces this repository ships. Anything else attaches through
the entry-point group ``settings_api.namespaces``: an installed package names a module,
that module supplies ``NAMESPACE`` and ``SETTINGS`` exactly as a built-in does, and it goes
through the identical check -- so a malformed extension fails at import too.

The point is that this repository is public and some services are not. A service nobody
outside a deployment is meant to know about cannot be a module here, because a module here
is its name, its capabilities and its retention policy published. Discovery means the
public side knows only that extension points exist: there is no list to leak and no name
to grep for. See ADR-0011.

An entry point rather than a configuration file because a namespace is code -- defaults,
bounds, validation and prose -- and because a file would mean either shipping a schema
language for settings definitions or handing arbitrary Python to this process at request
time. This way an extension is discovered once, at import, and a broken one stops the
process rather than one request.

## Why this package may import almost nothing

An import-linter contract restricts every module under this package to
:mod:`settings_api.domain.types`. The catalogue is a table, and a table is something a
person can read in one sitting and check against what the services actually do. A
catalogue module that could import the store is a catalogue entry that could have
behaviour, and thirty settings with behaviour are no longer reviewable as a table.

Discovery lives here rather than next door because it is assembly rather than data, and
because the only thing it needs is :mod:`importlib.metadata` -- the standard library's own
answer to "which installed packages extend this one". The contract forbids this package
the rest of ``settings_api``; it does not forbid the standard library, and a rule that did
would be a rule against reading the environment the process is already running in. What it
still forbids, and what matters, is a catalogue module reaching the store.

That restriction is why the lookups that raise this package's domain errors live next
door in :mod:`settings_api.domain.registry` rather than here, and why
:meth:`~settings_api.domain.types.SettingDef.validate` raises a plain ``ValueError``.

## Adding a setting

One entry in one of these modules, then ``make catalogue`` to regenerate
``docs/catalogue.md``. No migration, no schema change, and no deploy of the services that read it --
that is the property the whole design is arranged around, and it is worth protecting.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Protocol

from settings_api.domain.catalogue import (
    common,
    environments,
    keyring,
    lucy,
    memory,
    persona,
    search,
    spotify,
    user,
)

if TYPE_CHECKING:
    from settings_api.domain.types import SettingDef

COMMON = common.NAMESPACE
"""The namespace every service may read. Named here so callers need not spell it."""

ENTRY_POINT_GROUP = "settings_api.namespaces"
"""Where an installed package registers a namespace this repository does not ship.

Named as a constant because it is the public half of the extension point: the string an
extension writes in its own ``pyproject.toml`` has to be the string this process reads,
and two spellings of it is an extension that loads in nobody's deployment.
"""


class NamespaceModule(Protocol):
    """What a namespace module has to offer, built-in or discovered.

    A protocol rather than a base class because a built-in namespace is a plain module and
    a module cannot inherit. It exists so a discovered extension is held to the same shape
    at type-check time as the modules next door, rather than being trusted because it
    arrived through an entry point.
    """

    NAMESPACE: str
    SETTINGS: tuple[SettingDef, ...]


_MODULES: tuple[NamespaceModule, ...] = (
    common,
    keyring,
    user,
    persona,
    memory,
    lucy,
    spotify,
    search,
    environments,
)
"""Every namespace module this repository ships, in the order the documentation presents
them.

``common`` first because everything else is read alongside it; then the two services whose
settings already exist behind a port, then the rest. The order is documentation rather
than behaviour -- nothing resolves differently because of it. Discovered namespaces follow
these, in whatever order the environment reports them.
"""


def _discovered_modules() -> tuple[NamespaceModule, ...]:
    """Every namespace module an installed package registered under the entry-point group.

    Nothing is caught here. An entry point naming a module that will not import, or a
    package that was half-installed, is a deployment that is not the deployment somebody
    configured, and the moment to find that out is startup rather than the first read of a
    namespace that silently is not there.
    """
    return tuple(point.load() for point in entry_points(group=ENTRY_POINT_GROUP))


def _assemble() -> dict[str, tuple[SettingDef, ...]]:
    """Build the catalogue and refuse it if anything about it is wrong.

    Built-in modules first, then discovered ones, checked identically. A discovered module
    gets no latitude a built-in one does not: same namespace rules, same per-entry check,
    same refusal of two modules claiming one namespace.

    Raises:
        ValueError: naming the entry and the rule it broke. Raised at import, so a build
            with a malformed catalogue -- or a malformed extension -- fails to start
            rather than failing per request.
    """
    catalogue: dict[str, tuple[SettingDef, ...]] = {}
    for module in (*_MODULES, *_discovered_modules()):
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
