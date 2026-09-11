"""The shape of a setting, and the rules one value must satisfy.

This module is the whole vocabulary of the catalogue, and it is deliberately the *only*
thing a catalogue module is allowed to import -- an import-linter contract says so. The
reasoning is worth stating, because the restriction looks arbitrary until it does not:

A catalogue entry is a row in a table, and a table is something a person can read in one
sitting and check. A catalogue module that could import the store is a catalogue entry
that could have behaviour, and the moment one of them does, thirty settings stop being
reviewable and become code. So everything an entry needs to *declare itself* lives here,
including the validation, and nothing an entry might use to *do* something is reachable
from there.

That has one consequence a reader will notice: :meth:`SettingDef.validate` raises a plain
:class:`ValueError` rather than a domain error. Importing
:mod:`settings_api.domain.errors` from here would put it one indirect hop from every
catalogue module, and the contract counts indirect imports. The wrapper that turns a
``ValueError`` into the refusal a caller sees is :mod:`settings_api.domain.values`.

## Why there are five types and not "JSON"

user-api stores arbitrary shallow JSON in a field, because a fact about a person can be
almost anything. A *setting* cannot: every one of them is read by code in another service
that has to branch on it, and a service reading ``concurrent_jobs`` and finding a list has
no sensible thing to do. So the type is declared, the value is checked against it, and a
string that looks like a number stays a string -- ``"true"`` is not ``True`` and ``"5"``
is not ``5``. Coercion here would mean a person setting ``store_query_history`` to the
string ``"false"`` and getting ``True``, which is the single worst failure this service
could have.
"""

from __future__ import annotations

import re
import zoneinfo
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Callable

Value = bool | int | str | list[str] | None
"""Everything a setting may be worth. Deliberately narrow -- see the module docstring."""

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
"""What a setting key may be called.

The same shape as a Python identifier without the leading underscore, so a consuming
service can spell one as an attribute and a JSON document can hold it as a key without
quoting rules mattering.
"""

NAMESPACE_PATTERN = KEY_PATTERN
"""A namespace is named by the same rule as a key. ``common``, ``media``, ``search``."""

MAX_SUMMARY_CHARS = 120
MAX_DESCRIPTION_CHARS = 1_200


class SettingType(StrEnum):
    """What kind of value a setting holds."""

    BOOL = "bool"
    INT = "int"
    STR = "str"
    ENUM = "enum"
    STR_LIST = "str_list"


class OnUnavailable(StrEnum):
    """What a consuming service must do when this service cannot be reached.

    There is no default for the field that carries this, and that is the point: an author
    adding a setting has to decide which of these two it is, because getting it wrong is
    silent and the failure only shows up during an outage.
    """

    USE_DEFAULT = "use_default"
    """Fall back to the catalogue default and carry on.

    Only correct when the default is the *most conservative* value the setting has, so
    falling back can never be a privacy downgrade. ``user.erasure_mode = grace`` destroys
    nothing; ``user.log_values = false`` records nothing. Both are safe to land on.
    """

    REFUSE = "refuse"
    """Fail the operation rather than fall back.

    For settings whose default is permissive because it has to be for the service to
    work at all. ``search.disabled_providers = []`` means "every provider is allowed"; if
    a person has disabled a provider and this service is unreachable, falling back to the
    default would send their query to a provider they explicitly refused. Refusing the
    search is the lesser failure, and it is the only one that is not a broken promise.
    """


class Origin(StrEnum):
    """Whether a setting is a knob that already exists somewhere, or a new proposal."""

    EXISTING = "existing"
    """Lifted from a service's current configuration. Wiring it up is a change at the
    call site and nothing more: the behaviour is already there, applied deployment-wide."""

    PROPOSED = "proposed"
    """New here. Usually needs a change in the owning service before it does anything, and
    ``docs/catalogue.md`` says so per entry, so nobody ships a setting that silently does
    nothing."""


