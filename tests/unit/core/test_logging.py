"""The redaction pass, and the promise that the format you configured is the format you get.

Two independent properties live in :mod:`settings_api.core.logging`, and both are the kind
that fail silently.

**No setting value ever reaches a log record** (invariant 10). The first line of defence is
that no call site passes a value to a logger at all, which is a rule about call sites and
cannot be checked here. This file pins the second: a record is walked by field name before
anything is rendered, at every depth, through lists, and -- the part that is easy to leave
out -- through dictionary keys that are not strings, because ``{b"value": ...}`` renders as
``"b'value'"``, which matches no content field name and would otherwise walk straight past
the redactor into the log file. The tests below therefore assert on the *rendered* record
as well as on the redactor's return value: the only thing that matters is what lands in
stdout.

The matching rule is deliberately asymmetric and both halves need pinning. Content names
match **exactly**, so ``value_type`` and ``setting_values_changed`` survive as the metadata
they are; sensitive markers match as **substrings**, so ``refresh_token`` and
``client_secret`` are covered without anybody maintaining a list of every compound. Widen
the first and the logs lose their useful fields; narrow the second and a credential reaches
disk.

**A logger created at import time honours the configuration chosen at startup.** Every
module in ``src/`` does ``logger = get_logger(__name__)`` at import, which is necessarily
before :func:`configure_logging` runs. ``structlog.get_logger().bind(...)`` resolves
eagerly, so the obvious spelling would freeze every one of those loggers to structlog's
defaults and ``SETTINGS_API_LOG_FORMAT=json`` would validate, be documented, and do
nothing -- the exact defect a sibling service shipped. The test for it is the one at the
bottom: a logger built at *this module's* import time, configured afterwards, and its
output fed to :func:`json.loads`.
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any, TypeVar

import pytest
import structlog
from structlog.typing import EventDict

from settings_api.core.config import LogFormat
from settings_api.core.context import bind_account_id, bind_request_id, bind_service
from settings_api.core.logging import (
    MAX_REDACTION_DEPTH,
    REDACTED,
    add_account_id,
    add_request_id,
    add_service,
    configure_logging,
    get_logger,
    is_sensitive,
    redact_secrets,
)

MODULE_LEVEL_LOGGER = get_logger("settings_api.somewhere")
"""Built at import time, exactly as every module under ``src/`` builds its own.

This is the whole point of the last test class: import time is before any call to
:func:`configure_logging`, so a logger that resolved its processor chain here would be
wired to structlog's defaults for the life of the process.
"""

SENTINEL = "the.answer.this.person.chose"
"""A stand-in for a setting value. Distinctive enough to grep a whole stream for."""

T = TypeVar("T")

Fact = Callable[[None, str, EventDict], EventDict]
Binder = Callable[[str], AbstractContextManager[str]]

CONTENT_FIELDS = (
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
"""Spelled out again rather than imported from the module under test.

