"""The shape of a setting, and every rule one value must satisfy.

Two properties are pinned here that the rest of the service leans on without checking:

**Types are exact.** ``"true"`` is not ``True`` and ``"5"`` is not ``5``, and ``True`` is
not a whole number even though Python says it is. A setting that coerced would let a
person set ``store_query_history`` to the string ``"false"`` and get ``True``.

**A malformed entry is refused as an entry.** :meth:`SettingDef.check` runs over the whole
catalogue at import, so every rule it applies is a build that refuses to start rather
than a 500 the first time somebody reads that namespace.

The entries here are throwaway ones built in the test, not the real catalogue -- the real
one is covered by ``test_catalogue.py``. Building small ones is what lets a test bend
exactly one field.
"""

from __future__ import annotations

from typing import Any

import pytest

from settings_api.domain.types import (
    _CHECKERS,
    _ENTRY_RULES,
    KEY_PATTERN,
    MAX_DESCRIPTION_CHARS,
    MAX_SUMMARY_CHARS,
    ExtraCheck,
    OnUnavailable,
    Origin,
    SettingDef,
    SettingType,
)


def entry(**overrides: Any) -> SettingDef:
    """A minimal legal entry, with one or more fields bent."""
    fields: dict[str, Any] = {
        "namespace": "demo",
        "key": "knob",
        "value_type": SettingType.INT,
        "default": 5,
        "minimum": 1,
        "maximum": 10,
        "summary": "A knob.",
        "description": "What turning the knob does for the person.",
        "on_unavailable": OnUnavailable.USE_DEFAULT,
        "conservative_values": (5,),
        "origin": Origin.PROPOSED,
    }
    fields.update(overrides)
    return SettingDef(**fields)


def check_fails(definition: SettingDef, phrase: str) -> None:
    """Assert an entry is refused *as an entry*, naming the rule."""
    with pytest.raises(ValueError, match=phrase):
        definition.check()


class TestTheEntryIsFrozen:
    def test_an_entry_cannot_be_changed_after_it_is_built(self) -> None:
        # The catalogue is read for the life of the process. A mutable entry would be a
        # setting whose bounds a request could change.
        definition = entry()
        with pytest.raises(AttributeError):
            definition.default = 9  # type: ignore[misc]

    def test_qualified_is_namespace_dot_key(self) -> None:
        assert entry(namespace="search", key="safe_search").qualified == "search.safe_search"

    def test_retired_follows_retired_at(self) -> None:
        assert entry().retired is False
        assert entry(retired_at="2026-01-01").retired is True


class TestNull:
    def test_null_is_refused_unless_the_entry_allows_it(self) -> None:
        with pytest.raises(ValueError, match="may not be null"):
            entry().validate(None)

    def test_null_is_a_value_for_a_nullable_entry(self) -> None:
        nullable = entry(
            value_type=SettingType.STR,
            default=None,
            nullable=True,
            minimum=None,
            maximum=None,
            conservative_values=(None,),
        )
        assert nullable.validate(None) is None


class TestBool:
    @pytest.fixture
    def flag(self) -> SettingDef:
        return entry(
            value_type=SettingType.BOOL,
            default=False,
            minimum=None,
            maximum=None,
            conservative_values=(False,),
        )

    @pytest.mark.parametrize("value", [True, False])
    def test_a_boolean_is_accepted(self, flag: SettingDef, value: bool) -> None:
        assert flag.validate(value) is value

    @pytest.mark.parametrize("value", ["true", "false", "True", 1, 0, "", [], "yes"])
    def test_a_string_or_number_that_spells_a_boolean_is_not_one(
        self, flag: SettingDef, value: Any
    ) -> None:
        # The single worst failure this service could have: somebody sets
        # store_query_history to the string "false" and gets True.
        with pytest.raises(ValueError, match="must be true or false"):
            flag.validate(value)


