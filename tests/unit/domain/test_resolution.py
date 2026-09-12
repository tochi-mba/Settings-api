"""Three layers into one answer, and the eight ways they can combine.

The core of this file is one parametrised table: every combination of *policy default
present*, *pin present* and *account row present*, with the value AND the reported source
asserted for each. The reported source matters as much as the value, because "why is it
this" needs three different actions depending on the answer -- change it, ask the operator,
or nothing, it is the default.

One test here guards the nullable case specifically, because it is the one a rewrite would
get wrong: a stored ``None`` is a real stored value, and membership in the mapping -- not
``None``-ness -- is what says a row exists.
"""

from __future__ import annotations

from typing import Any

import pytest

from settings_api.domain.policy import NO_POLICY, Policy, parse
from settings_api.domain.registry import definition_for
from settings_api.domain.resolution import (
    Resolved,
    Source,
    resolve,
    resolve_for_service,
    resolve_namespace,
)
from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingType

GRACE = definition_for("user", "grace_days")  # default 30, clampable
MARKET = definition_for("spotify", "default_market")  # nullable, default None


def policy_with(**clauses: Any) -> Policy:
    return parse({"user": {"grace_days": clauses}})


# (policy default?, pin?, account row?) -> (expected value, expected source)
CASES = [
    pytest.param(False, False, False, 30, Source.DEFAULT, id="nothing: catalogue default"),
    pytest.param(True, False, False, 7, Source.POLICY, id="policy default only"),
    pytest.param(False, True, False, 14, Source.POLICY, id="pin only"),
    pytest.param(True, True, False, 14, Source.POLICY, id="policy default and pin: pin"),
    pytest.param(False, False, True, 3, Source.ACCOUNT, id="account only"),
    pytest.param(
        True,
        False,
        True,
        3,
        Source.ACCOUNT,
        id="account over policy default",
    ),
    pytest.param(False, True, True, 14, Source.POLICY, id="pin over account"),
    pytest.param(True, True, True, 14, Source.POLICY, id="everything: pin"),
]


@pytest.mark.parametrize(("policy_default", "pinned", "has_row", "expected", "source"), CASES)
def test_all_eight_combinations(
    policy_default: bool, pinned: bool, has_row: bool, expected: int, source: Source
) -> None:
    clauses: dict[str, Any] = {}
    if policy_default:
        clauses["default"] = 7
    if pinned:
        clauses["pin"] = 14
    policy = policy_with(**clauses) if clauses else NO_POLICY
    stored = {"user.grace_days": 3} if has_row else {}

    resolved = resolve(GRACE, stored=stored, policy=policy)

    assert resolved.value == expected
    assert resolved.source is source
    assert resolved.pinned is pinned
    # set_by_account reports whether a ROW EXISTS, even when a pin overrides it: the person
    # did express a preference, and the operator overrode it. Both facts are true.
    assert resolved.set_by_account is has_row


class TestAPinBeatsTheAccount:
    def test_because_a_pin_added_later_must_still_apply(self) -> None:
        # Otherwise pinning would do nothing for exactly the people it was added for --
        # anybody who had already touched the setting.
        resolved = resolve(GRACE, stored={"user.grace_days": 3}, policy=policy_with(pin=14))
        assert resolved.value == 14
        assert resolved.set_by_account is True


class TestNullIsAValue:
    def test_a_stored_none_is_set_and_comes_from_the_account(self) -> None:
        # Membership, not None-ness. `.get()` returning None could not tell "set to null"
        # from "never set", and for a nullable setting those are different decisions.
        resolved = resolve(MARKET, stored={"spotify.default_market": None}, policy=NO_POLICY)
        assert resolved.value is None
        assert resolved.set_by_account is True
        assert resolved.source is Source.ACCOUNT

    def test_an_absent_row_on_a_nullable_setting_is_the_default(self) -> None:
        resolved = resolve(MARKET, stored={}, policy=NO_POLICY)
        assert resolved.value is None
        assert resolved.set_by_account is False
        assert resolved.source is Source.DEFAULT


class TestTheNarrowedDefinitionIsReported:
    def test_bounds_come_from_policy_when_it_narrowed_them(self) -> None:
        # describe_settings must report the bounds a write is checked against; reporting
        # the catalogue's would offer a range and then refuse most of it.
        resolved = resolve(GRACE, stored={}, policy=policy_with(maximum=60))
        assert resolved.definition.maximum == 60
        assert GRACE.maximum == 365

    def test_the_properties_read_through_to_the_definition(self) -> None:
        resolved = resolve(GRACE, stored={}, policy=NO_POLICY)
        assert (resolved.namespace, resolved.key, resolved.qualified) == (
            "user",
            "grace_days",
            "user.grace_days",
        )


class TestResolveNamespace:
    def test_every_live_entry_is_present_keyed_by_key(self) -> None:
        resolved = resolve_namespace("spotify", stored={}, policy=NO_POLICY)
        assert set(resolved) == {
            "default_market",
            "max_batch_size",
            "confirm_timeout_seconds",
            "job_retention_hours",
        }
        assert all(isinstance(item, Resolved) for item in resolved.values())

    def test_a_retired_entry_is_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Retired rows survive only so reverting the catalogue restores them; they are not
        # part of what a person can see today.
        retired = SettingDef(
            namespace="spotify",
            key="old_knob",
            value_type=SettingType.INT,
            default=1,
            summary="Old.",
            description="Gone.",
            on_unavailable=OnUnavailable.REFUSE,
            origin=Origin.EXISTING,
            retired_at="2026-01-01",
        )
        import settings_api.domain.resolution as module

        monkeypatch.setattr(
            module, "live_entries_in", lambda _ns: (MARKET,) if _ns == "spotify" else ()
        )
        assert set(
            resolve_namespace("spotify", stored={"spotify.old_knob": 5}, policy=NO_POLICY)
        ) == {"default_market"}
        assert retired.retired is True


class TestResolveForService:
    def test_common_is_merged_underneath(self) -> None:
        merged = resolve_for_service("spotify", stored={}, policy=NO_POLICY)
        assert merged["timezone"].namespace == "common"
        assert merged["default_market"].namespace == "spotify"

    def test_the_namespace_wins_on_a_collision(self) -> None:
        # Not theoretical: common and media both define job_retention_hours. Defined this
        # way round so a new common key can never silently override a service's own.
        merged = resolve_for_service(
            "media", stored={"common.job_retention_hours": 99}, policy=NO_POLICY
        )
        item = merged["job_retention_hours"]
        assert item.namespace == "media"
        assert item.value == 1
        assert item.definition.maximum == 168

    def test_a_namespace_without_its_own_gets_the_common_answer(self) -> None:
        merged = resolve_for_service(
            "search", stored={"common.job_retention_hours": 24}, policy=NO_POLICY
        )
        assert merged["job_retention_hours"].namespace == "common"
        assert merged["job_retention_hours"].value == 24

    def test_asking_for_common_itself_does_not_merge_it_twice(self) -> None:
        merged = resolve_for_service("common", stored={}, policy=NO_POLICY)
        assert merged == resolve_namespace("common", stored={}, policy=NO_POLICY)
