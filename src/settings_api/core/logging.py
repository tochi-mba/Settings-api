"""Structured logging, with a redaction pass that runs before anything is rendered.

JSON in deployment so records are queryable, pretty console output locally. Every record
carries the request id, the authenticated account when there is one, and the calling
service on the internal surface -- which is what makes "who read this person's search
preferences, and when" answerable after the fact.

## What must never be recorded

A setting **value** is the person's own decision about their own data, and some of them
are more revealing than they look: ``search.disabled_providers`` is a list of companies
somebody refuses to send their queries to, and ``common.timezone`` is roughly where they
live. So log the ``namespace``, the ``key``, the ``revision``, the ``action`` and counts.
Never the value, never the document, never the old value.

There are two mechanisms and both are needed:

* **No call site passes a value to a logger at all.** That is a rule about call sites
  rather than about this module.
* **The redactor catches the call site that forgot**, by field name, at every depth,
  before anything is rendered.

Belt and braces. There is a test that drives a request whose body contains a sentinel
value and asserts it appears in no log record, on the success path and on every failure
path. Note what else that covers: :class:`fastapi.exceptions.RequestValidationError` is
reshaped by hand in :mod:`settings_api.api.errors`, because FastAPI's own handler echoes
the offending **input**; and an unhandled exception is rendered by type name only,
because a ``ValueError`` raised in a write path routinely carries the value in its
message.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import structlog

from settings_api.core.config import LogFormat
from settings_api.core.context import get_account_id, get_request_id, get_service

if TYPE_CHECKING:
    from structlog.typing import EventDict, Processor, WrappedLogger

REDACTED = "[redacted]"
"""What a sensitive value is replaced with. A constant so tests can assert on it."""

MAX_REDACTION_DEPTH = 6
"""How far into nested structures the redactor walks before giving up and dropping."""

_SENSITIVE_SUBSTRINGS = (
    "authorization",
    "cookie",
    "credential",
    "passphrase",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
    "api_key",
)
"""Substrings that make a field name sensitive wherever it appears.

Matched as substrings rather than exact names so ``refresh_token``, ``client_secret`` and
``service_token`` are covered without maintaining a list of every compound. The failure
mode of an over-broad rule is a redacted field that did not need it; the failure mode of
a narrow one is a credential in a log file.
"""

_CONTENT_FIELDS = (
    "body",
    "default",
    "detail",
    "document",
    "new_value",
    "old_value",
    "payload",
    "resolved",
    "settings",
    "value",
    "value_json",
    "values",
)
"""Field names that carry a person's own decision, and must never be recorded.

This list is the *second* line of defence. The first is that no call site passes a
setting value to a logger at all -- see the module docstring. Anything here is redacted
whether or not a call site remembered, which is what makes the sentinel test pass rather
than merely making it likely to.
"""


def is_sensitive(field: str) -> bool:
    """Whether a field name means the value must not be recorded.

    Content field names are matched *exactly* rather than as substrings, because "value"
    as a substring would also redact ``value_type``, ``values_logged`` and
    ``setting_values_changed`` -- which are metadata worth having, and none of which is
    anybody's personal data.
    """
    lowered = field.lower()
    if lowered in _CONTENT_FIELDS:
        return True
    return any(marker in lowered for marker in _SENSITIVE_SUBSTRINGS)


def redact_secrets(
    _logger: WrappedLogger | None,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Replace every sensitive value in the record, however deeply it is nested."""
    return {key: _redact_value(key, value, depth=0) for key, value in event_dict.items()}


def _redact_value(key: str, value: object, *, depth: int) -> Any:
    """Redact one key/value pair, recursing into containers."""
    if is_sensitive(key):
        # Replaced wholesale rather than masked: the length and type of a value are
        # themselves information, and `value=None` would reveal that none was sent.
        return REDACTED
    return _redact_container(value, depth=depth)