class TestInt:
    @pytest.mark.parametrize("value", [1, 5, 10])
    def test_a_whole_number_inside_the_range_is_accepted(self, value: int) -> None:
        assert entry().validate(value) == value

    def test_just_below_the_minimum_is_refused(self) -> None:
        with pytest.raises(ValueError, match="may not be below 1"):
            entry().validate(0)

    def test_just_above_the_maximum_is_refused(self) -> None:
        with pytest.raises(ValueError, match="may not be above 10"):
            entry().validate(11)

    def test_an_entry_with_no_bounds_accepts_any_whole_number(self) -> None:
        unbounded = entry(minimum=None, maximum=None, default=0, conservative_values=(0,))
        assert unbounded.validate(-1_000_000) == -1_000_000
        assert unbounded.validate(1_000_000) == 1_000_000

    @pytest.mark.parametrize("value", ["5", "5.0", 5.0, 5.5, [5], {"n": 5}])
    def test_anything_that_is_not_an_int_is_refused(self, value: Any) -> None:
        with pytest.raises(ValueError, match="must be a whole number"):
            entry().validate(value)

    def test_a_boolean_is_not_a_whole_number(self) -> None:
        # In Python `True` IS an `int`. Without the bool-first check, setting a numeric
        # limit to `true` would store 1 and read back as 1, and the person who typed
        # `true` would never find out.
        with pytest.raises(ValueError, match="must be a whole number"):
            entry().validate(True)


class TestStr:
    @pytest.fixture
    def text(self) -> SettingDef:
        return entry(
            value_type=SettingType.STR,
            default="hello",
            minimum=None,
            maximum=None,
            max_chars=5,
            conservative_values=("hello",),
        )

    def test_a_string_within_the_limit_is_accepted(self, text: SettingDef) -> None:
        assert text.validate("hi") == "hi"

    def test_exactly_the_limit_is_accepted(self, text: SettingDef) -> None:
        assert text.validate("abcde") == "abcde"

    def test_one_over_the_limit_is_refused(self, text: SettingDef) -> None:
        with pytest.raises(ValueError, match="at most 5 characters"):
            text.validate("abcdef")

    def test_length_is_counted_in_characters_not_bytes(self, text: SettingDef) -> None:
        # Five characters, fifteen bytes. A byte count would refuse a person's own name.
        assert text.validate("日本語です") == "日本語です"

    @pytest.mark.parametrize("value", [5, True, ["a"], None])
    def test_a_non_string_is_refused(self, text: SettingDef, value: Any) -> None:
        if value is None:
            with pytest.raises(ValueError, match="may not be null"):
                text.validate(value)
        else:
            with pytest.raises(ValueError, match="must be a string"):
                text.validate(value)

    def test_a_pattern_is_matched_as_a_whole(self) -> None:
        market = entry(
            value_type=SettingType.STR,
            default="GB",
            minimum=None,
            maximum=None,
            pattern=r"^[A-Z]{2}$",
            conservative_values=("GB",),
        )
        assert market.validate("PT") == "PT"
        # `fullmatch` rather than `search`: a pattern that merely had to appear somewhere
        # would accept "xxGBxx", and the description a person reads would then be a lie.
        for bad in ("gb", "GBR", "G", " GB", "GB\n"):
            with pytest.raises(ValueError, match="not in the form this setting requires"):
                market.validate(bad)

    def test_the_refusal_never_echoes_the_value(self, text: SettingDef) -> None:
        secret_shaped = "abcdefghijklmnop"
        with pytest.raises(ValueError, match="at most 5 characters") as refusal:
            text.validate(secret_shaped)
        # The message ends up in a 422 body and a log line; the value must be in neither.
        assert secret_shaped not in str(refusal.value)


