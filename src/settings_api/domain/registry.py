"""Looking things up in the catalogue, in this package's error vocabulary.

Thin by design. Everything here could have lived in
:mod:`settings_api.domain.catalogue`, and does not for one reason: an import-linter
contract keeps that package restricted to :mod:`settings_api.domain.types`, so a lookup
that raises :class:`~settings_api.domain.errors.UnknownSettingError` cannot live there.
Splitting the table from the lookups is what lets the table stay a table.

The distinction these functions draw is worth stating once, because it is the same
distinction three layers above will make again: **an unknown namespace and an ungranted
namespace are different answers, and both are safe to give.** The catalogue is identical
in every deployment of a build and is published in full by ``describe_settings`` to
anybody holding a token, so "there is no such namespace" reveals nothing. Conflating the
two would send a caller with a typo looking for a permissions problem it does not have.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from settings_api.domain.catalogue import BY_QUALIFIED, CATALOGUE, COMMON, NAMESPACES
from settings_api.domain.errors import UnknownNamespaceError, UnknownSettingError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from settings_api.domain.types import SettingDef

__all__ = [
    "BY_QUALIFIED",
    "CATALOGUE",
    "COMMON",
    "NAMESPACES",
    "definition_for",
    "entries_in",
    "every_entry",
    "live_entries_in",
]


def every_entry() -> Iterator[SettingDef]:
    """Every entry in the catalogue, namespace by namespace, in declared order."""
    for entries in CATALOGUE.values():
        yield from entries


def entries_in(namespace: str) -> tuple[SettingDef, ...]:
    """Every entry in one namespace, retired ones included.

    Retired entries are included here because this is the lookup the sweeper and the
    documentation generator use, and both need to see what has left. Request paths want
    :func:`live_entries_in`.

    Raises:
        UnknownNamespaceError: naming what this build does have.
    """
    entries = CATALOGUE.get(namespace)
    if entries is None:
        msg = f"no namespace {namespace!r}; this build has {', '.join(sorted(NAMESPACES))}"
        raise UnknownNamespaceError(msg)
    return entries


def live_entries_in(namespace: str) -> tuple[SettingDef, ...]:
    """Every entry in one namespace that has not been retired.

    Raises:
        UnknownNamespaceError: naming what this build does have.
    """
    return tuple(entry for entry in entries_in(namespace) if not entry.retired)


def definition_for(namespace: str, key: str) -> SettingDef:
    """One entry, by namespace and key.

    Checks the namespace first, so a caller that misspelled the namespace is told that
    rather than told the key does not exist in a namespace that also does not exist.

    Raises:
        UnknownNamespaceError: no such namespace.
        UnknownSettingError: the namespace exists and has no such key. Retired keys are
            *not* found here: a retired key is gone from the request surface, and its
            stored rows survive only so that reverting the catalogue restores them.
    """
    entries_in(namespace)
    entry = BY_QUALIFIED.get(f"{namespace}.{key}")
    if entry is None or entry.retired:
        msg = f"no setting {key!r} in the {namespace} namespace; see describe_settings"
        raise UnknownSettingError(msg)
    return entry
