"""Turning a value somebody sent into a value this service will store.

The order of the three checks -- shape, then credentials, then size -- decides what a
caller is told when more than one rule would fire, and every ordering test here exists
because the other order sends a caller round the loop with a worse hint.

The JSON rules are pinned separately: ``allow_nan=False`` is the difference between a value
this service can read back and a value every other reader in the world cannot.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from settings_api.domain import values
from settings_api.domain.errors import CredentialRefusedError, InvalidSettingValueError
from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingType


def entry(value_type: SettingType, default: Any, **overrides: Any) -> SettingDef:
    fields: dict[str, Any] = {
        "namespace": "demo",
        "key": "knob",
        "value_type": value_type,
        "default": default,
        "summary": "A knob.",
        "description": "What the knob does.",
        "on_unavailable": OnUnavailable.REFUSE,
        "origin": Origin.PROPOSED,
    }
    fields.update(overrides)
    return SettingDef(**fields)


TEXT = entry(SettingType.STR, "x")
NAMES = entry(SettingType.STR_LIST, [])
NUMBER = entry(SettingType.INT, 1, minimum=0, maximum=100)
FLAG = entry(SettingType.BOOL, False)
BIG = 4_096


class TestTheHappyPath:
    @pytest.mark.parametrize(
        ("definition", "value"),
        [
            (TEXT, "hello"),
            (TEXT, "日本語"),
            (NAMES, []),
            (NAMES, ["a", "b"]),
            (NUMBER, 0),
            (NUMBER, 100),
            (FLAG, True),
            (entry(SettingType.STR, None, nullable=True), None),
        ],
    )
    def test_a_legal_value_comes_back_unchanged(self, definition: SettingDef, value: Any) -> None:
        # Returned rather than None, so a caller cannot validate and then store a
        # separately-derived copy -- two paths that disagree after somebody edits one.
        assert values.validate(definition, value, max_bytes=BIG) == value


class TestShapeComesFirst:
    def test_a_wrong_type_is_an_invalid_value_in_this_packages_vocabulary(self) -> None:
        # types.py raises a bare ValueError because the catalogue contract keeps it one
        # hop from domain.errors; this is the one place the translation happens.
        with pytest.raises(InvalidSettingValueError, match="must be a string"):
            values.validate(TEXT, 5, max_bytes=BIG)

    def test_a_wrong_type_is_not_reported_as_a_credential_complaint(self) -> None:
        # A caller that sent a number where a string belongs is told that, rather than
        # told its number does not look like a secret.
        with pytest.raises(InvalidSettingValueError):
            values.validate(NAMES, "ghp_" + "a" * 30, max_bytes=BIG)


class TestCredentialsComeBeforeSize:
    def test_a_credential_in_a_string_is_refused_naming_keyring(self) -> None:
        with pytest.raises(CredentialRefusedError, match="store credentials in keyring") as refusal:
            values.validate(TEXT, "ghp_" + "abcdefgh" * 4, max_bytes=BIG)
        assert "abcdefgh" not in str(refusal.value)
        assert "demo.knob" in str(refusal.value)

    def test_a_credential_inside_a_list_is_refused_wherever_it_sits(self) -> None:
        # Walked rather than joined and scanned as one string, so the sixtieth item is
        # caught as surely as the first.
        clean = [f"provider-{index}" for index in range(10)]
        with pytest.raises(CredentialRefusedError):
            values.validate(NAMES, [*clean, "xoxb-" + "1234abcd" * 4], max_bytes=BIG)

    def test_a_long_credential_is_refused_as_a_credential_not_as_too_long(self) -> None:
        # Told "too long", a caller comes back with a shorter API key.
        with pytest.raises(CredentialRefusedError):
            values.validate(TEXT, "ghp_" + "abcdefgh" * 40, max_bytes=16)

    def test_refuse_credentials_ignores_non_string_values(self) -> None:
        values.refuse_credentials(NUMBER, 5)
        values.refuse_credentials(FLAG, True)
        values.refuse_credentials(entry(SettingType.STR, None, nullable=True), None)


class TestSize:
    def test_a_value_over_the_byte_limit_is_refused_naming_both_numbers(self) -> None:
        with pytest.raises(
            InvalidSettingValueError, match="at most 8 bytes serialized; this one is 11"
        ):
            values.validate(TEXT, "abcdefghi", max_bytes=8)

    def test_size_is_measured_after_encoding(self) -> None:
        # Three characters, eleven bytes with the quotes and the UTF-8. The catalogue
        # bounds count characters; this backstop counts what is actually stored.
        with pytest.raises(InvalidSettingValueError, match="this one is 11"):
            values.validate(TEXT, "日本語", max_bytes=10)
        assert values.validate(TEXT, "日本語", max_bytes=11) == "日本語"


class TestEncoding:
    @pytest.mark.parametrize(
        "value", [True, False, 0, -7, 12_345, "", "text", [], ["b", "a"], None]
    )
    def test_every_legal_shape_round_trips(self, value: Any) -> None:
        assert values.decode(values.encode(TEXT, value)) == value

    def test_the_encoding_is_canonical(self) -> None:
        # sort_keys and no ASCII escaping, so two equal values encode to equal bytes and a
        # by-value comparison in the store is also a by-text one.
        assert values.encode(TEXT, "日本") == '"日本"'

    def test_a_float_that_json_cannot_spell_is_refused(self) -> None:
        # Python renders NaN as the bare token `NaN`, which is not JSON: Python reads it
        # back and every other reader in the world does not. allow_nan=False is what makes
        # this arm reachable, and it is why the arm exists at all.
        with pytest.raises(InvalidSettingValueError, match="JSON-serializable"):
            values.encode(TEXT, float("nan"))  # type: ignore[arg-type]

    def test_something_that_is_not_json_at_all_is_refused(self) -> None:
        with pytest.raises(InvalidSettingValueError, match="JSON-serializable"):
            values.encode(TEXT, object())  # type: ignore[arg-type]

    def test_decode_reads_what_this_service_wrote(self) -> None:
        assert values.decode(json.dumps(["x", "y"])) == ["x", "y"]


class TestStringsIn:
    def test_a_string_is_one_string(self) -> None:
        assert values._strings_in("a") == ["a"]

    def test_a_list_is_its_items(self) -> None:
        assert values._strings_in(["a", "b"]) == ["a", "b"]

    @pytest.mark.parametrize("value", [None, 5, True])
    def test_anything_else_has_no_strings(self, value: Any) -> None:
        assert values._strings_in(value) == []


legal_text = st.text(alphabet=st.characters(exclude_categories=["Cs"]), max_size=40).filter(
    lambda text: not any(character.isspace() for character in text) or len(text) < 32
)


@given(
    value=st.one_of(
        st.booleans(), st.integers(), legal_text, st.lists(legal_text, max_size=5), st.none()
    )
)
@settings(
    derandomize=True, max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_every_value_survives_a_round_trip(value: Any) -> None:
    # derandomize so coverage is reproducible in CI; a property test that explored
    # different branches on different runs would make the gate flap.
    assert values.decode(values.encode(TEXT, value)) == value