class TestTimezone:
    @pytest.fixture
    def zone(self) -> SettingDef:
        return entry(
            value_type=SettingType.STR,
            default="UTC",
            minimum=None,
            maximum=None,
            extra_check=ExtraCheck.TIMEZONE,
            conservative_values=("UTC",),
        )

    @pytest.mark.parametrize(
        "name", ["UTC", "Europe/Lisbon", "America/New_York", "Pacific/Chatham"]
    )
    def test_a_real_zone_is_accepted(self, zone: SettingDef, name: str) -> None:
        assert zone.validate(name) == name

    @pytest.mark.parametrize("name", ["Europe/Atlantis", "Mars/Olympus", "", "not a zone"])
    def test_a_zone_tzdata_does_not_know_is_refused(self, zone: SettingDef, name: str) -> None:
        # Not a regex, because the set of valid names is the tzdata the machine has, and a
        # pattern that admitted Europe/Lisbon would also admit Europe/Atlantis. A service
        # formatting against an unresolvable zone would raise deep in a render path.
        with pytest.raises(ValueError, match="must name a time zone"):
            zone.validate(name)

    @pytest.mark.parametrize("name", ["../../etc/passwd", "Europe/../Lisbon", "UTC\x00"])
    def test_a_zone_name_that_is_a_path_is_refused_here_not_inside_zoneinfo(
        self, zone: SettingDef, name: str
    ) -> None:
        # A zone name is used to open a file. These are the shapes worth refusing before
        # zoneinfo gets them; the point of the test is that they are refused as a VALUE
        # rather than escaping as an OSError somebody has to map to a status code.
        with pytest.raises(ValueError, match="must name a time zone"):
            zone.validate(name)


class TestEnum:
    @pytest.fixture
    def choice(self) -> SettingDef:
        return entry(
            value_type=SettingType.ENUM,
            default="b",
            minimum=None,
            maximum=None,
            choices=("a", "b", "c"),
            conservative_values=("b",),
        )

    @pytest.mark.parametrize("value", ["a", "b", "c"])
    def test_a_listed_choice_is_accepted(self, choice: SettingDef, value: str) -> None:
        assert choice.validate(value) == value

    @pytest.mark.parametrize("value", ["d", "A", "", "a "])
    def test_anything_not_listed_is_refused_naming_the_choices(
        self, choice: SettingDef, value: str
    ) -> None:
        with pytest.raises(ValueError, match="must be one of a, b, c"):
            choice.validate(value)

    @pytest.mark.parametrize("value", [0, True, ["a"]])
    def test_a_non_string_is_refused_before_the_choices_are_consulted(
        self, choice: SettingDef, value: Any
    ) -> None:
        with pytest.raises(ValueError, match="must be a string"):
            choice.validate(value)


class TestStrList:
    @pytest.fixture
    def names(self) -> SettingDef:
        return entry(
            value_type=SettingType.STR_LIST,
            default=[],
            minimum=None,
            maximum=None,
            max_items=3,
            max_item_chars=4,
            conservative_values=([],),
        )

    def test_an_empty_list_is_a_value(self, names: SettingDef) -> None:
        assert names.validate([]) == []

    def test_a_list_of_strings_within_both_limits_is_accepted(self, names: SettingDef) -> None:
        assert names.validate(["a", "bb", "ccc"]) == ["a", "bb", "ccc"]

    def test_one_item_too_many_is_refused(self, names: SettingDef) -> None:
        with pytest.raises(ValueError, match="at most 3 items"):
            names.validate(["a", "b", "c", "d"])

    def test_one_item_too_long_is_refused(self, names: SettingDef) -> None:
        with pytest.raises(ValueError, match="items of at most 4 characters"):
            names.validate(["abcde"])

    @pytest.mark.parametrize("value", ["abc", 5, {"a": 1}, ("a",)])
    def test_something_that_is_not_a_list_is_refused(self, names: SettingDef, value: Any) -> None:
        with pytest.raises(ValueError, match="must be a list of strings"):
            names.validate(value)

    @pytest.mark.parametrize("item", [1, None, True, ["x"]])
    def test_a_non_string_item_is_refused(self, names: SettingDef, item: Any) -> None:
        with pytest.raises(ValueError, match="must be a list of strings"):
            names.validate(["ok", item])

    def test_a_repeated_item_is_refused(self, names: SettingDef) -> None:
        # A list of providers to disable with "acme" in it twice is a list somebody
        # concatenated without looking, and the second copy means nothing.
        with pytest.raises(ValueError, match="may not repeat an item"):
            names.validate(["a", "a"])

    def test_an_entry_with_no_item_limits_accepts_any_list_of_strings(self) -> None:
        loose = entry(
            value_type=SettingType.STR_LIST,
            default=[],
            minimum=None,
            maximum=None,
            conservative_values=([],),
        )
        many = [str(index) for index in range(500)]
        assert loose.validate(many) == many


