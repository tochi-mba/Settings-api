"""Operator policy: narrowing the catalogue for one deployment, and never widening it.

The catalogue says what a setting may be anywhere. A deployment sometimes needs to say
something tighter -- a box with a small disk cannot honour a thirty-day artifact
retention, and a deployment serving a small context window cannot honour forty pinned
entries. Policy is where that is written down.

## One direction only

A policy may make a bound **tighter**, change the deployment's default within the bounds
that result, and **pin** a value outright. It may not loosen a bound, add an enum choice,
or raise a cap. The asymmetry is the whole design: narrowing is an operator protecting
their own machine, and widening is an operator handing out a capability the owning service
was never built to honour. A policy that tried to raise ``spotify.max_batch_size`` above
200 would produce settings spotify-api refuses, and the person who set one would have no
way to find out except by the request failing.

Every refusal here is a **startup error**. A deployment with a contradictory policy does
not start, which is the only moment at which somebody is in a position to fix it.

## Pinning is visible

A pinned setting reports ``pinned: true`` from ``describe_settings``, and a write to it is
a 409 that says so. It is never a silent no-op. A person whose change vanished with a 200
and no explanation is the worst outcome this service has: they would believe the thing was
set, and behave accordingly, for as long as it took somebody to notice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, NoReturn, TypeGuard

from settings_api.domain.errors import UnknownNamespaceError, UnknownSettingError
from settings_api.domain.registry import definition_for, entries_in
from settings_api.domain.types import SettingDef, SettingType, Value

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

PIN = "pin"
DEFAULT = "default"
MINIMUM = "minimum"
MAXIMUM = "maximum"
CHOICES = "choices"
MAX_ITEMS = "max_items"

_CLAUSES = frozenset({PIN, DEFAULT, MINIMUM, MAXIMUM, CHOICES, MAX_ITEMS})
"""Everything a policy may say about one setting. Anything else is a typo, and a typo in a
policy file is a narrowing somebody believes is in force and is not."""

_CLAMPING = frozenset({MINIMUM, MAXIMUM, CHOICES, MAX_ITEMS})
"""The clauses that narrow a range, and so require ``operator_clampable``."""


class PolicyError(RuntimeError):
    """The policy file cannot be applied as written.

    Deliberately not a :class:`~settings_api.domain.errors.DomainError`: nothing here is
    about a person or a request, and nothing above should be catching it. It means the
    process should not have started.
    """


@dataclass(frozen=True, slots=True)
class Policy:
    """One deployment's narrowing of the catalogue.

    Empty is the common case and is not a special case: a deployment with no policy file
    gets :data:`NO_POLICY`, whose :meth:`definition_of` returns the catalogue entry
    unchanged and whose :meth:`pin_on` returns ``None``.
    """

    overrides: Mapping[str, SettingDef]
    """Narrowed entries, by ``namespace.key``. Absent means the catalogue entry stands."""

    pins: Mapping[str, Value]
    """Pinned values, by ``namespace.key``."""

    def definition_of(self, definition: SettingDef) -> SettingDef:
        """The entry as this deployment has it, which may be the catalogue's own."""
        return self.overrides.get(definition.qualified, definition)

    def pin_on(self, definition: SettingDef) -> Value | None:
        """The pinned value, or ``None`` if this setting is not pinned.

        ``None`` is unambiguous here despite being a legal setting value, because
        :meth:`is_pinned` is what callers ask when they need to tell "pinned to null" from
        "not pinned" -- and the two paths that care, resolution and the write refusal, ask
        that first.
        """
        return self.pins.get(definition.qualified)

    def is_pinned(self, definition: SettingDef) -> bool:
        """Whether this deployment fixes this setting's value."""
        return definition.qualified in self.pins


NO_POLICY = Policy(overrides={}, pins={})
"""What a deployment with no policy file gets. The catalogue, exactly as written."""


def load(path: Path | None) -> Policy:
    """Read and apply a policy file, or return :data:`NO_POLICY` when there is none.

    Args:
        path: the configured policy file, or ``None``.

    Raises:
        PolicyError: the file is missing, is not JSON, is not an object, or says something
            the catalogue refuses. A configured path that does not exist is an error
            rather than an empty policy: a deployment that meant to narrow something and
            mistyped the path would otherwise run wide open and look correct.
    """
    if path is None:
        return NO_POLICY

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"the policy file at {path} could not be read"
        raise PolicyError(msg) from exc

    try:
        document = json.loads(raw)
    except ValueError as exc:
        msg = f"the policy file at {path} is not valid JSON"
        raise PolicyError(msg) from exc

    return parse(document)