A test that read ``_CONTENT_FIELDS`` would agree with whatever the list happened to say,
including after somebody deleted an entry from it. This copy is the contract: dropping
``old_value`` from the source has to fail here.
"""

SENSITIVE_NAMES = (
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

COMPOUND_SENSITIVE_NAMES = (
    "refresh_token",
    "client_secret",
    "service_token",
    "x_settings_user_token",
    "set_cookie",
    "authorization_header",
    "user_password",
    "credentials",
    "api_key_id",
    "private_key_pem",
)
"""Why the markers are substrings: nobody has to remember to add these one by one."""

METADATA_NAMES = (
    "value_type",
    "values_logged",
    "setting_values_changed",
    "details",
    "defaults",
    "documents",
    "resolved_at",
    "namespace",
    "key",
    "revision",
    "action",
    "count",
    "changed",
    "event",
    "logger",
    "level",
    "timestamp",
    "request_id",
    "account_id",
    "service",
    "status_code",
    "duration_ms",
)
"""Near misses. Every one of these is worth having in a log record and none is personal."""

FACTS: list[tuple[Fact, Binder, str]] = [
    (add_request_id, bind_request_id, "request_id"),
    (add_account_id, bind_account_id, "account_id"),
    (add_service, bind_service, "service"),
]
FACT_IDS = ["request_id", "account_id", "service"]


@pytest.fixture(autouse=True)
def _structlog_configuration_restored() -> Iterator[None]:
    """Put structlog back where it was found.

    :func:`configure_logging` is process-wide, so a test here that left JSON installed
    would change what an unrelated test later in the session renders. Resetting *before*
    each test matters just as much: the last class asserts what a logger built under
    structlog's defaults does once it is configured, and it would prove nothing if the
    previous test had already configured the library.
    """
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()


def in_a_fresh_context(work: Callable[[], T]) -> T:
    """Run ``work`` with the request-context variables back at their defaults.

    So that "this fact was never bound" means the declared default rather than whatever
    the previously collected test happened to leave behind.
    """
    return contextvars.Context().run(work)


def nest_in_dicts(depth: int, leaf: object) -> object:
    """Wrap ``leaf`` in ``depth`` dictionaries, each under the key ``inner``."""
    node: object = leaf
    for _ in range(depth):
        node = {"inner": node}
    return node


def nest_in_lists(depth: int, leaf: object) -> object:
    """Wrap ``leaf`` in ``depth`` single-element lists."""
    node: object = leaf
    for _ in range(depth):
        node = [node]
    return node


def nest_alternating(depth: int, leaf: object) -> object:
    """Wrap ``leaf`` in ``depth`` containers, alternating a list and a dictionary."""
    node: object = leaf
    for level in range(depth):
        node = [node] if level % 2 else {"inner": node}
    return node


def dig(record: object, depth: int) -> object:
    """Follow ``depth`` ``inner`` keys down into a redacted record."""
    node: object = record
    for _ in range(depth):
        assert isinstance(node, dict), node
        node = node["inner"]
    return node


def redact(**fields: object) -> dict[str, Any]:
    """Run the redaction processor over one record, as structlog would."""
    return dict(redact_secrets(None, "info", dict(fields)))


def lines_of(capsys: pytest.CaptureFixture[str]) -> list[str]:
    """Every non-empty line the logger printed since the last read."""
    return [line for line in capsys.readouterr().out.splitlines() if line]


def one_line(capsys: pytest.CaptureFixture[str]) -> str:
    """The single line the logger printed, asserting that there was exactly one."""
    printed = lines_of(capsys)
    assert len(printed) == 1, printed
    return printed[0]


def one_json_record(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """The single record the logger printed, parsed."""
    parsed: dict[str, Any] = json.loads(one_line(capsys))
    return parsed


class TestWhichFieldNamesAreSensitive:
    """The name is the whole decision, so the boundary between the two lists is the test."""

    @pytest.mark.parametrize("field", CONTENT_FIELDS)
    def test_a_field_that_carries_a_persons_own_decision_is_sensitive(self, field: str) -> None:
        assert is_sensitive(field)

    @pytest.mark.parametrize("field", SENSITIVE_NAMES + COMPOUND_SENSITIVE_NAMES)
    def test_a_field_whose_name_contains_a_credential_marker_is_sensitive(self, field: str) -> None:
        # Substring matching is what covers the compounds. The failure mode of an
        # over-broad rule here is a redacted field nobody needed; the failure mode of a
        # narrow one is a bearer token sitting in a log aggregator for ever.
        assert is_sensitive(field)

    @pytest.mark.parametrize("field", METADATA_NAMES)
    def test_a_name_that_merely_resembles_a_content_field_is_kept(self, field: str) -> None:
        # `value_type`, `values_logged` and `setting_values_changed` are the reason content
        # names are matched exactly. Matching "value" as a substring would redact all three
        # and leave the records describing a write with nothing in them but a count.
        assert not is_sensitive(field)

    @pytest.mark.parametrize("field", CONTENT_FIELDS)
    @pytest.mark.parametrize("suffix", ["_type", "_count", "s_changed"])
    def test_a_content_name_with_a_suffix_is_metadata_rather_than_content(
        self, field: str, suffix: str
    ) -> None:
        assert not is_sensitive(f"{field}{suffix}")

    @pytest.mark.parametrize(
        "field", ["VALUE", "Value", "vAlUe", "BODY", "Authorization", "REFRESH_TOKEN"]
    )
    def test_the_check_ignores_case(self, field: str) -> None:
        # structlog records whatever key the call site typed, and `Authorization` arrives
        # from an HTTP header in exactly that spelling.
        assert is_sensitive(field)

    def test_a_field_with_no_name_is_not_sensitive(self) -> None:
        assert not is_sensitive("")


class TestAValueIsReplacedRatherThanMasked:
    """Wholesale replacement, because the shape of a value is itself information."""

    @pytest.mark.parametrize("field", CONTENT_FIELDS)
    def test_a_content_field_is_replaced_with_the_marker(self, field: str) -> None:
        assert redact(**{field: SENTINEL}) == {field: REDACTED}

    def test_the_marker_keeps_nothing_of_the_original(self) -> None:
        one_character = redact(value="G")
        a_long_one = redact(value=SENTINEL * 10)

        # Not a prefix, not a hash, not a length-preserving mask. `common.timezone` is
        # roughly where somebody lives and `spotify.default_market` is two letters: a
        # marker that kept the length would narrow either one to a handful of candidates.
        assert one_character == a_long_one == {"value": REDACTED}

    @pytest.mark.parametrize(
        "value",
        [SENTINEL, None, "", 0, False, ["GB", "IE"], {"nested": "document"}],
        ids=["string", "none", "empty", "zero", "false", "list", "dict"],
    )
    def test_every_sensitive_value_renders_identically_whatever_it_was(self, value: object) -> None:
        # The two situations that must be indistinguishable: a record for a person who set
        # a value and a record for a person who set none. `value=None` passed through would
        # say "this account has no preference here", which is a fact about them too.
        assert redact(value=value) == {"value": REDACTED}

    @pytest.mark.parametrize(
        "value",
        ["GB", 7, 1.5, True, None, b"bytes"],
        ids=["string", "int", "float", "bool", "none", "bytes"],
    )
    def test_a_field_that_is_not_sensitive_passes_through_untouched(self, value: object) -> None:
        assert redact(namespace=value) == {"namespace": value}

    def test_an_empty_record_stays_empty(self) -> None:
        assert redact() == {}

    def test_the_record_handed_in_is_not_rewritten_in_place(self) -> None:
        event_dict: dict[str, Any] = {"outer": {"value": SENTINEL}, "count": 1}

        redacted = redact_secrets(None, "info", event_dict)

        # The nested structures in a record can be the service's own live objects. A
        # redactor that edited them would destroy the caller's data as a side effect of
        # logging, which is a far worse bug than the one it is preventing.
        assert event_dict == {"outer": {"value": SENTINEL}, "count": 1}
        assert redacted["outer"] == {"value": REDACTED}


class TestTheRedactorWalksTheWholeRecord:
    """A value one level down is the same value. Depth must not be a way out."""

    def test_a_value_inside_a_nested_dictionary_is_redacted(self) -> None:
        assert redact(outer={"value": SENTINEL, "count": 2}) == {
            "outer": {"value": REDACTED, "count": 2}
        }

    def test_a_list_is_walked_item_by_item(self) -> None:
        redacted = redact(changed=[{"value": SENTINEL}, {"key": "default_market"}, "plain"])

        # Lists have no keys of their own, so each item is re-examined as a container
        # rather than inheriting the list's name. The entries that are not dictionaries
        # have to survive intact, or `changed=["a", "b"]` would come out as a list of
        # markers and tell nobody anything.
        assert redacted["changed"] == [{"value": REDACTED}, {"key": "default_market"}, "plain"]

    def test_a_list_inside_a_list_is_walked_too(self) -> None:
        assert redact(rows=[[{"old_value": SENTINEL}]]) == {"rows": [[{"old_value": REDACTED}]]}

    def test_a_sensitive_field_holding_a_list_is_dropped_rather_than_walked(self) -> None:
        # `values` is a content name, so the list goes whole. Walking into it would keep
        # its length -- "this person blocked four providers" is a fact about them.
        assert redact(values=[SENTINEL, SENTINEL]) == {"values": REDACTED}

    @pytest.mark.parametrize("container", [{}, []], ids=["dict", "list"])
    def test_an_empty_container_survives_the_walk(self, container: object) -> None:
        assert redact(changed=container) == {"changed": container}

    @pytest.mark.parametrize("depth", range(MAX_REDACTION_DEPTH + 4))
    def test_a_value_buried_at_any_depth_never_reaches_the_record(self, depth: int) -> None:
        buried = redact(outer=nest_in_dicts(depth, {"value": SENTINEL}))
        in_lists = redact(outer=nest_in_lists(depth, {"value": SENTINEL}))

        # The property the whole module exists for, stated once and checked past the point
        # where the walker gives up: however deeply a call site buries a value, it is not
        # in the rendered record. Rendering is what matters, so this asserts on the JSON
        # text rather than on the structure.
        assert SENTINEL not in json.dumps(buried)
        assert SENTINEL not in json.dumps(in_lists)

    @pytest.mark.parametrize("depth", range(MAX_REDACTION_DEPTH + 4))
    def test_alternating_lists_and_dictionaries_buy_no_extra_depth(self, depth: int) -> None:
        redacted = redact(outer=nest_alternating(depth, {"value": SENTINEL}))

        # One meter for both container types. A walker that counted only dictionaries
        # would descend for ever through `[{"a": [{"a": ...`, and that shape is exactly
        # what somebody who had read half the implementation would build.
        assert SENTINEL not in json.dumps(redacted)


class TestTheRedactorFailsClosed:
    """Where the walker cannot be sure, it drops. Both places that happens are here."""

    @pytest.mark.parametrize("depth", range(MAX_REDACTION_DEPTH))
    def test_a_structure_within_the_depth_limit_is_walked_rather_than_dropped(
        self, depth: int
    ) -> None:
        redacted = redact(outer=nest_in_dicts(depth, {"value": SENTINEL, "count": 3}))

        # Up to the limit the record keeps its shape: the surrounding metadata survives and
        # only the value goes. Dropping a whole sub-tree at depth two would throw away the
        # namespace and key that make a record worth reading.
        assert dig(redacted["outer"], depth) == {"value": REDACTED, "count": 3}

    @pytest.mark.parametrize("extra", range(3))
    def test_a_structure_past_the_depth_limit_is_dropped_whole(self, extra: int) -> None:
        depth = MAX_REDACTION_DEPTH + extra
        redacted = redact(outer=nest_in_dicts(depth, {"value": SENTINEL, "count": 3}))

        # The container at the limit is replaced rather than descended into, which also
        # takes the harmless `count` with it. That is the trade: a structure this deep is
        # either a bug or an attempt to bury something past the walker, and rendering it
        # unexamined is the one outcome that cannot be allowed.
        assert dig(redacted["outer"], MAX_REDACTION_DEPTH) == REDACTED

    @pytest.mark.parametrize("extra", range(3))
    def test_a_list_past_the_depth_limit_is_dropped_whole(self, extra: int) -> None:
        redacted = redact(outer=nest_in_lists(MAX_REDACTION_DEPTH + extra, {"value": SENTINEL}))

        node: object = redacted["outer"]
        for _ in range(MAX_REDACTION_DEPTH):
            assert isinstance(node, list), node
            node = node[0]
        # A list costs a level of depth exactly as a dictionary does. If only dictionaries
        # were counted, a list of lists would be an unbounded walk and a place to hide.
        assert node == REDACTED

    @pytest.mark.parametrize(
        "key",
        [b"value", b"harmless", 42, None, ("value",), 1.5],
        ids=["bytes-content-name", "bytes-other", "int", "none", "tuple", "float"],
    )
    def test_a_dictionary_key_that_is_not_a_string_takes_its_value_with_it(
        self, key: object
    ) -> None:
        redacted = redact(outer={key: SENTINEL})

        # This is a real escape, not a tidiness rule. `{b"value": ...}` stringifies to
        # `"b'value'"` at render time, which matches no content name -- so a redactor that
        # checked the rendered spelling would let it past. Every call site in this service
        # uses string keys, so refusing to reason about anything else costs nothing.
        assert redacted["outer"] == {str(key): REDACTED}
        assert SENTINEL not in json.dumps(redacted)

    def test_a_string_key_beside_a_non_string_one_is_still_judged_on_its_name(self) -> None:
        redacted = redact(outer={b"value": SENTINEL, "count": 4, "value": SENTINEL})

        # One odd key must not turn the whole dictionary into markers: the fail-closed rule
        # is per key, so the metadata sharing the dictionary survives.
        assert redacted["outer"] == {"b'value'": REDACTED, "count": 4, "value": REDACTED}


class TestTheFactsEveryRecordCarries:
    """Request id, account and calling service -- present when known, absent when not."""

    @pytest.mark.parametrize(("processor", "bind", "field"), FACTS, ids=FACT_IDS)
    def test_a_fact_nobody_bound_is_absent_rather_than_null(
        self, processor: Fact, bind: Binder, field: str
    ) -> None:
        record = in_a_fresh_context(lambda: processor(None, "info", {"event": "started"}))

        # Absent, not `None`. Startup and shutdown records have no request behind them, and
        # a null field on every one of them is both noise and a query that cannot be
        # written: `request_id IS NOT NULL` is the filter that separates the two surfaces.
        assert record == {"event": "started"}

    @pytest.mark.parametrize(("processor", "bind", "field"), FACTS, ids=FACT_IDS)
    def test_a_bound_fact_is_attached_to_the_record(
        self, processor: Fact, bind: Binder, field: str
    ) -> None:
        with bind("bound-for-this-request"):
            record = processor(None, "info", {"event": "settings.read"})

        # Equality rather than a membership check: each processor adds its own field and
        # nothing else, so three variables sharing one slot would fail here.
        assert record == {"event": "settings.read", field: "bound-for-this-request"}

    @pytest.mark.parametrize(("processor", "bind", "field"), FACTS, ids=FACT_IDS)
    def test_an_existing_field_is_left_alone(
        self, processor: Fact, bind: Binder, field: str
    ) -> None:
        with bind("bound-for-this-request"):
            record = processor(None, "info", {"event": "settings.read", "namespace": "spotify"})

        assert record["namespace"] == "spotify"

    def test_the_three_facts_compose_into_one_record(self) -> None:
        with bind_request_id("req-1"), bind_account_id("account-a"), bind_service("spotify-api"):
            record = add_service(
                None, "info", add_account_id(None, "info", add_request_id(None, "info", {}))
            )

        # "Who read this person's search preferences, and when" is answerable only if all
        # three land on the same record and stay matched up.
        assert record == {
            "request_id": "req-1",
            "account_id": "account-a",
            "service": "spotify-api",
        }

    @pytest.mark.parametrize(("processor", "bind", "field"), FACTS, ids=FACT_IDS)
    def test_the_facts_survive_the_redactor(
        self, processor: Fact, bind: Binder, field: str
    ) -> None:
        with bind("bound-for-this-request"):
            record = redact_secrets(None, "info", processor(None, "info", {}))

        # The account id is keyring's opaque identifier rather than an email address, so it
        # is safe to record -- and it has to be, or the audit trail redacts itself.
        assert record[field] == "bound-for-this-request"


class TestTheConfiguredRenderer:
    """`log_format` is a deployment's promise to its log aggregator."""

    def test_json_format_produces_one_parseable_object_per_record(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="info", log_format=LogFormat.JSON)

        MODULE_LEVEL_LOGGER.info("settings.read", namespace="spotify", count=3)

        record = one_json_record(capsys)
        assert record["event"] == "settings.read"
        assert record["level"] == "info"
        assert record["namespace"] == "spotify"
        assert record["logger"] == "settings_api.somewhere"
        # Zulu, not a local offset: records from two boxes have to sort as plain strings
        # and mean the same instant.
        assert record["timestamp"].endswith("Z")

    def test_console_format_produces_something_a_person_reads_rather_than_json(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="info", log_format=LogFormat.CONSOLE)

        MODULE_LEVEL_LOGGER.info("settings.read", namespace="spotify")

        printed = one_line(capsys)
        assert "settings.read" in printed
        assert "spotify" in printed
        # The two formats have to be genuinely different, or the setting is decoration. A
        # console line is not a JSON document.
        with pytest.raises(json.JSONDecodeError):
            json.loads(printed)

    @pytest.mark.parametrize("log_format", list(LogFormat), ids=[f.value for f in LogFormat])
    def test_no_renderer_ever_prints_a_setting_value(
        self, log_format: LogFormat, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="info", log_format=log_format)

        MODULE_LEVEL_LOGGER.info(
            "settings.written",
            namespace="spotify",
            key="default_market",
            value=SENTINEL,
            document={"deeper": {"old_value": SENTINEL}},
            changed=[{b"value": SENTINEL}],
        )

        printed = one_line(capsys)
        # The end of the chain, which is the only place worth asserting: whatever the
        # processors did, this is the text that reaches stdout and from there a file.
        assert SENTINEL not in printed
        assert REDACTED in printed
        # The fields that make the record useful have to come through the same pass intact,
        # or "redact everything" would pass this test.
        assert "default_market" in printed

    def test_the_request_context_reaches_the_rendered_record(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="info", log_format=LogFormat.JSON)

        with bind_request_id("req-1"), bind_account_id("account-a"), bind_service("spotify-api"):
            MODULE_LEVEL_LOGGER.info("internal.read")

        record = one_json_record(capsys)
        assert record["request_id"] == "req-1"
        assert record["account_id"] == "account-a"
        assert record["service"] == "spotify-api"

    def test_outside_a_request_the_context_fields_are_missing_entirely(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="info", log_format=LogFormat.JSON)

        in_a_fresh_context(lambda: MODULE_LEVEL_LOGGER.info("service.starting"))

        record = one_json_record(capsys)
        assert "request_id" not in record
        assert "account_id" not in record
        assert "service" not in record

    def test_an_exception_is_rendered_through_the_same_chain(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="info", log_format=LogFormat.JSON)

        try:
            raise ValueError(SENTINEL)  # noqa: TRY301
        except ValueError:
            MODULE_LEVEL_LOGGER.exception("write.failed", namespace="spotify")

        record = one_json_record(capsys)
        # `.exception` goes through the proxy like any other method, and the record it
        # produces is subject to the same processors -- including the redactor.
        assert record["event"] == "write.failed"
        assert "ValueError" in record["exception"]