class TestEveryTypeHasAChecker:
    def test_the_checker_table_covers_every_setting_type(self) -> None:
        # The exhaustiveness lives here rather than in an import-time assertion, because
        # an assertion no input can reach is a line the coverage gate can never cover --
        # and the answer to that is never a pragma. A sixth SettingType without a checker
        # fails this test.
        assert set(_CHECKERS) == set(SettingType)

    def test_every_entry_rule_is_a_callable(self) -> None:
        assert all(callable(rule) for rule in _ENTRY_RULES)
        assert len(_ENTRY_RULES) == 6


class TestEntryNames:
    @pytest.mark.parametrize("key", ["knob", "a", "a1", "snake_case_9", "x_"])
    def test_a_lowercase_snake_case_key_is_accepted(self, key: str) -> None:
        entry(key=key).check()

    @pytest.mark.parametrize(
        "key", ["Knob", "1knob", "_knob", "knob-name", "knob.name", "", "knöb"]
    )
    def test_anything_else_is_refused(self, key: str) -> None:
        check_fails(entry(key=key), "not lowercase snake case")

    @pytest.mark.parametrize("namespace", ["Demo", "de-mo", "", "9ns"])
    def test_a_namespace_obeys_the_same_rule(self, namespace: str) -> None:
        check_fails(entry(namespace=namespace), "not lowercase snake case")

    def test_the_key_pattern_is_the_one_the_documentation_quotes(self) -> None:
        assert KEY_PATTERN.pattern == r"^[a-z][a-z0-9_]*$"


class TestEntryProse:
    def test_an_empty_summary_is_refused(self) -> None:
        check_fails(entry(summary=""), f"summary of 1 to {MAX_SUMMARY_CHARS}")

    def test_an_over_long_summary_is_refused(self) -> None:
        check_fails(entry(summary="x" * (MAX_SUMMARY_CHARS + 1)), "summary of 1 to")

    def test_an_empty_description_is_refused(self) -> None:
        check_fails(entry(description=""), f"description of 1 to {MAX_DESCRIPTION_CHARS}")

    def test_an_over_long_description_is_refused(self) -> None:
        check_fails(entry(description="x" * (MAX_DESCRIPTION_CHARS + 1)), "description of 1 to")

    def test_a_description_that_only_repeats_the_summary_is_refused(self) -> None:
        # What an author writes when they have not decided what the setting means -- and
        # both are read by a model deciding whether to touch it.
        check_fails(
            entry(summary="A knob.", description="  A knob. "),
            "only repeats its summary",
        )

    def test_the_limits_are_exactly_at_the_boundary(self) -> None:
        entry(summary="s" * MAX_SUMMARY_CHARS, description="d" * MAX_DESCRIPTION_CHARS).check()


