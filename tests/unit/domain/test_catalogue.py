"""Every entry in the catalogue, checked the same way, and the catalogue as a whole.

Parametrised over the live catalogue so a new entry is covered the moment it is added and
a malformed one fails here by name. The count assertions exist to catch an accidental
deletion: they are meant to need updating when a setting is added.
"""

from __future__ import annotations

import re
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from settings_api.domain import catalogue, registry
from settings_api.domain.catalogue import BY_QUALIFIED, CATALOGUE, COMMON, NAMESPACES
from settings_api.domain.errors import UnknownNamespaceError, UnknownSettingError
from settings_api.domain.types import (
    KEY_PATTERN,
    MAX_DESCRIPTION_CHARS,
    MAX_SUMMARY_CHARS,
    OnUnavailable,
    Origin,
    SettingDef,
    SettingType,
)

ENTRIES = list(registry.every_entry())
IDS = [entry.qualified for entry in ENTRIES]

EXPECTED_COUNTS = {
    "common": 7,
    "keyring": 6,
    "user": 6,
    "persona": 7,
    "media": 6,
    "spotify": 4,
    "search": 6,
}


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
class TestEveryEntry:
    def test_the_key_is_lowercase_snake_case(self, entry: SettingDef) -> None:
        assert KEY_PATTERN.match(entry.key)

    def test_it_is_declared_in_its_own_namespaces_module(self, entry: SettingDef) -> None:
        assert entry in CATALOGUE[entry.namespace]

    def test_its_default_validates_against_its_own_bounds(self, entry: SettingDef) -> None:
        assert entry.validate(entry.default) == entry.default

    def test_it_declares_on_unavailable(self, entry: SettingDef) -> None:
        assert isinstance(entry.on_unavailable, OnUnavailable)

    def test_a_use_default_entry_calls_its_default_conservative(self, entry: SettingDef) -> None:
        if entry.on_unavailable is OnUnavailable.USE_DEFAULT:
            assert entry.conservative_values
            assert entry.default in entry.conservative_values

    def test_every_conservative_value_is_itself_legal(self, entry: SettingDef) -> None:
        for value in entry.conservative_values:
            assert entry.validate(value) == value

    def test_only_an_enum_has_choices(self, entry: SettingDef) -> None:
        assert bool(entry.choices) == (entry.value_type is SettingType.ENUM)

    def test_a_pattern_is_anchored_and_compiles(self, entry: SettingDef) -> None:
        if entry.pattern is not None:
            assert entry.pattern.startswith("^")
            assert entry.pattern.endswith("$")
            re.compile(entry.pattern)

    def test_the_prose_is_present_bounded_and_distinct(self, entry: SettingDef) -> None:
        assert 0 < len(entry.summary) <= MAX_SUMMARY_CHARS
        assert 0 < len(entry.description) <= MAX_DESCRIPTION_CHARS
        assert entry.summary.strip() != entry.description.strip()
        # Written for a model: the summary ends like a sentence.
        assert entry.summary.rstrip().endswith((".", "?"))

    def test_the_origin_is_explained(self, entry: SettingDef) -> None:
        assert isinstance(entry.origin, Origin)
        assert entry.origin_note.strip()

    def test_a_replacement_names_a_real_setting(self, entry: SettingDef) -> None:
        if entry.deprecated_by is not None:
            assert entry.deprecated_by in BY_QUALIFIED

    def test_a_retirement_date_parses(self, entry: SettingDef) -> None:
        if entry.retired_at is not None:
            date.fromisoformat(entry.retired_at)

    def test_check_passes(self, entry: SettingDef) -> None:
        entry.check()


class TestTheCatalogueAsAWhole:
    def test_the_counts_guard_against_an_accidental_deletion(self) -> None:
        assert {
            namespace: len(entries) for namespace, entries in CATALOGUE.items()
        } == EXPECTED_COUNTS
        assert len(BY_QUALIFIED) == sum(EXPECTED_COUNTS.values()) == 42

    def test_no_qualified_name_repeats(self) -> None:
        assert len(IDS) == len(set(IDS))

    def test_namespaces_and_by_qualified_agree(self) -> None:
        assert frozenset(EXPECTED_COUNTS) == NAMESPACES
        assert set(BY_QUALIFIED) == set(IDS)

    def test_common_is_first_in_documentation_order(self) -> None:
        assert next(iter(CATALOGUE)) == COMMON == "common"

    def test_exactly_two_settings_are_owner_writable_only(self) -> None:
        owner_only = {entry.qualified for entry in ENTRIES if entry.owner_writable_only}
        assert owner_only == {
            "keyring.require_reauth_for_credential_changes",
            "search.store_query_history",
        }

    def test_the_refuse_settings_are_the_ones_whose_default_is_permissive(self) -> None:
        refusing = {
            entry.qualified for entry in ENTRIES if entry.on_unavailable is OnUnavailable.REFUSE
        }
        assert refusing == {
            "common.default_profile",
            "keyring.require_reauth_for_credential_changes",
            "search.disabled_providers",
        }

    def test_the_common_and_namespace_collision_is_the_one_that_is_meant(self) -> None:
        common_keys = {entry.key for entry in CATALOGUE["common"]}
        collisions = {
            entry.qualified
            for namespace, entries in CATALOGUE.items()
            if namespace != "common"
            for entry in entries
            if entry.key in common_keys
        }
        # Deliberate, and exercised by resolution tests: the namespace wins.
        assert collisions == {"media.job_retention_hours", "spotify.job_retention_hours"}

    def test_nothing_is_retired_yet(self) -> None:
        assert all(entry.retired_at is None for entry in ENTRIES)


