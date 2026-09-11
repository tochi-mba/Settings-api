"""What a resolved namespace looks like to the service reading it.

The type a consuming service actually touches. Everything about caching, revalidation and
outages is arranged so that this object means the same thing whichever of those paths
produced it -- with one flag, :attr:`ResolvedSettings.stale`, saying which.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from settings_client.errors import SettingsRefused

if TYPE_CHECKING:
    from collections.abc import Mapping

Value = bool | int | str | list[str] | None
"""Every shape a setting value takes. Mirrors settings-api's own five types."""


class OnUnavailable(StrEnum):
    """What to do about one setting when settings-api cannot be reached."""

    USE_DEFAULT = "use_default"
    REFUSE = "refuse"


@dataclass(frozen=True, slots=True)
class Fallback:
    """One setting's outage behaviour, as the server declared it."""

    default: Value
    on_unavailable: OnUnavailable


_MISSING = object()
"""A sentinel, because ``None`` is a legal setting value and cannot mean "absent"."""


@dataclass(frozen=True, slots=True)
class ResolvedSettings:
    """One namespace, resolved for one person, with ``common`` merged underneath.

    Read it like a mapping. The reason it is not simply a ``dict`` is
    :attr:`refused`: during an outage some keys are genuinely unknowable and must not be
    guessed at, and the honest way to express that is for reading one to raise rather than
    for the whole namespace to be missing.
    """

    namespace: str
    values: Mapping[str, Value]
    fallbacks: Mapping[str, Fallback]
    revision: int | None = None
    """``None`` when this was assembled from fallbacks rather than fetched."""

    stale: bool = False
    """Whether this came from cache because settings-api could not be reached.

    A service may want to log it, degrade quietly, or carry on. What it should *not* do is
    treat a stale read as a failure: the values are the person's own, and the most recent
    ones this client saw.
    """

    refused: frozenset[str] = field(default_factory=frozenset)
    """Keys that cannot be resolved and must not be guessed at. Reading one raises."""

    def __getitem__(self, key: str) -> Value:
        """The value for ``key``.

        Raises:
            SettingsRefused: settings-api is unreachable and this key's default is
                permissive, so falling back would override a restriction the person set.
            KeyError: no such setting in this namespace. A programming error rather than
                an operational one -- the key names are fixed by the catalogue.
        """
        if key in self.refused:
            raise SettingsRefused(self.namespace, key)
        return self.values[key]

    def get(self, key: str, default: Any = _MISSING) -> Value:
        """The value for ``key``, or ``default`` if this namespace has no such setting.

        The default is for a key the *catalogue* does not have -- a service reading a
        setting that has been retired, say. It deliberately does **not** swallow
        :class:`~settings_client.errors.SettingsRefused`: a caller that wanted to proceed
        on a guess would be making exactly the decision that flag exists to prevent.
        """
        if key in self.refused:
            raise SettingsRefused(self.namespace, key)
        if key not in self.values:
            if default is _MISSING:
                raise KeyError(key)
            value: Value = default
            return value
        return self.values[key]

    def __contains__(self, key: str) -> bool:
        """Whether this namespace has such a setting at all, refused or not."""
        return key in self.values or key in self.refused