def parse(document: object) -> Policy:
    """Turn a parsed policy document into a :class:`Policy`.

    Separate from :func:`load` so the rules can be tested without a filesystem, and so a
    future policy source -- an environment variable, a config service -- reuses all of
    them rather than reimplementing half.

    Raises:
        PolicyError: naming the setting and the rule it broke.
    """
    if not isinstance(document, dict):
        msg = "a policy must be an object of namespaces"
        raise PolicyError(msg)

    overrides: dict[str, SettingDef] = {}
    pins: dict[str, Value] = {}

    for namespace, settings in document.items():
        _check_namespace(namespace, settings)
        for key, clauses in settings.items():
            definition = _definition(namespace, key)
            _check_clauses(definition, clauses)
            narrowed = _narrow(definition, clauses)
            if narrowed != definition:
                overrides[definition.qualified] = narrowed
            if PIN in clauses:
                pins[definition.qualified] = _checked_pin(narrowed, clauses[PIN])

    return Policy(overrides=overrides, pins=pins)


def _check_namespace(namespace: object, settings: object) -> None:
    """Refuse a namespace a policy names that the catalogue does not have."""
    if not isinstance(namespace, str):
        msg = "a policy's namespaces must be named by strings"
        raise PolicyError(msg)
    try:
        entries_in(namespace)
    except UnknownNamespaceError as exc:
        msg = f"policy names {namespace!r}, which is not a namespace"
        raise PolicyError(msg) from exc
    if not isinstance(settings, dict):
        msg = f"policy for {namespace!r} must be an object of settings"
        raise PolicyError(msg)


def _definition(namespace: str, key: object) -> SettingDef:
    """The catalogue entry a policy clause is about."""
    if not isinstance(key, str):
        msg = f"policy for {namespace!r} names a setting that is not a string"
        raise PolicyError(msg)
    try:
        return definition_for(namespace, key)
    except (UnknownNamespaceError, UnknownSettingError) as exc:
        msg = f"policy names {namespace}.{key}, which is not a setting in this build"
        raise PolicyError(msg) from exc


def _check_clauses(definition: SettingDef, clauses: object) -> None:
    """Refuse a clause this build does not understand, or one the entry cannot take."""
    if not isinstance(clauses, dict):
        msg = f"policy for {definition.qualified} must be an object"
        raise PolicyError(msg)

    unknown = sorted(set(clauses) - _CLAUSES)
    if unknown:
        msg = f"policy for {definition.qualified} has unknown clauses: {', '.join(unknown)}"
        raise PolicyError(msg)

    clamping = sorted(set(clauses) & _CLAMPING)
    if clamping and not definition.operator_clampable:
        msg = (
            f"policy narrows {definition.qualified} with {', '.join(clamping)}, "
            "but that setting is not operator-clampable"
        )
        raise PolicyError(msg)


def _narrow(definition: SettingDef, clauses: Mapping[str, object]) -> SettingDef:
    """Apply the narrowing clauses, refusing anything that widens.

    The default is applied last and validated against the *narrowed* entry, which is the
    order that makes "a policy default outside the bounds is a startup error" true even
    when the same policy moved the bounds.
    """
    narrowed = definition
    if MINIMUM in clauses:
        narrowed = replace(narrowed, minimum=_raised(narrowed, clauses[MINIMUM]))
    if MAXIMUM in clauses:
        narrowed = replace(narrowed, maximum=_lowered(narrowed, clauses[MAXIMUM]))
    if CHOICES in clauses:
        narrowed = replace(narrowed, choices=_subset(narrowed, clauses[CHOICES]))
    if MAX_ITEMS in clauses:
        narrowed = replace(narrowed, max_items=_fewer(narrowed, clauses[MAX_ITEMS]))
    if DEFAULT in clauses:
        narrowed = replace(narrowed, default=_checked_default(narrowed, clauses[DEFAULT]))
    return narrowed


