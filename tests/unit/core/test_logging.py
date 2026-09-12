"""Structured logging, and the redaction pass that runs before anything is rendered.

Two mechanisms keep a setting value out of a log record: no call site passes one, and this
redactor catches the call site that forgot. This file pins the second. It also pins the
``_NamedLogger`` defect: a module-level logger created before ``configure_logging`` ran
must still honour the format that was configured afterwards, or ``LOG_FORMAT=json`` does
nothing and nobody notices until an aggregator chokes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from settings_api.core import logging as logging_module
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

module_logger = get_logger("tests.module_level")
"""Created at import, before any configure_logging call -- exactly like every module in src."""


class TestIsSensitive:
    @pytest.mark.parametrize(
        "field",
        [
            "value",
            "values",
            "value_json",
            "body",
            "detail",
            "document",
            "new_value",
            "old_value",
            "payload",
            "default",
            "settings",
            "resolved",
        ],
    )
    def test_content_fields_are_matched_exactly(self, field: str) -> None:
        assert is_sensitive(field) is True
        assert is_sensitive(field.upper()) is True

    @pytest.mark.parametrize(
        "field",
        ["value_type", "values_logged", "settings_count", "changed_keys", "default_profile_name"],
    )
    def test_near_misses_are_not_content(self, field: str) -> None:
        # "value" as a substring would redact value_type and values_logged, which are
        # metadata worth having and nobody's personal data.
        assert is_sensitive(field) is False

    @pytest.mark.parametrize(
        "field",
        [
            "authorization",
            "Authorization",
            "refresh_token",
            "client_secret",
            "service_token",
            "api_key",
            "x_api_key",
            "password",
            "passwd",
            "passphrase",
            "cookie",
            "private_key",
            "credential",
        ],
    )
    def test_sensitive_names_are_matched_as_substrings(self, field: str) -> None:
        assert is_sensitive(field) is True

    @pytest.mark.parametrize(
        "field", ["namespace", "key", "revision", "action", "count", "request_id", "account_id"]
    )
    def test_the_fields_we_do_log_are_not_sensitive(self, field: str) -> None:
        assert is_sensitive(field) is False


class TestRedaction:
    def test_a_sensitive_top_level_field_is_replaced_wholesale(self) -> None:
        record = redact_secrets(
            None, "info", {"event": "x", "value": "Europe/Lisbon", "key": "timezone"}
        )
        # Wholesale rather than masked: the length and type are themselves information,
        # and `value=None` would reveal that none was sent.
        assert record == {"event": "x", "value": REDACTED, "key": "timezone"}

    def test_nested_sensitive_fields_are_replaced(self) -> None:
        record = redact_secrets(
            None, "info", {"body": {"a": 1}, "meta": {"token": "t", "count": 2}}
        )
        assert record == {"body": REDACTED, "meta": {"token": REDACTED, "count": 2}}

    def test_lists_are_walked(self) -> None:
        record = redact_secrets(None, "info", {"items": [{"password": "p", "n": 1}, "plain"]})
        assert record == {"items": [{"password": REDACTED, "n": 1}, "plain"]}

    def test_a_structure_past_the_depth_limit_is_dropped_whole(self) -> None:
        deep: dict[str, Any] = {"n": 0}
        node = deep
        for level in range(MAX_REDACTION_DEPTH + 2):
            node["child"] = {"n": level + 1}
            node = node["child"]
        record = redact_secrets(None, "info", {"deep": deep})
        # Fail closed: a structure this deep is a bug or an attempt to bury something past
        # the walker, and neither deserves to be rendered.
        rendered = json.dumps(record)
        assert REDACTED in rendered
        assert rendered.count("child") < MAX_REDACTION_DEPTH + 2

    def test_a_non_string_dict_key_is_redacted_wholesale(self) -> None:
        # `{b"value": ...}` stringifies to `"b'value'"`, which matches no content name and
        # would walk straight past a name check. A real escape, closed here.
        record = redact_secrets(None, "info", {"outer": {b"value": "secret", 7: "seven", "ok": 1}})
        assert record == {"outer": {"b'value'": REDACTED, "7": REDACTED, "ok": 1}}

    def test_scalars_pass_through_untouched(self) -> None:
        assert redact_secrets(
            None, "info", {"n": 1, "f": 1.5, "s": "x", "b": True, "none": None}
        ) == {
            "n": 1,
            "f": 1.5,
            "s": "x",
            "b": True,
            "none": None,
        }


class TestContextProcessors:
    def test_absent_rather_than_null_outside_a_request(self) -> None:
        record: dict[str, Any] = {"event": "x"}
        add_request_id(None, "info", record)
        add_account_id(None, "info", record)
        add_service(None, "info", record)
        assert record == {"event": "x"}

    def test_present_when_bound(self) -> None:
        record: dict[str, Any] = {"event": "x"}
        with bind_request_id("req-1"), bind_account_id("acct-1"), bind_service("spotify-api"):
            add_request_id(None, "info", record)
            add_account_id(None, "info", record)
            add_service(None, "info", record)
        assert record == {
            "event": "x",
            "request_id": "req-1",
            "account_id": "acct-1",
            "service": "spotify-api",
        }


class TestTheNamedLoggerDefect:
    def test_a_logger_made_before_configuration_honours_json_configured_after(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The obvious spelling binds eagerly at import and is wired to structlog's
        # defaults for ever, so LOG_FORMAT=json validated, was documented, and did nothing.
        configure_logging(level="INFO", log_format=LogFormat.JSON)
        module_logger.info("probe", key="timezone", value="Europe/Lisbon")
        line = capsys.readouterr().out.strip().splitlines()[-1]
        record = json.loads(line)
        assert record["event"] == "probe"
        assert record["logger"] == "tests.module_level"
        assert record["level"] == "info"
        assert record["value"] == REDACTED
        assert "timestamp" in record

    def test_switching_to_console_takes_effect_on_the_same_logger(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="INFO", log_format=LogFormat.CONSOLE)
        module_logger.info("probe")
        line = capsys.readouterr().out.strip().splitlines()[-1]
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)
        assert "probe" in line

    def test_the_level_filters(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="WARNING", log_format=LogFormat.JSON)
        module_logger.info("quiet")
        module_logger.warning("loud")
        out = capsys.readouterr().out
        assert "quiet" not in out
        assert "loud" in out

    def test_the_request_context_reaches_the_record(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="INFO", log_format=LogFormat.JSON)
        with bind_request_id("req-9"), bind_account_id("acct-9"):
            module_logger.info("probe")
        record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert (record["request_id"], record["account_id"]) == ("req-9", "acct-9")

    def test_exceptions_are_rendered(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", log_format=LogFormat.JSON)

        def explode() -> None:
            msg = "boom"
            raise RuntimeError(msg)

        try:
            explode()
        except RuntimeError:
            module_logger.exception("failed", error_type="RuntimeError")
        record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert "RuntimeError" in record["exception"]

    def test_get_logger_returns_the_proxy(self) -> None:
        assert isinstance(get_logger("x"), logging_module._NamedLogger)
