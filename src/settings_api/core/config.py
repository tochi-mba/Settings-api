"""Application configuration.

Every knob is an environment variable prefixed ``SETTINGS_API_``. Unknown variables under
the prefix are rejected rather than ignored (see :func:`check_for_unknown_env_vars`), so a
typo in a deployment surfaces at startup instead of silently leaving a security-relevant
default in place.

## What is deliberately absent

Keyring and user-api each keep a list of the settings they refused to add, and the habit
is worth keeping here most of all -- this is the service whose entire job is settings, so
"add a setting" is the path of least resistance for every bad idea. An absent setting is
invisible in a diff and a present one is a single line away from being set. There is:

* no setting that disables token verification;
* no setting that accepts an unsigned or HS256 token;
* no setting that lets one account read another's;
* no setting that turns off the credential refusal on values;
* no setting naming an account id, anywhere;
* no per-profile setting, and no way to make one -- see ADR-0002;
* no person-settable ``allow_private_networks``, ``respect_robots`` or
  ``require_authentication``. A person cannot turn off SSRF protection, robots compliance
  or authentication from here. Those stay operator-only, in the service that owns them;
* no administrative HTTP surface at all. No operator can read somebody's settings over
  HTTP, because no route exists that would let them -- see ADR-0008.

Each of those would be a one-variable route past the property the service is built
around. They are not configuration; they are the service.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from settings_api.domain.registry import NAMESPACES

if TYPE_CHECKING:
    from collections.abc import Mapping

ENV_PREFIX = "SETTINGS_API_"
ENV_NESTED_DELIMITER = "__"

PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]

MIN_SERVICE_TOKEN_CHARS = 32
"""Short enough to type and long enough that guessing one is not a strategy.