class TestEntryBounds:
    def test_an_enum_with_no_choices_is_refused(self) -> None:
        check_fails(
            entry(
                value_type=SettingType.ENUM,
                default="a",
                minimum=None,
                maximum=None,
                conservative_values=("a",),
            ),
            "enum with no choices",
        )

    def test_an_enum_that_repeats_a_choice_is_refused(self) -> None:
        check_fails(
            entry(
                value_type=SettingType.ENUM,
                default="a",
                minimum=None,
                maximum=None,
                choices=("a", "b", "a"),
                conservative_values=("a",),
            ),
            "repeats a choice",
        )

    def test_choices_on_something_that_is_not_an_enum_are_refused(self) -> None:
        check_fails(entry(choices=("a",)), "declares choices but is not an enum")

    def test_a_numeric_range_on_a_non_integer_is_refused(self) -> None:
        check_fails(
            entry(
                value_type=SettingType.STR,
                default="x",
                minimum=1,
                maximum=None,
                conservative_values=("x",),
            ),
            "numeric range but is not a whole number",
        )
        check_fails(
            entry(
                value_type=SettingType.BOOL,
                default=True,
                minimum=None,
                maximum=3,
                conservative_values=(True,),
            ),
            "numeric range but is not a whole number",
        )

    def test_a_minimum_above_the_maximum_is_refused(self) -> None:
        check_fails(entry(minimum=10, maximum=1, default=5), "minimum above its maximum")

    @pytest.mark.parametrize("pattern", ["[A-Z]{2}", "^[A-Z]{2}", "[A-Z]{2}$"])
    def test_an_unanchored_pattern_is_refused(self, pattern: str) -> None:
        # Anchored in the text as well as by fullmatch, so the pattern a person reads in
        # describe_settings means what the server does with it. Unanchored, it reads as
        # "contains", which is a different rule.
        check_fails(
            entry(
                value_type=SettingType.STR,
                default="GB",
                minimum=None,
                maximum=None,
                pattern=pattern,
                conservative_values=("GB",),
            ),
            "not anchored",
        )


class TestEntryDefault:
    def test_a_default_its_own_bounds_reject_is_refused(self) -> None:
        # A default that fails its own rules is a setting nobody can leave alone.
        check_fails(entry(default=99, conservative_values=(99,)), "may not be above 10")

    def test_a_default_of_the_wrong_type_is_refused(self) -> None:
        check_fails(entry(default="five", conservative_values=("five",)), "must be a whole number")


class TestEntryFallback:
    def test_a_use_default_entry_with_no_conservative_values_is_refused(self) -> None:
        check_fails(entry(conservative_values=()), "declares no conservative values")

    def test_a_use_default_entry_whose_default_is_not_conservative_is_refused(self) -> None:
        # The pair is what turns "the default is safe to fall back to" from a claim in a
        # docstring into something the build checks -- and a later change of default trips
        # it unless somebody also revisits the list.
        check_fails(entry(default=5, conservative_values=(1, 2)), "does not call conservative")

    def test_a_refuse_entry_needs_no_conservative_values(self) -> None:
        entry(on_unavailable=OnUnavailable.REFUSE, conservative_values=()).check()

    def test_a_list_default_is_found_in_its_conservative_set_by_equality(self) -> None:
        # Lists are not hashable, so this has to work by `==` rather than by a set --
        # and it does, which is what lets `disabled_providers` declare `([],)`.
        entry(
            value_type=SettingType.STR_LIST,
            default=[],
            minimum=None,
            maximum=None,
            conservative_values=([],),
        ).check()


class TestEntryLifecycle:
    def test_a_retirement_date_must_be_a_date(self) -> None:
        check_fails(entry(retired_at="yesterday"), "retired_at that is not an ISO date")

    def test_an_iso_date_is_accepted(self) -> None:
        entry(retired_at="2026-03-01").check()

    def test_a_replacement_must_name_namespace_dot_key(self) -> None:
        check_fails(entry(deprecated_by="knob2"), "does not name namespace.key")

    def test_a_qualified_replacement_is_accepted(self) -> None:
        entry(deprecated_by="demo.knob2").check()


class TestCheckRunsEveryRule:
    def test_a_fully_legal_entry_passes(self) -> None:
        entry().check()

    def test_an_entry_with_labels_passes(self) -> None:
        entry(labels=("privacy",)).check()
