"""Operator policy: narrowing the catalogue, and never widening it.

Every refusal here is a startup error. A deployment with a contradictory policy does not
start, because the only moment somebody is in a position to fix it is that one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from settings_api.domain.policy import NO_POLICY, Policy, PolicyError, load, parse
from settings_api.domain.registry import definition_for

GRACE = definition_for("user", "grace_days")  # INT 0-365, clampable
ERASURE = definition_for("user", "erasure_mode")  # ENUM, not clampable
LOG_VALUES = definition_for("user", "log_values")  # BOOL, not clampable
PROVIDERS = definition_for("search", "disabled_providers")  # STR_LIST ≤60, not clampable
MARKET = definition_for("spotify", "default_market")  # nullable STR
SHELL = definition_for("environments", "default_shell")  # ENUM, not clampable
CONTENT = definition_for("search", "max_content_chars")  # INT 1000-200000, clampable


def refused(document: object, phrase: str) -> None:
    with pytest.raises(PolicyError, match=phrase):
        parse(document)


class TestLoading:
    def test_no_path_means_the_catalogue_stands_as_written(self) -> None:
        assert load(None) is NO_POLICY
        assert NO_POLICY.definition_of(GRACE) is GRACE
        assert NO_POLICY.pin_on(GRACE) is None
        assert NO_POLICY.is_pinned(GRACE) is False

    def test_a_configured_path_that_does_not_exist_is_an_error_not_an_empty_policy(
        self, tmp_path: Path
    ) -> None:
        # A deployment that meant to narrow something and mistyped the path would otherwise
        # run wide open and look correct.
        with pytest.raises(PolicyError, match="could not be read"):
            load(tmp_path / "missing.json")

    def test_a_file_that_is_not_json_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "policy.json"
        path.write_text("{not json")
        with pytest.raises(PolicyError, match="not valid JSON"):
            load(path)

    def test_a_real_file_is_parsed(self, tmp_path: Path) -> None:
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"user": {"grace_days": {"maximum": 60, "pin": 7}}}))
        policy = load(path)
        assert policy.definition_of(GRACE).maximum == 60
        assert policy.pin_on(GRACE) == 7


class TestTheDocumentShape:
    @pytest.mark.parametrize("document", [[], "x", 5, None])
    def test_the_root_must_be_an_object(self, document: object) -> None:
        refused(document, "must be an object of namespaces")

    def test_a_namespace_must_be_named_by_a_string(self) -> None:
        # JSON keys are always strings, so this needs a Python caller to reach; it is
        # here so the parser is honest about every shape it can be handed.
        refused({5: {}}, "must be named by strings")

    def test_an_unknown_namespace_is_refused(self) -> None:
        refused({"nope": {}}, "'nope', which is not a namespace")

    def test_a_namespace_block_must_be_an_object(self) -> None:
        refused({"user": []}, "policy for 'user' must be an object of settings")

    def test_an_unknown_setting_is_refused(self) -> None:
        refused({"user": {"nope": {}}}, "user.nope, which is not a setting")

    def test_a_setting_must_be_named_by_a_string(self) -> None:
        refused({"user": {7: {}}}, "names a setting that is not a string")

    def test_a_clause_block_must_be_an_object(self) -> None:
        refused({"user": {"grace_days": 5}}, "policy for user.grace_days must be an object")

    def test_an_unknown_clause_is_refused(self) -> None:
        # A typo in a policy file is a narrowing somebody believes is in force and is not.
        refused({"user": {"grace_days": {"maxmium": 5}}}, "unknown clauses: maxmium")

    def test_an_empty_clause_block_changes_nothing(self) -> None:
        policy = parse({"user": {"grace_days": {}}})
        assert policy == NO_POLICY


class TestNarrowingNeedsPermission:
    @pytest.mark.parametrize("clause", ["minimum", "maximum", "choices", "max_items"])
    def test_a_clamping_clause_on_a_non_clampable_setting_is_refused(self, clause: str) -> None:
        refused({"user": {"log_values": {clause: 1}}}, "not operator-clampable")

    def test_a_default_and_a_pin_need_no_permission(self) -> None:
        policy = parse({"user": {"log_values": {"default": True, "pin": True}}})
        assert policy.definition_of(LOG_VALUES).default is True
        assert policy.pin_on(LOG_VALUES) is True


class TestMinimum:
    def test_raised_is_accepted(self) -> None:
        assert parse({"user": {"grace_days": {"minimum": 7}}}).definition_of(GRACE).minimum == 7

    def test_lowered_is_refused(self) -> None:
        refused(
            {"search": {"max_content_chars": {"minimum": 10}}},
            "lowers search.max_content_chars's minimum",
        )

    def test_must_be_a_whole_number(self) -> None:
        refused({"user": {"grace_days": {"minimum": "7"}}}, "minimum must be a whole number")
        refused({"user": {"grace_days": {"minimum": True}}}, "minimum must be a whole number")


class TestTheWholeNumberReader:
    def test_a_range_clause_on_a_non_integer_is_refused_even_when_clampable(self) -> None:
        # No clampable non-INT exists in the catalogue today; the guard is for the day one
        # does, and it is reached directly so the rule does not quietly rot.
        from dataclasses import replace

        from settings_api.domain import policy as module

        clampable_list = replace(PROVIDERS, operator_clampable=True)
        with pytest.raises(PolicyError, match="minimum applies only to whole numbers"):
            module._whole(clampable_list, "minimum", 5)

    def test_a_minimum_on_an_entry_without_one_is_simply_taken(self) -> None:
        # Every clampable INT in the catalogue has a minimum, so the "nothing to compare
        # against" arm is reached with a hand-built entry.
        from dataclasses import replace

        from settings_api.domain import policy as module

        open_ended = replace(GRACE, minimum=None)
        assert module._raised(open_ended, 3) == 3


class TestNarrowThroughTheParserShape:
    def test_choices_and_max_items_flow_through_narrow(self) -> None:
        # No ENUM or STR_LIST in the catalogue is clampable, so these two arms of _narrow
        # are reached with entries made clampable by hand. The rule is for the day one is.
        from dataclasses import replace

        from settings_api.domain import policy as module

        enum = module._narrow(replace(SHELL, operator_clampable=True), {"choices": ["bash"]})
        assert enum.choices == ("bash",)
        lst = module._narrow(replace(PROVIDERS, operator_clampable=True), {"max_items": 3})
        assert lst.max_items == 3


class TestMaximum:
    def test_lowered_is_accepted(self) -> None:
        assert parse({"user": {"grace_days": {"maximum": 60}}}).definition_of(GRACE).maximum == 60

    def test_raised_is_refused(self) -> None:
        refused({"user": {"grace_days": {"maximum": 9999}}}, "raises user.grace_days's maximum")

    def test_a_range_on_a_non_integer_is_refused(self) -> None:
        # persona.recall_default_limit is clampable and an INT; there is no clampable
        # non-INT with a range, so the guard is reached through the type check itself.
        refused({"user": {"grace_days": {"maximum": 1.5}}}, "maximum must be a whole number")


class TestChoices:
    def test_narrowed_is_accepted_and_keeps_catalogue_order(self) -> None:
        # No ENUM in the catalogue is clampable today, and that is the point of this test:
        # the rule exists for the day one is. Use a clampable INT to prove the clause is
        # gated, and prove the narrowing logic directly below.
        refused(
            {"environments": {"default_shell": {"choices": ["bash"]}}}, "not operator-clampable"
        )

    def test_the_narrowing_itself(self) -> None:
        from settings_api.domain import policy as module

        narrowed = module._subset(SHELL, ["sh", "bash"])
        assert narrowed == ("bash", "sh")

    def test_widened_is_refused(self) -> None:
        from settings_api.domain import policy as module

        with pytest.raises(
            PolicyError, match=r"adds choices .* that the catalogue does not have: fish"
        ):
            module._subset(SHELL, ["bash", "fish"])

    def test_empty_is_refused(self) -> None:
        from settings_api.domain import policy as module

        with pytest.raises(PolicyError, match="may not be empty"):
            module._subset(SHELL, [])

    def test_on_a_non_enum_is_refused(self) -> None:
        from settings_api.domain import policy as module

        with pytest.raises(PolicyError, match="applies only to an enum"):
            module._subset(GRACE, ["a"])

    @pytest.mark.parametrize("value", ["bash", [1, 2], None])
    def test_must_be_a_list_of_strings(self, value: Any) -> None:
        from settings_api.domain import policy as module

        with pytest.raises(PolicyError, match="must be a list of strings"):
            module._subset(SHELL, value)


class TestMaxItems:
    def test_the_narrowing_itself(self) -> None:
        from settings_api.domain import policy as module

        assert module._fewer(PROVIDERS, 10) == 10

    def test_raised_is_refused(self) -> None:
        from settings_api.domain import policy as module

        with pytest.raises(PolicyError, match=r"raises search\.disabled_providers's max_items"):
            module._fewer(PROVIDERS, 999)

    def test_on_a_non_list_is_refused(self) -> None:
        from settings_api.domain import policy as module

        with pytest.raises(PolicyError, match="applies only to a list"):
            module._fewer(GRACE, 5)

    def test_gated_by_clampable_at_the_parser(self) -> None:
        refused({"search": {"disabled_providers": {"max_items": 5}}}, "not operator-clampable")


class TestDefaults:
    def test_a_default_inside_the_bounds_is_accepted(self) -> None:
        assert parse({"user": {"grace_days": {"default": 7}}}).definition_of(GRACE).default == 7

    def test_a_default_outside_the_catalogue_bounds_is_a_startup_error(self) -> None:
        refused(
            {"user": {"grace_days": {"default": 9999}}},
            "default is not a value this setting allows",
        )

    def test_a_default_outside_the_narrowed_bounds_is_refused_too(self) -> None:
        # The default is applied LAST and validated against the entry as narrowed by the
        # same policy -- so 100 is fine against the catalogue's 365 and refused here.
        refused({"user": {"grace_days": {"maximum": 60, "default": 100}}}, "default is not a value")

    def test_a_default_of_the_wrong_type_is_refused(self) -> None:
        refused({"user": {"grace_days": {"default": "7"}}}, "default is not a value")

    def test_a_default_that_is_not_a_settings_value_at_all_is_refused(self) -> None:
        refused({"user": {"grace_days": {"default": 1.5}}}, "default is not a value")
        refused({"user": {"grace_days": {"default": {"a": 1}}}}, "default is not a value")
        refused({"search": {"disabled_providers": {"default": [1, 2]}}}, "default is not a value")

    def test_a_default_equal_to_the_catalogues_produces_no_override(self) -> None:
        assert parse({"user": {"grace_days": {"default": 30}}}) == NO_POLICY


class TestPins:
    def test_a_pin_is_recorded_and_visible(self) -> None:
        policy = parse({"user": {"erasure_mode": {"pin": "tombstone"}}})
        assert policy.is_pinned(ERASURE) is True
        assert policy.pin_on(ERASURE) == "tombstone"
        assert policy.is_pinned(GRACE) is False

    def test_a_pin_is_validated_against_the_narrowed_entry(self) -> None:
        refused({"user": {"grace_days": {"maximum": 60, "pin": 100}}}, "pin is not a value")

    def test_a_pin_of_the_wrong_type_is_refused(self) -> None:
        refused({"user": {"erasure_mode": {"pin": "forever"}}}, "pin is not a value")

    def test_pinned_to_null_is_distinguishable_from_not_pinned(self) -> None:
        # pin_on returns None for both; is_pinned is what the two paths that care ask first.
        policy = parse({"spotify": {"default_market": {"pin": None}}})
        assert policy.pin_on(MARKET) is None
        assert policy.is_pinned(MARKET) is True
        assert NO_POLICY.is_pinned(MARKET) is False


class TestThePolicyObject:
    def test_it_is_frozen(self) -> None:
        with pytest.raises(AttributeError):
            NO_POLICY.pins = {}  # type: ignore[misc]

    def test_definition_of_returns_the_catalogue_entry_when_nothing_narrowed_it(self) -> None:
        policy = parse({"user": {"grace_days": {"maximum": 60}}})
        assert policy.definition_of(GRACE).maximum == 60
        assert policy.definition_of(CONTENT) is CONTENT

    def test_a_policy_may_narrow_and_pin_several_settings_at_once(self) -> None:
        policy = parse(
            {
                "user": {"grace_days": {"maximum": 60}, "log_values": {"pin": False}},
                "search": {"max_content_chars": {"default": 20_000}},
            }
        )
        assert isinstance(policy, Policy)
        assert set(policy.overrides) == {"user.grace_days", "search.max_content_chars"}
        assert set(policy.pins) == {"user.log_values"}