class TestTheLevelThreshold:
    """A level that did not take effect is a service logging either nothing or everything."""

    @pytest.mark.parametrize("spelling", ["warning", "WARNING", "Warning"])
    def test_the_level_name_is_read_however_it_is_spelled(
        self, spelling: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level=spelling, log_format=LogFormat.JSON)

        MODULE_LEVEL_LOGGER.info("below.the.threshold")
        MODULE_LEVEL_LOGGER.warning("at.the.threshold")

        # `SETTINGS_API_LOG_LEVEL=info` is how a person would write it, and the setting is
        # a plain string with no validator, so the casing has to be forgiven here.
        record = one_json_record(capsys)
        assert record["event"] == "at.the.threshold"

    @pytest.mark.parametrize(
        ("method", "expected"),
        [("debug", False), ("info", True), ("warning", True), ("error", True)],
    )
    def test_records_below_the_configured_level_are_never_rendered(
        self, method: str, expected: bool, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="info", log_format=LogFormat.JSON)

        getattr(MODULE_LEVEL_LOGGER, method)("a.record", value=SENTINEL)

        assert (len(lines_of(capsys)) == 1) is expected

    def test_a_level_nobody_recognises_is_refused_rather_than_guessed(self) -> None:
        # Startup fails loudly instead of quietly picking a default. Guessing would mean a
        # deployment that asked for `warning`, got `debug`, and wrote every record it has
        # to disk -- with the operator believing the opposite.
        with pytest.raises(KeyError):
            configure_logging(level="chatty", log_format=LogFormat.JSON)