def _redact_container(value: object, *, depth: int) -> Any:
    """Walk into a dict or list, or return the value untouched."""
    if not isinstance(value, dict | list):
        return value

    if depth >= MAX_REDACTION_DEPTH:
        # Fail closed. A structure this deep is either a bug or an attempt to bury
        # something past the walker, and neither deserves to be rendered.
        return REDACTED

    if isinstance(value, dict):
        # A key that is not a string is redacted wholesale rather than rendered and then
        # name-checked. `{b"value": ...}` stringifies to `"b'value'"`, which matches no
        # content name and would have walked straight past the redactor. Every call site
        # in this service uses string keys, so failing closed here costs nothing and
        # removes a way in.
        return {
            str(key): (
                _redact_value(key, item, depth=depth + 1) if isinstance(key, str) else REDACTED
            )
            for key, item in value.items()
        }
    return [_redact_container(item, depth=depth + 1) for item in value]


def add_request_id(
    _logger: WrappedLogger | None,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Attach the bound request id, if there is one.

    Absent outside a request rather than present-and-null, so queries can filter on
    existence.
    """
    request_id = get_request_id()
    if request_id is not None:
        event_dict["request_id"] = request_id
    return event_dict


def add_account_id(
    _logger: WrappedLogger | None,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Attach the authenticated account, if the request has one.

    The account id is keyring's opaque identifier, not an email address -- this service
    never learns one. It is safe to record, and it is what makes "what did this person
    change" answerable without a log record ever naming them.
    """
    account_id = get_account_id()
    if account_id is not None:
        event_dict["account_id"] = account_id
    return event_dict


def add_service(
    _logger: WrappedLogger | None,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Attach the calling service on the internal surface, if there is one."""
    service = get_service()
    if service is not None:
        event_dict["service"] = service
    return event_dict


def configure_logging(*, level: str, log_format: LogFormat) -> None:
    """Configure structlog process-wide. Safe to call again to change the configuration."""
    shared: list[Processor] = [
        structlog.processors.add_log_level,
        add_request_id,
        add_account_id,
        add_service,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        # Last before rendering, so it also covers anything the processors above added.
        redact_secrets,
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if log_format is LogFormat.JSON
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


class _NamedLogger:
    """A logger that resolves against the configuration in force *when it is called*.

    This exists because of a defect in a sibling service, and the defect is worth writing
    down because the obvious spelling has it.

    ``structlog.get_logger().bind(logger=name)`` binds **eagerly**: the proxy
    ``get_logger()`` returns is lazy, but ``.bind()`` resolves it there and then against
    whatever configuration is in force, and the resulting logger keeps that processor
    chain for ever. Every module here does ``logger = get_logger(__name__)`` at import
    time, which is necessarily *before* :func:`configure_logging` has run -- so every
    module-level logger in the service would be permanently wired to structlog's
    defaults.

    The symptom was the bad kind. ``USER_API_LOG_FORMAT=json`` validated, was documented,
    and did nothing: every record still came out in the human-readable console format,
    and a deployment shipping those to an aggregator would have had a pile of unparseable
    lines and no clue why. Only loggers created *after* startup honoured the setting, and
    there are none.

    Resolving per call fixes it and costs a dictionary lookup and a bind on each record,
    at a volume of a few records per request. The alternative -- deferring every module's
    logger behind a function call -- puts the burden on every call site instead, which is
    a rule that holds until somebody forgets it.
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, method: str) -> Any:
        # The bind happens here, on the way to `.info` or `.exception`, so it picks up
        # whatever configure_logging last installed.
        return getattr(structlog.get_logger().bind(logger=self._name), method)


def get_logger(name: str) -> Any:
    """Return a logger tagged with ``name``.

    The name is bound into the event dict rather than read off the underlying logger, so
    it survives regardless of which logger factory is configured.

    The return type is deliberately loose: structlog's filtering bound loggers are
    generated at configuration time and have no single static type.
    """
    return _NamedLogger(name)