Checked at startup rather than trusted, because a service token is the entire proof that
a caller on ``/v1/internal`` is a service at all, and a deployment that pasted a
placeholder would look exactly like a working one.
"""


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


def _check_audience_prefix(value: str) -> str:
    """Refuse a prefix that could not appear at the head of an audience.

    The audience is ``{prefix}`` or ``{prefix}.{scope}``, so a prefix containing a dot
    would make one family parse as another. Surrounding whitespace is refused too, and
    that clause is here because it was once on only one of the two validators that need
    it: ``SETTINGS_API_AUDIENCE_PREFIX=" settings"`` -- a copy-paste out of a YAML block --
    matches no audience keyring ever mints, so every person-facing request becomes a 401
    with nothing in the configuration looking wrong.

    One function rather than two identical methods, so the person-facing prefix and a
    service's prefix cannot drift into refusing different things.
    """
    if not value or "." in value or value.strip() != value:
        msg = f"audience_prefix {value!r} must be non-empty, trimmed, and contain no dot"
        raise ValueError(msg)
    return value


def _check_namespace_names(names: tuple[str, ...], *, field: str) -> tuple[str, ...]:
    """Refuse namespace names that are not in the catalogue, or that repeat.

    Checked at startup, where it is a typo, rather than at request time, where it is a
    service that silently reads nothing. An unknown name in a service's grant is the
    worse of the two: the service would authenticate, ask for its namespace, and be told
    403 by a deployment that believed it had been granted one.
    """
    if not names:
        msg = (
            f"{field} is empty, so no token could grant anything; omit it to get every "
            "namespace this build has"
        )
        raise ValueError(msg)
    for name in names:
        if name not in NAMESPACES:
            known = ", ".join(sorted(NAMESPACES))
            msg = f"{field} names {name!r}, which is not a namespace; this build has {known}"
            raise ValueError(msg)
    if len(set(names)) != len(names):
        msg = f"{field} contains a duplicate"
        raise ValueError(msg)
    return names


class ServiceConfig(BaseModel):
    """One consuming service: how it proves who it is, and what it may see.

    Three fields, and the middle one is the one that matters. ``audience_prefix`` is the
    audience family the *end user's* token must belong to, and it is what stops a
    compromised service from reading anybody's settings: media-tool may only present
    tokens minted for media-tool. Without it, anything able to reach this service with any
    service token could read any account -- the confused deputy, moved from inside one
    process into the gap between two.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    token: SecretStr = Field(description="The service's own bearer token.")
    audience_prefix: str = Field(
        description="The audience family the end user's token must belong to.",
    )
    namespaces: tuple[str, ...] = Field(
        description="Which namespaces this service may read and write. `common` is added.",
    )

    @field_validator("audience_prefix")
    @classmethod
    def _check_audience_prefix(cls, value: str) -> str:
        """Refuse a prefix that could not appear at the head of an audience."""
        return _check_audience_prefix(value)

    @field_validator("namespaces")
    @classmethod
    def _check_namespaces(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            # Checked here rather than left to the shared helper so the message names the
            # fix: a service granted nothing is a configuration mistake, and the answer is
            # to remove the service rather than to widen it.
            msg = "a service with no namespaces could read nothing; omit it instead"
            raise ValueError(msg)
        return _check_namespace_names(value, field="namespaces")

    @field_validator("token")
    @classmethod
    def _check_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_SERVICE_TOKEN_CHARS:
            msg = f"a service token must be at least {MIN_SERVICE_TOKEN_CHARS} characters"
            raise ValueError(msg)
        return value


class Settings(BaseSettings):
    """The complete runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=ENV_NESTED_DELIMITER,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    # -- Identity ----------------------------------------------------------------------
    app_name: str = "settings"
    environment: str = "local"

    # -- Observability -----------------------------------------------------------------
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # -- Serving -----------------------------------------------------------------------
    host: str = "127.0.0.1"
    """Loopback by default. This service belongs behind a TLS-terminating proxy."""

    port: PositiveInt = 8003

    # -- Storage -----------------------------------------------------------------------
    database_path: Path = Path("var/settings.db")
    """The one file everything lives in.

    Nothing in it is encrypted. The file mode is therefore the only thing between these
    decisions and every other process on the box -- see
    :mod:`settings_api.storage.database` for how 0600 is applied.
    """

    # -- Who we believe, and why -------------------------------------------------------
    keyring_jwks_url: str = "http://127.0.0.1:8001/.well-known/jwks.json"
    """Where keyring publishes the public half of its signing key.

    Fetched lazily and cached. This service never calls keyring at request time to ask
    *about* an account -- it only ever fetches keys, and only when it meets a ``kid`` it
    has not seen. Note what this does **not** require: no change to keyring, no token
    exchange, and no new keyring endpoint. A user token presented by a service is
    verified here against this same document, with that service's audience.
    """

    keyring_issuer: str = "http://127.0.0.1:8001"
    """Pinned against every token's ``iss``. A token from anywhere else is not a token."""

    audience_prefix: str = "settings"
    """The family of audiences the person-facing surface answers to.

    ``settings`` grants every namespace; ``settings.search`` grants exactly the ``search``
    namespace -- which is a genuinely useful compartment: an assistant that may set search
    preferences but may not touch erasure policy.
    """

    allowed_namespaces: tuple[str, ...] = tuple(sorted(NAMESPACES))
    """Every namespace an audience may name.

    Configuration rather than an open set, so an audience naming an unknown namespace is a
    401 rather than a token that silently grants nothing and reads as a correctly
    configured assistant that just has not been told anything yet.
    """

    services: dict[str, ServiceConfig] = Field(default_factory=dict)
    """The consuming services, by name, as one JSON document.

    One variable rather than a nested tree, because the nested spelling cannot be
    enumerated -- and a configuration this service cannot enumerate is one
    :func:`check_for_unknown_env_vars` cannot check for typos. Set it as::

        SETTINGS_API_SERVICES='{"spotify-api": {"token": "...",
            "audience_prefix": "spotify-api", "namespaces": ["spotify"]}}'

    For a service that also calls keyring's internal surface with the same user token, the
    prefix must be exactly that service's name in keyring's ``KEYRING_SERVICE_TOKENS``,
    because keyring accepts the token only when its audience is that name.

    Empty by default. A deployment that has configured no services has a working
    person-facing surface and a ``/v1/internal`` that refuses everything, which is the
    right thing for a service nobody has been told to trust yet.
    """

    jwks_cache_seconds: PositiveFloat = 3_600.0
    jwks_min_refetch_seconds: PositiveFloat = 60.0
    """Floor between two fetches provoked by an unknown ``kid``.

    Without it, a stream of tokens carrying random ``kid`` values is one outbound fetch
    per request -- a DoS amplifier aimed at keyring, triggerable by anyone who can reach
    this service unauthenticated. There is a test for exactly that.
    """

    keyring_http_timeout_seconds: PositiveFloat = 5.0

    # -- Operator policy ---------------------------------------------------------------
    policy_path: Path | None = None
    """A JSON file narrowing or pinning catalogue entries for this deployment.

    Absent by default, which means the catalogue stands as written. A policy may make a
    bound tighter or pin a value; it may never widen one. See
    :mod:`settings_api.domain.policy`.
    """

    # -- Limits ------------------------------------------------------------------------
    max_events: PositiveInt = 2_000
    """Per account. The log is trimmed to this in the same transaction that appends."""

    max_value_bytes: PositiveInt = 4_096
    """The serialized size of one setting value, whatever the catalogue's own bounds say.

    A backstop rather than a per-setting rule: the catalogue bounds a list to sixty items
    of sixty-four characters, and this is what stops a type whose bounds somebody forgot
    to write from being a place to put a megabyte.
    """

    cache_ttl_seconds: NonNegativeInt = 60
    """What a consuming service is told to cache a resolved namespace for.

    Advertised in ``Cache-Control`` on ``/v1/internal``. Revalidation is cheap -- an ETag
    and a 304 -- so this trades a minute of staleness for most of the request volume.
    """

    # -- Retention ---------------------------------------------------------------------
    retired_retention_days: NonNegativeInt = 90
    """How long a retired key's rows survive before the sweeper destroys them.

    Not zero, and this is the whole point of the field. Deleting on sight would destroy a
    person's expressed choice the instant somebody merged a catalogue typo, with no way
    back; ninety days is long enough to notice and revert, and a rollback of the catalogue
    restores the rows intact because reads merely ignore them.
    """

    sweep_interval_seconds: PositiveFloat = 3_600.0

    @field_validator("database_path")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        """Resolve early so a relative path cannot mean two places after a chdir."""
        return value.expanduser().resolve()

    @field_validator("policy_path")
    @classmethod
    def _resolve_policy_path(cls, value: Path | None) -> Path | None:
        return None if value is None else value.expanduser().resolve()

    @field_validator("audience_prefix")
    @classmethod
    def _check_audience_prefix(cls, value: str) -> str:
        return _check_audience_prefix(value)

    @field_validator("allowed_namespaces")
    @classmethod
    def _check_allowed_namespaces(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _check_namespace_names(value, field="allowed_namespaces")

    @model_validator(mode="after")
    def _check_services(self) -> Self:
        """Refuse two services sharing a token, and a service granted nothing readable.

        Two services sharing a token is the failure that makes the audience-family check
        meaningless: whichever name matched first would decide which audience is
        acceptable, so the weaker of the two grants would be reachable with the other's
        token.
        """
        tokens = [service.token.get_secret_value() for service in self.services.values()]
        if len(set(tokens)) != len(tokens):
            msg = "two services share a service token; each needs its own"
            raise ValueError(msg)

        for name, service in self.services.items():
            unreadable = sorted(set(service.namespaces) - set(self.allowed_namespaces))
            if unreadable:
                msg = (
                    f"service {name!r} is granted {', '.join(unreadable)}, which "
                    "allowed_namespaces does not include"
                )
                raise ValueError(msg)
        return self


class UnknownSettingError(ValueError):
    """A ``SETTINGS_API_``-prefixed variable is set that no setting corresponds to."""


def known_env_names(model: type[BaseModel] = Settings, prefix: str = ENV_PREFIX) -> set[str]:
    """Every environment variable name this configuration understands.

    Walks nested settings models, so a nested name is recognised alongside the flat ones.
    """
    names: set[str] = set()
    for field_name, field in model.model_fields.items():
        env_name = f"{prefix}{field_name.upper()}"
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            names |= known_env_names(annotation, f"{env_name}{ENV_NESTED_DELIMITER}")
        else:
            names.add(env_name)
    return names


def check_for_unknown_env_vars(environ: Mapping[str, str] | None = None) -> None:
    """Fail on a misspelled setting instead of quietly running with the default.

    pydantic-settings ignores prefixed variables it does not recognise, which for most
    services is a harmless convenience. Here it is not: ``SETTINGS_API_ALOWED_NAMESPACES``
    would leave the namespace list on its default with nothing in the logs to say so, and
    a deployment that believed it had compartmentalised its services would not have.

    Raises:
        UnknownSettingError: naming every unrecognised variable, so a deployment is fixed
            in one pass rather than one restart per typo.
    """
    present = environ if environ is not None else os.environ
    known = known_env_names()
    # Compared case-insensitively, because pydantic-settings is. Its `case_sensitive`
    # defaults to False, so `settings_api_port=9999` really does set the port -- and a
    # check that only looked at upper-case names would let `settings_api_alowed_namespaces`
    # straight past, which is the exact failure this function exists to prevent. The two
    # have to agree about what a name is.
    unknown = sorted(
        name
        for name in present
        if name.upper().startswith(ENV_PREFIX) and name.upper() not in known
    )
    if unknown:
        msg = f"unknown {ENV_PREFIX}* environment variables: {', '.join(unknown)}"
        raise UnknownSettingError(msg)


class ConfigurationError(ValueError):
    """The configuration was rejected, and this is why -- without the values.

    pydantic's own :class:`~pydantic.ValidationError` renders the **input** beside each
    failure, and a startup crash is normally logged verbatim. For most settings that is
    helpful. For ``services``, it pastes every configured service token into the log --
    the one place the ``SecretStr`` wrapper exists to keep them out of, and it does not
    help here because pydantic reports the raw input to the field, not the coerced value.

    So :func:`load_settings` catches the validation error and re-raises this one, carrying
    only each failure's location and message, exactly as :mod:`settings_api.api.errors`
    reshapes a request validation error before it reaches a caller. The chain is cut with
    ``from None`` so the original, values included, is not attached to the traceback.
    """


def describe_validation_error(exc: ValidationError) -> str:
    """Render a validation error as locations and messages only. Never the input."""
    problems = (
        f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
        for error in exc.errors()
    )
    return "invalid configuration: " + "; ".join(problems)


def load_settings() -> Settings:
    """Build settings from the environment and ``.env``.

    Raises:
        UnknownSettingError: a ``SETTINGS_API_``-prefixed variable matches no setting.
        ConfigurationError: a setting was rejected. Names the setting and the rule, and
            never the value -- see the class.
    """
    check_for_unknown_env_vars()
    try:
        return Settings()
    except ValidationError as exc:
        raise ConfigurationError(describe_validation_error(exc)) from None
