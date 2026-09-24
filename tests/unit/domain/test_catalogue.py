"""Every entry in the catalogue, checked the same way, and the catalogue as a whole.

Parametrised over the live catalogue so a new entry is covered the moment it is added and
a malformed one fails here by name. The count assertions exist to catch an accidental
deletion: they are meant to need updating when a setting is added.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
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
    AgentAccess,
    OnUnavailable,
    Origin,
    SettingDef,
    SettingScope,
    SettingType,
)

ENTRIES = list(registry.every_entry())
IDS = [entry.qualified for entry in ENTRIES]

EXPECTED_COUNTS = {
    "common": 8,
    "keyring": 7,
    "user": 6,
    "persona": 7,
    "memory": 7,
    "lucy": 83,
    "spotify": 8,
    "search": 8,
    "environments": 7,
}

PROFILE_SCOPED = frozenset(
    {
        "spotify.default_market",
        "spotify.max_batch_size",
        "spotify.confirm_timeout_seconds",
        "spotify.default_device",
        "spotify.shuffle_on_play",
        "spotify.repeat_mode",
        "spotify.allow_explicit",
        "environments.idle_environment_hours",
        "environments.idle_shell_minutes",
        "environments.default_shell",
        "environments.persist_history",
        "environments.command_timeout_seconds",
        "persona.default_persona",
        "persona.recall_default_limit",
        "search.default_model",
        "search.search_backend",
        "search.safe_search",
        "search.default_result_count",
        "search.recency_days",
        "lucy.model",
        "lucy.fallback_model",
        "lucy.thinking",
        "lucy.temperature",
        "lucy.response_style",
        "lucy.vision_enabled",
        "lucy.reserve_percent",
        "lucy.warn_at_percent",
        "lucy.compaction_trigger_percent",
        "lucy.history_turns_kept",
        "lucy.tool_results_kept",
        "lucy.max_steps_per_plan",
        "lucy.max_parallel_steps",
        "lucy.step_timeout_seconds",
        "lucy.plan_timeout_seconds",
        "lucy.permission_mode",
        "lucy.confirm_outward_actions",
        "lucy.incognito",
        "lucy.input_policy",
        "lucy.auto_title",
        "lucy.session_idle_archive_days",
        "lucy.workspace_retention_hours",
        "lucy.stream_thinking",
        "lucy.notify_on_long_turn",
        "lucy.long_turn_seconds",
        "lucy.prompt_feeds_enabled",
        "lucy.feeds_account",
        "lucy.feeds_persona",
        "lucy.feeds_music",
        "lucy.feeds_workspace",
        "lucy.feeds_research",
        "lucy.feeds_account_pinned",
        "lucy.feeds_persona_identity",
        "lucy.feeds_persona_notes",
        "lucy.feeds_music_now_playing",
        "lucy.feeds_music_device",
        "lucy.feeds_music_shuffled",
        "lucy.feeds_music_repeat",
        "lucy.feeds_music_queue_head",
        "lucy.feeds_workspace_cwd",
        "lucy.feeds_workspace_shell",
        "lucy.feeds_workspace_pid",
        "lucy.feeds_workspace_shells_running",
        "lucy.feeds_workspace_sandbox",
        "lucy.feeds_workspace_git_branch",
        "lucy.feeds_workspace_last_command",
        "lucy.feeds_research_backend",
    }
)


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
        assert len(BY_QUALIFIED) == sum(EXPECTED_COUNTS.values()) == 141

    def test_profile_scoped_settings_are_exactly_this_set(self) -> None:
        # Exclusive scopes, declared on the entry. A setting nobody thought about stays
        # ACCOUNT so a restriction cannot quietly split across profiles. This set is
        # the whole of the PROFILE opt-in; adding one is a deliberate catalogue change.
        actual = {entry.qualified for entry in ENTRIES if entry.scope is SettingScope.PROFILE}
        assert actual == PROFILE_SCOPED

    def test_every_common_setting_is_account_scoped(self) -> None:
        assert all(entry.scope is SettingScope.ACCOUNT for entry in CATALOGUE["common"])

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
            # The assistant's floor under every permission it was told to skip, and three
            # memory settings where neither direction of a guess is safe: relying on more
            # than somebody agreed to, recording more than they agreed to, and rewriting
            # what is already recorded.
            "lucy.approval_policy",
            "lucy.disabled_capabilities",
            "memory.retrieval_trust_floor",
            "memory.write_importance_floor",
            "memory.consolidation",
            "memory.erasure_grace_days",
            # And two where the permissive default is the one the service needs to work,
            # so landing on it would undo a restriction somebody expressed -- and the turn
            # would succeed, which is why nobody would find out.
            "lucy.vision_enabled",
            "spotify.allow_explicit",
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
        assert collisions == {"spotify.job_retention_hours"}

    def test_nothing_is_retired_yet(self) -> None:
        assert all(entry.retired_at is None for entry in ENTRIES)

    def test_the_built_in_catalogue_is_exactly_the_modules_this_repository_ships(self) -> None:
        # The guard behind ADR-0011. A namespace reaches the public catalogue only through
        # a module in this package or through the entry-point group, and this pins the
        # first half: every assembled namespace is on disk here, so a namespace nobody can
        # see the source of is a namespace that did not come from this repository.
        #
        # "On disk here" means a `<namespace>.py` or, once a namespace outgrows one file,
        # a `<namespace>/` package -- `lucy` is split by what a
        # person is deciding. Both are a namespace this tree ships and both satisfy the
        # NamespaceModule protocol; neither is something an installed package can add.
        package = Path(catalogue.__file__).parent
        shipped = {path.stem for path in package.glob("*.py") if not path.stem.startswith("_")}
        shipped |= {path.name for path in package.iterdir() if (path / "__init__.py").is_file()}
        assert {module.NAMESPACE for module in catalogue._MODULES} == shipped
        assert set(EXPECTED_COUNTS) == shipped

    def test_nothing_is_registered_under_the_entry_point_group_in_this_repository(self) -> None:
        # The other half. This repository ships no extension, so the live catalogue is the
        # built-in modules and nothing else -- which is what makes the counts above a
        # statement about this tree rather than about whatever happens to be installed.
        assert catalogue._discovered_modules() == ()

    def test_only_settings_that_change_nothing_are_freely_assistant_writable(self) -> None:
        freely = {
            entry.qualified for entry in ENTRIES if entry.agent_writable is AgentAccess.FREELY
        }
        # None of these changes what is stored about somebody, who may read it, how long it
        # survives, or what an assistant is allowed to do. That is the whole membership
        # rule, and it is worth pinning because the cost of a wrong entry here is a
        # privilege an assistant takes quietly.
        assert freely == {
            "lucy.temperature",
            "lucy.notify_on_long_turn",
            "lucy.long_turn_seconds",
        }

    def test_the_settings_an_assistant_may_only_propose_are_the_prompt_feed_ones(self) -> None:
        proposing = {
            entry.qualified
            for entry in ENTRIES
            if entry.agent_writable is AgentAccess.WITH_APPROVAL
        }
        assert proposing == {
            "lucy.prompt_feeds_enabled",
            "lucy.prompt_hide_personal_feeds",
            "lucy.auto_title",
        } | {entry.qualified for entry in CATALOGUE["lucy"] if entry.key.startswith("feeds_")}

    def test_prompt_feed_toggles_cover_the_declared_table_and_nothing_else(self) -> None:
        from settings_api.domain.catalogue.lucy.feeds import _FEED_CAPS, _FEED_FIELDS

        expected = {f"lucy.feeds_{capability}" for capability in _FEED_CAPS}
        expected.update(
            f"lucy.feeds_{capability}_{key}" for capability, key, _default, _summary in _FEED_FIELDS
        )
        actual = {entry.qualified for entry in CATALOGUE["lucy"] if entry.key.startswith("feeds_")}
        assert actual == expected
        unknown = BY_QUALIFIED["lucy.prompt_allow_unknown_feed_fields"]
        assert unknown.agent_writable is AgentAccess.NEVER
        assert BY_QUALIFIED["lucy.disabled_capabilities"].on_unavailable is OnUnavailable.REFUSE
        assert BY_QUALIFIED["lucy.thinking"].default == "medium"

    def test_the_two_model_round_limits_are_separate_and_bounded(self) -> None:
        main = BY_QUALIFIED["lucy.max_llm_turns"]
        child = BY_QUALIFIED["lucy.max_subagent_turns"]
        assert (main.default, main.minimum, main.maximum) == (12, 1, 100)
        assert (child.default, child.minimum, child.maximum) == (8, 1, 50)


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


class TestDiscovery:
    """A namespace an installed package registered, held to exactly the built-in rules."""

    module = staticmethod(TestAssembly.module)
    entry = staticmethod(TestAssembly.entry)

    def test_a_registered_namespace_is_assembled_alongside_the_built_in_ones(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extension = self.module("extra", self.entry("extra", "k"))
        monkeypatch.setattr(catalogue, "_discovered_modules", lambda: (extension,))

        assembled = catalogue._assemble()

        assert assembled["extra"] == extension.SETTINGS
        assert set(assembled) == set(EXPECTED_COUNTS) | {"extra"}

    def test_a_registered_entry_is_validated_exactly_as_a_built_in_one_is(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The property that makes an entry point safe to offer: an extension gets no
        # latitude a module in this package does not, so a malformed one stops the process
        # that installed it rather than answering a request wrongly months later.
        malformed = SettingDef(
            namespace="extra",
            key="k",
            value_type=SettingType.BOOL,
            default=False,
            summary="A flag.",
            description="What the flag does.",
            on_unavailable=OnUnavailable.USE_DEFAULT,
            origin=Origin.PROPOSED,
        )
        monkeypatch.setattr(
            catalogue, "_discovered_modules", lambda: (self.module("extra", malformed),)
        )

        with pytest.raises(ValueError, match="declares no conservative values"):
            catalogue._assemble()

    def test_a_registered_module_declaring_someone_elses_namespace_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            catalogue, "_discovered_modules", lambda: (self.module("extra", self.entry("a", "k")),)
        )

        with pytest.raises(ValueError, match=r"a\.k is declared in the 'extra' module"):
            catalogue._assemble()

    def test_a_registered_namespace_may_not_replace_a_built_in_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Otherwise an installed package is a way to redefine a public namespace's bounds
        # and prose, in a process where nothing on disk here says it happened.
        monkeypatch.setattr(
            catalogue,
            "_discovered_modules",
            lambda: (self.module(COMMON, self.entry(COMMON, "k")),),
        )

        with pytest.raises(ValueError, match="both define the 'common' namespace"):
            catalogue._assemble()

    def test_an_entry_point_in_the_group_is_loaded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        extension = self.module("extra", self.entry("extra", "k"))
        point = SimpleNamespace(load=lambda: extension)
        seen: list[str] = []

        def fake_entry_points(*, group: str) -> tuple[Any, ...]:
            seen.append(group)
            return (point,)

        monkeypatch.setattr(catalogue, "entry_points", fake_entry_points)

        assert catalogue._discovered_modules() == (extension,)
        assert seen == [catalogue.ENTRY_POINT_GROUP]


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