class ExtraCheck(StrEnum):
    """A rule that cannot be written as a bound.

    Kept as an enum rather than a callable so a catalogue entry stays declarative. A
    catalogue that could name a function is a catalogue that could contain one.
    """

    NONE = "none"
    TIMEZONE = "timezone"
    """The value must name a zone :mod:`zoneinfo` can resolve.

    Not a regex, because the set of valid zone names is the tzdata the machine has, and a
    pattern that admitted ``Europe/Lisbon`` would also admit ``Europe/Atlantis``. A
    service that formatted a time against an unresolvable zone would raise deep in a
    render path, a long way from the settings write that caused it.
    """


@dataclass(frozen=True, slots=True)
class SettingDef:
    """One catalogue entry: what a setting is, what it may be, and what it means.

    Frozen and slotted because the catalogue is assembled once at import and read for the
    life of the process. A mutable entry would be a setting whose bounds a request could
    change.
    """

    namespace: str
    key: str
    value_type: SettingType
    default: Value
    summary: str
    """One line, written for a model choosing whether to touch this setting."""

    description: str
    """What choosing each value actually means for the person. Written for a model, and
    for the person reading ``describe_settings`` to decide."""

    on_unavailable: OnUnavailable
    """Required, with no default. See :class:`OnUnavailable`."""

    origin: Origin
    origin_note: str = ""
    """Where this came from: the config attribute it replaces, or why it is new."""

    # -- Bounds ------------------------------------------------------------------------
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    max_chars: int | None = None
    pattern: str | None = None
    max_items: int | None = None
    max_item_chars: int | None = None
    nullable: bool = False
    extra_check: ExtraCheck = ExtraCheck.NONE

    # -- Policy and writability --------------------------------------------------------
    operator_clampable: bool = False
    """Whether a deployment's policy may narrow this setting's range.

    Narrowing only, never widening: an operator may cap ``media.job_retention_hours`` at
    24 on a box with a small disk, and may not raise ``user.max_pinned`` above what the
    service can serve.
    """

    owner_writable_only: bool = False
    """Whether only the person themselves may change this, with a settings-audience token.

    The escape hatch for the cases where "a service may write within its own namespace"
    is not good enough -- ``keyring.require_reauth_for_credential_changes`` above all,
    since a service that could turn that off through the ordinary write path would be a
    service that could turn off the check protecting the credentials it is about to ask
    for. See ADR-0004.
    """

    conservative_values: tuple[Value, ...] = ()
    """The values it is always safe to fall back to.

    Every ``USE_DEFAULT`` entry must declare this and its default must be in it, and a
    test asserts both across the whole catalogue. That pair is what turns "the default is
    conservative" from a claim in a docstring into something a build can check.
    """

    deprecated_by: str | None = None
    """The key that replaces this one, ``namespace.key``. Reads still work."""

    retired_at: str | None = None
    """The ISO date this key left the catalogue, or ``None`` while it is live.

    Rows for a retired key are ignored on read -- so a rollback of the catalogue restores
    them intact -- and are destroyed by the sweeper only once this is further in the past
    than the deployment's retention window.
    """

    labels: tuple[str, ...] = field(default=())
    """Free-form tags for grouping in the generated documentation. No behaviour."""

    @property
    def qualified(self) -> str:
        """``namespace.key``, which is how a setting is named everywhere outside a URL."""
        return f"{self.namespace}.{self.key}"

    @property
    def retired(self) -> bool:
        """Whether this key has left the catalogue."""
        return self.retired_at is not None

    def validate(self, value: Value) -> Value:
        """Return ``value`` if it is a legal value for this setting, or raise.

        Returns the value rather than ``None`` so a caller cannot validate and then use a
        differently-derived copy -- which is two paths that can disagree after somebody
        edits one.

        Raises:
            ValueError: naming the rule that failed and **never echoing the value**. This
                message ends up in a 422 body and in a log line, and the value is the
                thing that must not be in either. :mod:`settings_api.domain.values` turns
                it into the refusal a caller sees.
        """
        if value is None:
            if self.nullable:
                return None
            msg = f"{self.qualified} may not be null"
            raise ValueError(msg)

        checker = _CHECKERS[self.value_type]
        checker(self, value)
        return value

    def check(self) -> None:
        """Refuse an entry that is malformed as an entry, rather than as a value.

        Called for every entry when the catalogue is assembled, so a badly shaped entry
        is an import error in the process that has it rather than a 500 the first time
        somebody reads that namespace.

        Raises:
            ValueError: naming the entry and the rule it broke.
        """
        for rule in _ENTRY_RULES:
            rule(self)


