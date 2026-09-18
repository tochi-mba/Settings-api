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
"""A namespace is named by the same rule as a key. ``common``, ``search``, ``spotify``."""

ACCOUNT_PROFILE = "*"
"""The profile column value for account-scoped rows.

Not a keyring profile name -- keyring's pattern refuses ``*`` -- so a stored account
row can never collide with a real profile.
"""

PROFILE_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
"""keyring's own profile-name rule, used here so a query parameter this service
accepts is a name keyring will accept. Single-character names are allowed (``a``);
``*`` is not.
"""

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


class AgentAccess(StrEnum):
    """Whether an assistant may change this setting on somebody's behalf.

    A different question from :attr:`SettingDef.owner_writable_only`, which asks which
    *person* may write a setting. This one asks whether a model counts as an acceptable
    writer at all, and the two do not narrow each other. A setting an assistant may never
    touch is still one its owner changes in a single call, which is the ordinary case and
    the one that has to keep working.

    The field carrying this defaults to ``NEVER``, and that default is the whole design.
    The two ways of getting an entry wrong are not symmetrical. Marking something
    assistant-writable that should not be is a privilege an assistant acquires quietly,
    and nobody finds out until it has used it. Marking something ``NEVER`` that could
    safely have been ``FREELY`` is an assistant saying "you will have to change that one
    yourself", which somebody reports the same afternoon. So a setting nobody thought
    about is a setting an assistant may not touch, and the price of that is a sentence in
    a conversation.

    Nothing in this service enforces it, and the documentation says so rather than
    implying otherwise. settings-api cannot tell an assistant's write from a person's:
    both arrive as that person's own token, and inventing a distinction here would mean
    guessing at one. So this is a declaration the assistant hub reads and applies at the
    point where it knows a model is the writer. It is written down here because it is a
    fact about the setting rather than about the hub, and because the table is where
    somebody reviews it.
    """

    NEVER = "never"
    """An assistant may not set this, with a confirmation or without one.

    For anything that widens what an assistant may do, weakens a protection, or shortens
    the window in which somebody can undo something. A confirmation does not rescue these.
    "Shall I stop asking before I act" is exactly the question a person says yes to while
    thinking about something else, and exactly the one an instruction smuggled into a
    document would have an assistant ask.
    """

    WITH_APPROVAL = "with_approval"
    """An assistant may propose a value; a person confirms that particular change.

    For anything that changes what is recorded about somebody, or spends their money. The
    confirmation is of the change and not of the assistant: "you can manage my settings"
    is not approval to set ``memory.write_importance_floor`` to 1.
    """

    FREELY = "freely"
    """An assistant may set this without asking.

    Only where every legal value is a matter of taste -- how a reply reads, how many
    results one call returns, which zone a time is rendered in. Nothing in this class
    changes what is stored about somebody, who may read it, how long it survives, or what
    an assistant is permitted to do.
    """

    @property
    def detail(self) -> str:
        """The same answer as a sentence, saying what to do rather than naming a member.

        Carried in ``describe_settings``, so the rule a client reads and the rule this
        module states are one string rather than two that drift.
        """
        return _AGENT_ACCESS_DETAIL[self]


class SettingScope(StrEnum):
    """Which level a setting's stored value belongs to.

    Exclusive, not layered: a setting is either one value for the account or one value
    per keyring profile, never both. Overlay of the same key would be a second settings
    system (which value wins?), and that is the compromise ADR-0002 refused. Declaring
    the level on the entry keeps one value per row and makes a write to the wrong level
    a 422 that names the fix.

    Defaulted to ``ACCOUNT``. A setting nobody thought about is the same for every
    credential set -- the safe answer for restrictions and identity, and the cheap one
    for everything else. Profile-scoped entries opt in.
    """

    ACCOUNT = "account"
    """One value for the person, regardless of which keyring profile is in use.

    Restrictions, spend ceilings, erasure, and facts about who they are. A work
    profile must not silently weaken a promise made on the account.
    """

    PROFILE = "profile"
    """One value per keyring profile -- work vs personal, two Spotify accounts, two shells.

    Taste, routing, and anything whose correct answer depends on which credential set
    is in use rather than on who the person is.
    """

    @property
    def detail(self) -> str:
        """The same answer as a sentence, carried in ``describe_settings``."""
        return _SCOPE_DETAIL[self]


_SCOPE_DETAIL: dict[SettingScope, str] = {
    SettingScope.ACCOUNT: (
        "One value for this person, the same under every keyring profile. Pass no "
        "profile to write it; a profile query is ignored."
    ),
    SettingScope.PROFILE: (
        "One value per keyring profile. Pass ?profile= with a profile name to read "
        "or write it; omitting the profile reads the catalogue default and refuses a write."
    ),
}