class TestAssembly:
    """The failure arms of the import-time check, reached with hand-built modules."""

    @staticmethod
    def module(namespace: str, *entries: SettingDef) -> Any:
        return SimpleNamespace(NAMESPACE=namespace, SETTINGS=tuple(entries))

    @staticmethod
    def entry(namespace: str, key: str) -> SettingDef:
        return SettingDef(
            namespace=namespace,
            key=key,
            value_type=SettingType.BOOL,
            default=False,
            summary="A flag.",
            description="What the flag does.",
            on_unavailable=OnUnavailable.REFUSE,
            origin=Origin.PROPOSED,
        )

    def test_two_modules_claiming_one_namespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(catalogue, "_MODULES", (self.module("a"), self.module("a")))
        with pytest.raises(ValueError, match="both define the 'a' namespace"):
            catalogue._assemble()

    def test_an_entry_declared_in_the_wrong_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(catalogue, "_MODULES", (self.module("a", self.entry("b", "k")),))
        with pytest.raises(ValueError, match=r"b\.k is declared in the 'a' module"):
            catalogue._assemble()

    def test_a_duplicate_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            catalogue, "_MODULES", (self.module("a", self.entry("a", "k"), self.entry("a", "k")),)
        )
        with pytest.raises(ValueError, match=r"a\.k is declared twice"):
            catalogue._assemble()

    def test_a_malformed_entry_is_refused_at_assembly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad = SettingDef(
            namespace="a",
            key="Bad",
            value_type=SettingType.BOOL,
            default=False,
            summary="s",
            description="d",
            on_unavailable=OnUnavailable.REFUSE,
            origin=Origin.PROPOSED,
        )
        monkeypatch.setattr(catalogue, "_MODULES", (self.module("a", bad),))
        with pytest.raises(ValueError, match="not lowercase snake case"):
            catalogue._assemble()

    def test_a_good_set_of_modules_assembles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(catalogue, "_MODULES", (self.module("a", self.entry("a", "k")),))
        assert list(catalogue._assemble()) == ["a"]


class TestRegistry:
    def test_every_entry_walks_the_catalogue_in_order(self) -> None:
        assert [entry.qualified for entry in registry.every_entry()] == IDS

    def test_entries_in_and_live_entries_in_agree_while_nothing_is_retired(self) -> None:
        assert registry.entries_in("spotify") == registry.live_entries_in("spotify")

    def test_live_entries_in_drops_a_retired_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dataclasses import replace

        live = registry.CATALOGUE["spotify"]
        retired = (*live, replace(live[0], key="old_market", retired_at="2026-01-01"))
        monkeypatch.setitem(registry.CATALOGUE, "spotify", retired)
        assert len(registry.entries_in("spotify")) == len(live) + 1
        assert len(registry.live_entries_in("spotify")) == len(live)

    def test_an_unknown_namespace_names_what_exists(self) -> None:
        with pytest.raises(
            UnknownNamespaceError, match="no namespace 'medai'; this build has common"
        ):
            registry.entries_in("medai")

    def test_definition_for_finds_an_entry(self) -> None:
        assert (
            registry.definition_for("spotify", "default_market").qualified
            == "spotify.default_market"
        )

    def test_the_namespace_is_checked_before_the_key(self) -> None:
        # A caller that misspelled the namespace is told that, rather than told the key
        # does not exist in a namespace that also does not exist.
        with pytest.raises(UnknownNamespaceError):
            registry.definition_for("nope", "default_market")

    def test_an_unknown_key_points_at_describe_settings(self) -> None:
        with pytest.raises(
            UnknownSettingError,
            match="no setting 'nope' in the spotify namespace; see describe_settings",
        ):
            registry.definition_for("spotify", "nope")

    def test_a_retired_key_is_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dataclasses import replace

        live = registry.CATALOGUE["spotify"]
        old = replace(live[0], key="old_market", retired_at="2026-01-01")
        monkeypatch.setitem(registry.CATALOGUE, "spotify", (*live, old))
        monkeypatch.setitem(registry.BY_QUALIFIED, "spotify.old_market", old)
        with pytest.raises(UnknownSettingError):
            registry.definition_for("spotify", "old_market")