# --------------------------------------------------------------------------------------
# Value checks, one per type
# --------------------------------------------------------------------------------------


def _fail(definition: SettingDef, rule: str) -> NoReturn:
    """Refuse, naming the setting and the rule -- and never the value.

    Annotated ``NoReturn`` so a type checker knows the guard above each call narrows the
    value, which is what lets the checks below be written as early returns rather than as
    a pyramid of ``else``.
    """
    msg = f"{definition.qualified} {rule}"
    raise ValueError(msg)


def _check_bool(definition: SettingDef, value: Value) -> None:
    if not isinstance(value, bool):
        _fail(definition, "must be true or false; a string that spells one is not one")


def _check_int(definition: SettingDef, value: Value) -> None:
    # bool before int, because in Python `True` *is* an `int`. Without this, setting a
    # numeric limit to `true` would store 1 and read back as 1, and the person who typed
    # `true` would never find out.
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(definition, "must be a whole number; a string that spells one is not one")
    if definition.minimum is not None and value < definition.minimum:
        _fail(definition, f"may not be below {definition.minimum}")
    if definition.maximum is not None and value > definition.maximum:
        _fail(definition, f"may not be above {definition.maximum}")


def _check_str(definition: SettingDef, value: Value) -> None:
    if not isinstance(value, str):
        _fail(definition, "must be a string")
    if definition.max_chars is not None and len(value) > definition.max_chars:
        _fail(definition, f"may be at most {definition.max_chars} characters")
    if definition.pattern is not None and not re.fullmatch(definition.pattern, value):
        _fail(definition, "is not in the form this setting requires")
    if definition.extra_check is ExtraCheck.TIMEZONE:
        _check_timezone(definition, value)


def _check_timezone(definition: SettingDef, value: str) -> None:
    """Refuse a zone this machine's tzdata cannot resolve.

    ``ZoneInfoNotFoundError`` is a subclass of :class:`KeyError`, and the other two are
    what a name containing a path separator or a null byte produces -- a zone name is used
    to open a file, so ``../../etc/passwd`` is a shape worth refusing here rather than
    inside :mod:`zoneinfo`.
    """
    try:
        zoneinfo.ZoneInfo(value)
    except (KeyError, ValueError, OSError) as exc:
        msg = f"{definition.qualified} must name a time zone this deployment can resolve"
        raise ValueError(msg) from exc


def _check_enum(definition: SettingDef, value: Value) -> None:
    if not isinstance(value, str):
        _fail(definition, "must be a string")
    if value not in definition.choices:
        _fail(definition, f"must be one of {', '.join(definition.choices)}")


def _check_str_list(definition: SettingDef, value: Value) -> None:
    if not isinstance(value, list):
        _fail(definition, "must be a list of strings")
    if definition.max_items is not None and len(value) > definition.max_items:
        _fail(definition, f"may hold at most {definition.max_items} items")
    for item in value:
        if not isinstance(item, str):
            _fail(definition, "must be a list of strings")
        if definition.max_item_chars is not None and len(item) > definition.max_item_chars:
            _fail(definition, f"may hold items of at most {definition.max_item_chars} characters")
    if len(set(value)) != len(value):
        _fail(definition, "may not repeat an item")


_CHECKERS: dict[SettingType, Callable[[SettingDef, Value], None]] = {
    SettingType.BOOL: _check_bool,
    SettingType.INT: _check_int,
    SettingType.STR: _check_str,
    SettingType.ENUM: _check_enum,
    SettingType.STR_LIST: _check_str_list,
}
"""One check per type. A test asserts this covers every member of :class:`SettingType`.

The exhaustiveness lives in a test rather than in an import-time assertion here, because
an assertion no input can reach is a line the coverage gate can never cover -- and the
answer to that is never a pragma.
"""