class TestALoggerBuiltBeforeStartup:
    """The defect this module was written to avoid, pinned as behaviour.

    ``MODULE_LEVEL_LOGGER`` was built when this file was imported -- before any call to
    :func:`configure_logging`, exactly as every logger in ``src/`` is. If it had resolved
    its processor chain then, it would render structlog's defaults for ever and the
    ``log_format`` setting would be documented, validated, and inert.
    """

    def test_a_logger_built_before_configuration_still_honours_the_chosen_format(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="info", log_format=LogFormat.JSON)

        MODULE_LEVEL_LOGGER.info("settings.read", namespace="spotify")

        # The failing version of this parses nothing: it prints a console line with
        # brackets and colours, an aggregator stores a pile of unparseable text, and
        # nobody finds out until they try to query it.
        record = one_json_record(capsys)
        assert record["event"] == "settings.read"
        assert record["logger"] == "settings_api.somewhere"

    def test_the_same_logger_follows_a_later_reconfiguration(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="info", log_format=LogFormat.CONSOLE)
        MODULE_LEVEL_LOGGER.info("first.record")
        console = one_line(capsys)

        configure_logging(level="info", log_format=LogFormat.JSON)
        MODULE_LEVEL_LOGGER.info("second.record")

        # One logger object, two configurations, and the second one wins. Resolving per
        # call is what costs a dictionary lookup per record and buys this.
        assert "first.record" in console
        assert one_json_record(capsys)["event"] == "second.record"

    def test_a_logger_is_tagged_with_the_name_it_was_asked_for(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="info", log_format=LogFormat.JSON)

        get_logger("settings_api.storage.database").info("query.slow")
        first = one_json_record(capsys)
        get_logger("settings_api.auth.tokens").info("token.rejected")
        second = one_json_record(capsys)

        # The name is bound into the event dict rather than read off the underlying logger,
        # so it survives whichever logger factory is configured -- here a `PrintLogger`,
        # which has no notion of a name at all.
        assert first["logger"] == "settings_api.storage.database"
        assert second["logger"] == "settings_api.auth.tokens"

    def test_a_logger_built_after_configuration_behaves_the_same_way(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="info", log_format=LogFormat.JSON)

        get_logger("built.late").info("settings.read", value=SENTINEL)

        # Both orders have to agree, or the service's behaviour would depend on whether a
        # module happened to be imported before or after startup -- which is precisely the
        # bug that made the format setting inert in the first place.
        record = one_json_record(capsys)
        assert record["value"] == REDACTED
        assert record["logger"] == "built.late"