_AGENT_ACCESS_DETAIL: dict[AgentAccess, str] = {
    AgentAccess.NEVER: (
        "An assistant may not set this on somebody's behalf, with a confirmation or "
        "without one. Say what to change and leave the change to the person."
    ),
    AgentAccess.WITH_APPROVAL: (
        "An assistant may propose a value and set it once the person has confirmed that "
        "particular change. A general permission to manage settings is not that "
        "confirmation."
    ),
    AgentAccess.FREELY: (
        "An assistant may set this without asking. Every legal value is a matter of "
        "taste: nothing here changes what is recorded about the person, who may read it, "
        "how long it survives, or what an assistant is allowed to do."
    ),
}
"""One sentence per member. A test asserts it covers every member of :class:`AgentAccess`.

A table rather than the members' own docstrings, because a docstring is stripped under
``-OO`` and is not something a JSON response can carry -- and this string is part of the
response.
"""


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

    Narrowing only, never widening: an operator may cap ``common.job_retention_hours`` at
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

    scope: SettingScope = SettingScope.ACCOUNT
    """Whether this value is one per account or one per keyring profile.

    See :class:`SettingScope`. Defaulted rather than required, for the same reason as
    ``agent_writable``: the unconsidered answer has to be the one that cannot quietly
    split a restriction across profiles.
    """

    agent_writable: AgentAccess = AgentAccess.NEVER
    """Whether an assistant may change this on somebody's behalf. See :class:`AgentAccess`.

    Defaulted rather than required, unlike ``on_unavailable``, and for the opposite
    reason. There both answers are plausible and only the author knows which is right;
    here one of the three is safe whatever the setting turns out to be, so an author who
    says nothing gets the restrictive answer rather than a coin toss.

    It does not narrow ``owner_writable_only`` and is not narrowed by it. A setting marked
    ``NEVER`` is still written by the person it belongs to through the ordinary route, and
    there is a test that says exactly that, so nobody later folds the two into one field.
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


def _entry_agent_access(definition: SettingDef) -> None:
    """An assistant may not be handed more reach over a setting than the entry allows.

    Three rules of one shape: the entry already says somewhere that this setting is
    restricted, and ``agent_writable`` must not quietly contradict it. None of them makes
    a setting less writable by a person -- they stop the two axes drifting apart, and
    nothing else.
    """
    # If only the account owner may write it, with a token they minted for settings
    # itself, then a model acting on their behalf is not who that restriction had in
    # mind. It may still put the change to them; it may not simply make it.
    if definition.owner_writable_only and definition.agent_writable is AgentAccess.FREELY:
        _fail(definition, "is owner-writable only, so an assistant may not write it freely")

    # A retired key is gone from the request surface, so nobody writes it and an assistant
    # certainly does not. Retiring an entry that was assistant-writable has to move this
    # field too, rather than leaving a stale permission in the table for whoever reverts
    # the retirement later.
    if definition.retired and definition.agent_writable is not AgentAccess.NEVER:
        _fail(definition, "is retired, so it is not writable by anyone, an assistant included")

    # A REFUSE entry is one whose default is permissive and whose stored value is a
    # restriction somebody expressed -- that is the whole reason it refuses rather than
    # falling back to that default. An assistant overwriting it unprompted is the failure
    # the refusal exists to prevent, arriving by a different route.
    if (
        definition.on_unavailable is OnUnavailable.REFUSE
        and definition.agent_writable is AgentAccess.FREELY
    ):
        _fail(definition, "holds a restriction it refuses to fall back from; freely is too much")


def _entry_scope(definition: SettingDef) -> None:
    """Account-level facts cannot be stored per profile, and ``common`` is all of those.

    ``common.default_profile`` is the setting that *names* a profile. Storing it per
    profile is circular: to read it you would already need to know which profile you
    were reading for. The rest of ``common`` is the same shape -- where somebody lives,
    what language to write to them in -- and splitting those across profiles would
    reintroduce the silent disagreement this namespace exists to end.
    """
    if definition.namespace == "common" and definition.scope is SettingScope.PROFILE:
        _fail(definition, "is in common, which is always account-scoped")
    if definition.key == "default_profile" and definition.scope is SettingScope.PROFILE:
        _fail(
            definition,
            "names a profile and cannot itself be per-profile; that would be circular",
        )


_ENTRY_RULES: tuple[Callable[[SettingDef], None], ...] = (
    _entry_names,
    _entry_prose,
    _entry_bounds,
    _entry_default,
    _entry_fallback,
    _entry_lifecycle,
    _entry_agent_access,
    _entry_scope,
)
"""Every rule an entry must satisfy, run by :meth:`SettingDef.check`."""