def _whole(definition: SettingDef, clause: str, value: object) -> int:
    """Read a clause that must be a whole number, on a setting that has a range."""
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"policy for {definition.qualified}: {clause} must be a whole number"
        raise PolicyError(msg)
    if definition.value_type is not SettingType.INT and clause in {MINIMUM, MAXIMUM}:
        msg = f"policy for {definition.qualified}: {clause} applies only to whole numbers"
        raise PolicyError(msg)
    return value


def _raised(definition: SettingDef, value: object) -> int:
    """A minimum a policy may only raise."""
    floor = _whole(definition, MINIMUM, value)
    if definition.minimum is not None and floor < definition.minimum:
        msg = (
            f"policy lowers {definition.qualified}'s minimum below the catalogue's "
            f"{definition.minimum}; a policy may only narrow"
        )
        raise PolicyError(msg)
    return floor


def _lowered(definition: SettingDef, value: object) -> int:
    """A maximum a policy may only lower."""
    ceiling = _whole(definition, MAXIMUM, value)
    if definition.maximum is not None and ceiling > definition.maximum:
        msg = (
            f"policy raises {definition.qualified}'s maximum above the catalogue's "
            f"{definition.maximum}; a policy may only narrow"
        )
        raise PolicyError(msg)
    return ceiling


def _subset(definition: SettingDef, value: object) -> tuple[str, ...]:
    """Enum choices a policy may only remove from."""
    if definition.value_type is not SettingType.ENUM:
        msg = f"policy for {definition.qualified}: choices applies only to an enum"
        raise PolicyError(msg)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"policy for {definition.qualified}: choices must be a list of strings"
        raise PolicyError(msg)
    if not value:
        msg = f"policy for {definition.qualified}: choices may not be empty"
        raise PolicyError(msg)
    added = sorted(set(value) - set(definition.choices))
    if added:
        msg = (
            f"policy adds choices to {definition.qualified} that the catalogue does not "
            f"have: {', '.join(added)}"
        )
        raise PolicyError(msg)
    return tuple(choice for choice in definition.choices if choice in set(value))


def _fewer(definition: SettingDef, value: object) -> int:
    """A list cap a policy may only lower."""
    if definition.value_type is not SettingType.STR_LIST:
        msg = f"policy for {definition.qualified}: max_items applies only to a list"
        raise PolicyError(msg)
    cap = _whole(definition, MAX_ITEMS, value)
    if definition.max_items is not None and cap > definition.max_items:
        msg = (
            f"policy raises {definition.qualified}'s max_items above the catalogue's "
            f"{definition.max_items}; a policy may only narrow"
        )
        raise PolicyError(msg)
    return cap


def _checked_default(definition: SettingDef, value: object) -> Value:
    """A deployment default, checked against the entry as narrowed."""
    return _checked(definition, value, clause=DEFAULT)


def _checked_pin(definition: SettingDef, value: object) -> Value:
    """A pinned value, checked against the entry as narrowed."""
    return _checked(definition, value, clause=PIN)


def _checked(definition: SettingDef, value: object, *, clause: str) -> Value:
    """Refuse a policy value the setting's own rules reject.

    The cast is safe because :meth:`SettingDef.validate` refuses anything that is not one
    of the five shapes ``Value`` admits -- it is the check, not a formality after one.
    """
    candidate: Value = value if _is_value(value) else _refuse(definition, clause)
    try:
        return definition.validate(candidate)
    except ValueError as exc:
        msg = f"policy for {definition.qualified}: {clause} is not a value this setting allows"
        raise PolicyError(msg) from exc


def _is_value(value: object) -> TypeGuard[Value]:
    """Whether a parsed JSON value is one of the five shapes a setting may hold.

    A :class:`~typing.TypeGuard` rather than a bare ``bool`` so the caller's ternary
    narrows: without it the value stays ``object`` and the assignment below would need a
    cast, which is the same claim made without the check behind it.
    """
    if isinstance(value, list):
        return all(isinstance(item, str) for item in value)
    return value is None or isinstance(value, bool | int | str)


def _refuse(definition: SettingDef, clause: str) -> NoReturn:
    """Refuse a policy value that is not a settings value at all."""
    msg = f"policy for {definition.qualified}: {clause} is not a value this setting allows"
    raise PolicyError(msg)