# --------------------------------------------------------------------------------------
# Entry checks: rules about an entry, rather than about a value
# --------------------------------------------------------------------------------------


def _entry_names(definition: SettingDef) -> None:
    """The namespace and key must be spellable everywhere they appear."""
    if not NAMESPACE_PATTERN.match(definition.namespace):
        _fail(definition, "has a namespace that is not lowercase snake case")
    if not KEY_PATTERN.match(definition.key):
        _fail(definition, "has a key that is not lowercase snake case")


def _entry_prose(definition: SettingDef) -> None:
    """A setting nobody described is a setting nobody can choose.

    The summary and the description must differ, because a description that merely
    restates the summary is what an author writes when they have not decided what the
    setting means -- and both are read by a model deciding whether to touch it.
    """
    if not definition.summary or len(definition.summary) > MAX_SUMMARY_CHARS:
        _fail(definition, f"needs a summary of 1 to {MAX_SUMMARY_CHARS} characters")
    if not definition.description or len(definition.description) > MAX_DESCRIPTION_CHARS:
        _fail(definition, f"needs a description of 1 to {MAX_DESCRIPTION_CHARS} characters")
    if definition.summary.strip() == definition.description.strip():
        _fail(definition, "has a description that only repeats its summary")


def _entry_bounds(definition: SettingDef) -> None:
    """Bounds must belong to the type they are written on."""
    if definition.value_type is SettingType.ENUM:
        if not definition.choices:
            _fail(definition, "is an enum with no choices")
        if len(set(definition.choices)) != len(definition.choices):
            _fail(definition, "repeats a choice")
    elif definition.choices:
        _fail(definition, "declares choices but is not an enum")

    if definition.value_type is not SettingType.INT and (
        definition.minimum is not None or definition.maximum is not None
    ):
        _fail(definition, "declares a numeric range but is not a whole number")

    if (
        definition.minimum is not None
        and definition.maximum is not None
        and definition.minimum > definition.maximum
    ):
        _fail(definition, "has a minimum above its maximum")

    if definition.pattern is not None and not (
        definition.pattern.startswith("^") and definition.pattern.endswith("$")
    ):
        # Anchored in the text as well as by `fullmatch`, so the pattern a person reads in
        # `describe_settings` means what the server does with it. An unanchored pattern
        # rendered into documentation reads as "contains", which is a different rule.
        _fail(definition, "has a pattern that is not anchored with ^ and $")


def _entry_default(definition: SettingDef) -> None:
    """A default that its own bounds refuse is a setting nobody can leave alone."""
    definition.validate(definition.default)


def _entry_fallback(definition: SettingDef) -> None:
    """A ``USE_DEFAULT`` entry must declare that its default is safe to land on."""
    if definition.on_unavailable is not OnUnavailable.USE_DEFAULT:
        return
    if not definition.conservative_values:
        _fail(definition, "falls back to its default and declares no conservative values")
    if definition.default not in definition.conservative_values:
        _fail(definition, "falls back to a default that it does not call conservative")


def _entry_lifecycle(definition: SettingDef) -> None:
    """A retirement date must be a date, and a replacement must name one."""
    if definition.retired_at is not None:
        try:
            date.fromisoformat(definition.retired_at)
        except ValueError:
            _fail(definition, "has a retired_at that is not an ISO date")
    if definition.deprecated_by is not None and "." not in definition.deprecated_by:
        _fail(definition, "has a deprecated_by that does not name namespace.key")


_ENTRY_RULES: tuple[Callable[[SettingDef], None], ...] = (
    _entry_names,
    _entry_prose,
    _entry_bounds,
    _entry_default,
    _entry_fallback,
    _entry_lifecycle,
)
"""Every rule an entry must satisfy, run by :meth:`SettingDef.check`."""
