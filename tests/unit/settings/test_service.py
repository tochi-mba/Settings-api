"""Where a request becomes a change, and the order in which it is refused.

Built directly, with hand-made identities: the seam is the service, and no HTTP is needed
to prove what it decides. The order of refusals is the point of several tests here -- a
caller with a typo must be told it is a typo, and a caller writing a pinned setting must be
told it is pinned before its value is looked at.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from settings_api.auth.tokens import Identity
from settings_api.domain.errors import (
    CredentialRefusedError,
    InvalidSettingValueError,
    NamespaceNotGrantedError,
    RevisionMismatchError,
    SettingNotWritableError,
    SettingPinnedError,
    UnknownNamespaceError,
    UnknownSettingError,
    UnknownSettingsError,
)
from settings_api.domain.policy import NO_POLICY, Policy, load
from settings_api.domain.registry import NAMESPACES
from settings_api.domain.resolution import Source
from settings_api.events.log import Action
from settings_api.events.sql_log import SqlEventLog
from settings_api.settings.service import EXPORT_VERSION, SettingsService
from settings_api.settings.sql_store import SqlSettingsStore
from tests.conftest import build_settings
from tests.fakes.clock import FakeClock

A = "account-a"
OWNER = Identity(account_id=A, audience="settings", namespaces=frozenset(NAMESPACES))
SEARCH_ONLY = Identity(
    account_id=A, audience="settings.search", namespaces=frozenset({"search", "common"})
)
SPOTIFY_SERVICE = Identity(
    account_id=A,
    audience="spotify",
    namespaces=frozenset({"spotify", "common"}),
    service="spotify-api",
)
SEARCH_SERVICE = Identity(
    account_id=A,
    audience="web-search-api",
    namespaces=frozenset({"search", "common"}),
    service="web-search-api",
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def build(
    store: SqlSettingsStore,
    events: SqlEventLog,
    tmp_path: Path,
    clock: FakeClock,
    policy: Policy = NO_POLICY,
) -> SettingsService:
    return SettingsService(
        store=store, events=events, policy=policy, clock=clock, config=build_settings(tmp_path)
    )


@pytest.fixture
def service(
    store: SqlSettingsStore, events: SqlEventLog, tmp_path: Path, clock: FakeClock
) -> SettingsService:
    return build(store, events, tmp_path, clock)


def pinned_policy(tmp_path: Path, **pins: Any) -> Policy:
    path = tmp_path / "policy.json"
    document: dict[str, dict[str, dict[str, Any]]] = {}
    for qualified, value in pins.items():
        namespace, key = qualified.split("__")
        document.setdefault(namespace, {})[key] = {"pin": value}
    path.write_text(json.dumps(document))
    return load(path)


class TestReads:
    async def test_get_all_never_fails_for_an_account_with_nothing_stored(
        self, service: SettingsService
    ) -> None:
        document = await service.get_all(OWNER)
        assert set(document.namespaces) == NAMESPACES
        assert document.revision == 0
        assert document.etag == '"account-a.0"'
        assert document.values()["spotify"]["default_market"] is None

    async def test_get_all_is_bounded_by_the_tokens_namespaces(
        self, service: SettingsService
    ) -> None:
        assert set((await service.get_all(SEARCH_ONLY)).namespaces) == {"search", "common"}

    async def test_get_namespace(self, service: SettingsService) -> None:
        document = await service.get_namespace(OWNER, "spotify")
        assert set(document.namespaces) == {"spotify"}

    async def test_an_unknown_namespace_is_told_it_is_unknown_before_anything_about_the_grant(
        self, service: SettingsService
    ) -> None:
        # A caller with a typo goes looking for the wrong problem otherwise.
        with pytest.raises(UnknownNamespaceError):
            await service.get_namespace(SEARCH_ONLY, "spotfy")

    async def test_an_ungranted_namespace_is_forbidden(self, service: SettingsService) -> None:
        with pytest.raises(NamespaceNotGrantedError, match="does not grant the spotify"):
            await service.get_namespace(SEARCH_ONLY, "spotify")

    async def test_get_setting_reports_value_set_source_and_pin(
        self, service: SettingsService
    ) -> None:
        resolved = await service.get_setting(OWNER, "spotify", "default_market")
        assert (resolved.value, resolved.set_by_account, resolved.source, resolved.pinned) == (
            None,
            False,
            Source.DEFAULT,
            False,
        )
        await service.set_setting(OWNER, "spotify", "default_market", "GB")
        resolved = await service.get_setting(OWNER, "spotify", "default_market")
        assert (resolved.value, resolved.set_by_account, resolved.source) == (
            "GB",
            True,
            Source.ACCOUNT,
        )

    async def test_get_setting_refuses_an_unknown_key(self, service: SettingsService) -> None:
        with pytest.raises(UnknownSettingError, match="see describe_settings"):
            await service.get_setting(OWNER, "spotify", "nope")

    async def test_describe_is_the_same_document_as_get_all(self, service: SettingsService) -> None:
        assert (await service.describe(OWNER)).etag == (await service.get_all(OWNER)).etag

    async def test_resolve_for_merges_common_underneath(self, service: SettingsService) -> None:
        await service.set_setting(OWNER, "common", "timezone", "Europe/Lisbon")
        document = await service.resolve_for(SPOTIFY_SERVICE, "spotify")
        merged = document.namespaces["spotify"]
        assert merged["timezone"].value == "Europe/Lisbon"
        assert "default_market" in merged

    async def test_resolve_for_refuses_another_services_namespace(
        self, service: SettingsService
    ) -> None:
        with pytest.raises(NamespaceNotGrantedError):
            await service.resolve_for(SPOTIFY_SERVICE, "user")


class TestWrites:
    async def test_set_returns_the_new_revision_and_etag(self, service: SettingsService) -> None:
        written = await service.set_setting(OWNER, "spotify", "default_market", "GB")
        assert (written.revision, written.etag, written.changed) == (
            1,
            '"account-a.1"',
            ("spotify.default_market",),
        )
        assert written.any_change is True

    async def test_an_identical_set_is_reported_unchanged(self, service: SettingsService) -> None:
        await service.set_setting(OWNER, "spotify", "default_market", "GB")
        written = await service.set_setting(OWNER, "spotify", "default_market", "GB")
        assert written.any_change is False
        assert written.revision == 1

    async def test_a_value_is_validated_against_the_narrowed_definition(
        self, store: SqlSettingsStore, events: SqlEventLog, tmp_path: Path, clock: FakeClock
    ) -> None:
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"user": {"grace_days": {"maximum": 60}}}))
        service = build(store, events, tmp_path, clock, load(path))
        with pytest.raises(InvalidSettingValueError, match="may not be above 60"):
            await service.set_setting(OWNER, "user", "grace_days", 100)

    async def test_a_bad_value_is_refused(self, service: SettingsService) -> None:
        with pytest.raises(InvalidSettingValueError, match="must be true or false"):
            await service.set_setting(OWNER, "user", "log_values", "false")

    async def test_a_credential_is_refused(self, service: SettingsService) -> None:
        with pytest.raises(CredentialRefusedError, match="keyring"):
            await service.set_setting(
                OWNER, "search", "default_model", "openai:sk-" + "Ab3dEf7h" * 4
            )

    async def test_a_credential_inside_a_list_is_refused(self, service: SettingsService) -> None:
        with pytest.raises(CredentialRefusedError):
            await service.set_setting(
                OWNER, "search", "disabled_providers", ["fine", "ghp_" + "Ab3dEf7h" * 4]
            )

    async def test_a_pinned_setting_is_a_conflict_never_a_silent_no_op(
        self, store: SqlSettingsStore, events: SqlEventLog, tmp_path: Path, clock: FakeClock
    ) -> None:
        service = build(
            store, events, tmp_path, clock, pinned_policy(tmp_path, user__log_values=False)
        )
        with pytest.raises(SettingPinnedError, match="pinned by this deployment's policy"):
            await service.set_setting(OWNER, "user", "log_values", True)
        assert await events.count_for_account(A) == 0

    async def test_pinned_is_reported_before_the_value_is_looked_at(
        self, store: SqlSettingsStore, events: SqlEventLog, tmp_path: Path, clock: FakeClock
    ) -> None:
        # "You cannot change this" is more useful than "that value is out of range" when
        # both are true.
        service = build(
            store, events, tmp_path, clock, pinned_policy(tmp_path, user__log_values=False)
        )
        with pytest.raises(SettingPinnedError):
            await service.set_setting(OWNER, "user", "log_values", "not even a boolean")

    async def test_an_owner_only_setting_is_refused_for_a_service(
        self, service: SettingsService
    ) -> None:
        with pytest.raises(SettingNotWritableError, match="only be changed by the person"):
            await service.set_setting(SEARCH_SERVICE, "search", "store_query_history", True)

    async def test_an_owner_only_setting_is_allowed_for_the_person(
        self, service: SettingsService
    ) -> None:
        written = await service.set_setting(OWNER, "search", "store_query_history", True)
        assert written.any_change is True

    async def test_a_service_may_write_within_its_own_namespace(
        self, service: SettingsService, events: SqlEventLog
    ) -> None:
        await service.set_setting(SPOTIFY_SERVICE, "spotify", "default_market", "PT")
        (event,) = await events.read(A, limit=1)
        assert event.actor == "service:spotify-api"
        assert event.service == "spotify-api"

    async def test_a_service_may_not_write_outside_it(self, service: SettingsService) -> None:
        with pytest.raises(NamespaceNotGrantedError):
            await service.set_setting(SPOTIFY_SERVICE, "user", "grace_days", 7)

    async def test_if_revision_threads_through(self, service: SettingsService) -> None:
        await service.set_setting(OWNER, "spotify", "default_market", "GB")
        with pytest.raises(RevisionMismatchError):
            await service.set_setting(OWNER, "spotify", "default_market", "PT", if_revision=0)
        written = await service.set_setting(OWNER, "spotify", "default_market", "PT", if_revision=1)
        assert written.revision == 2


class TestDocuments:
    async def test_update_merges_rather_than_replaces(self, service: SettingsService) -> None:
        await service.update(
            OWNER, {"spotify": {"default_market": "GB"}, "common": {"timezone": "Europe/Lisbon"}}
        )
        await service.update(OWNER, {"spotify": {"default_market": "PT"}})
        values = (await service.get_all(OWNER)).values()
        assert values["spotify"]["default_market"] == "PT"
        assert values["common"]["timezone"] == "Europe/Lisbon"

    async def test_update_is_all_or_nothing(
        self, service: SettingsService, events: SqlEventLog
    ) -> None:
        # The last key is refused, so the first is not written either.
        with pytest.raises(InvalidSettingValueError):
            await service.update(
                OWNER, {"spotify": {"default_market": "GB", "max_batch_size": 9999}}
            )
        assert (await service.get_all(OWNER)).values()["spotify"]["default_market"] is None
        assert await events.count_for_account(A) == 0

    async def test_every_unknown_key_is_named_at_once(self, service: SettingsService) -> None:
        # One round trip per typo is how an assistant ends up in a loop.
        with pytest.raises(
            UnknownSettingsError, match=r"spotify\.markte, spotify\.nope; see describe_settings"
        ):
            await service.update(
                OWNER, {"spotify": {"nope": 1, "markte": "GB", "default_market": "GB"}}
            )

    async def test_update_records_one_event_with_the_update_action(
        self, service: SettingsService, events: SqlEventLog
    ) -> None:
        await service.update(OWNER, {"spotify": {"default_market": "GB", "max_batch_size": 10}})
        (event,) = await events.read(A, limit=10)
        assert event.action is Action.UPDATE

    async def test_import_records_the_import_action(
        self, service: SettingsService, events: SqlEventLog
    ) -> None:
        # "I restored a backup" and "I changed three things" are different answers to
        # "what did I do in March".
        await service.import_document(OWNER, {"spotify": {"default_market": "GB"}})
        (event,) = await events.read(A, limit=10)
        assert event.action is Action.IMPORT

    async def test_an_update_across_an_ungranted_namespace_is_refused_whole(
        self, service: SettingsService
    ) -> None:
        with pytest.raises(NamespaceNotGrantedError):
            await service.update(
                SEARCH_ONLY,
                {"search": {"safe_search": "strict"}, "spotify": {"default_market": "GB"}},
            )
        assert (await service.get_all(OWNER)).values()["search"]["safe_search"] == "moderate"


class TestResets:
    async def test_reset_setting(self, service: SettingsService) -> None:
        await service.set_setting(OWNER, "spotify", "default_market", "GB")
        written = await service.reset_setting(OWNER, "spotify", "default_market")
        assert written.changed == ("spotify.default_market",)
        assert (
            await service.get_setting(OWNER, "spotify", "default_market")
        ).set_by_account is False

    async def test_reset_setting_respects_pins_and_ownership(
        self, store: SqlSettingsStore, events: SqlEventLog, tmp_path: Path, clock: FakeClock
    ) -> None:
        service = build(
            store, events, tmp_path, clock, pinned_policy(tmp_path, user__log_values=False)
        )
        with pytest.raises(SettingPinnedError):
            await service.reset_setting(OWNER, "user", "log_values")
        with pytest.raises(SettingNotWritableError):
            await service.reset_setting(SEARCH_SERVICE, "search", "store_query_history")

    async def test_reset_namespace_skips_what_the_caller_may_not_write(
        self, store: SqlSettingsStore, events: SqlEventLog, tmp_path: Path, clock: FakeClock
    ) -> None:
        service = build(
            store, events, tmp_path, clock, pinned_policy(tmp_path, search__safe_search="strict")
        )
        await service.set_setting(OWNER, "search", "max_content_chars", 5_000)
        await service.set_setting(OWNER, "search", "store_query_history", True)

        # A service clearing the namespace: the pinned key and the owner-only key are
        # skipped rather than refusing the whole request.
        written = await service.reset_namespace(SEARCH_SERVICE, "search")
        assert written.changed == ("search.max_content_chars",)
        assert (await service.get_setting(OWNER, "search", "store_query_history")).value is True

    async def test_reset_namespace_by_the_owner_clears_owner_only_too(
        self, service: SettingsService
    ) -> None:
        await service.set_setting(OWNER, "search", "store_query_history", True)
        written = await service.reset_namespace(OWNER, "search")
        assert "search.store_query_history" in written.changed

    async def test_reset_namespace_of_nothing_changes_nothing(
        self, service: SettingsService
    ) -> None:
        assert (await service.reset_namespace(OWNER, "media")).any_change is False


class TestExport:
    async def test_export_is_sparse(self, service: SettingsService) -> None:
        await service.set_setting(OWNER, "spotify", "default_market", "GB")
        exported = await service.export(OWNER)
        # Only what was chosen. Defaults are absent so an import elsewhere restores the
        # decisions rather than freezing this deployment's defaults into another one.
        assert exported.settings == {"spotify": {"default_market": "GB"}}
        assert exported.version == EXPORT_VERSION
        assert exported.revision == 1
        assert exported.exported_at.startswith("2026-01-01T12:00:00")

    async def test_export_is_bounded_by_the_token(self, service: SettingsService) -> None:
        await service.set_setting(OWNER, "spotify", "default_market", "GB")
        await service.set_setting(OWNER, "search", "safe_search", "strict")
        assert (await service.export(SEARCH_ONLY)).settings == {"search": {"safe_search": "strict"}}

    async def test_export_excludes_a_retired_key(
        self, service: SettingsService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await service.set_setting(OWNER, "spotify", "default_market", "GB")
        await service.set_setting(OWNER, "spotify", "max_batch_size", 10)
        import settings_api.settings.service as module
        from settings_api.domain.registry import live_entries_in

        live = live_entries_in("spotify")
        without = tuple(entry for entry in live if entry.key != "max_batch_size")
        monkeypatch.setattr(
            module,
            "live_entries_in",
            lambda ns: without if ns == "spotify" else live_entries_in(ns),
        )
        # A retired key's rows survive so reverting the catalogue restores them; they are
        # not part of what this person can see or restore today.
        assert (await service.export(OWNER)).settings == {"spotify": {"default_market": "GB"}}

    async def test_export_then_import_round_trips_and_is_idempotent(
        self, service: SettingsService
    ) -> None:
        await service.update(
            OWNER, {"spotify": {"default_market": "GB"}, "common": {"timezone": "Europe/Lisbon"}}
        )
        exported = await service.export(OWNER)
        written = await service.import_document(OWNER, exported.settings)
        assert written.any_change is False


class TestEventsAndForget:
    async def test_read_events_pages_newest_first(self, service: SettingsService) -> None:
        for market in ("GB", "PT", "ES"):
            await service.set_setting(OWNER, "spotify", "default_market", market)
        page = await service.read_events(OWNER, limit=2, before=None)
        assert [event.revision for event in page] == [3, 2]
        rest = await service.read_events(OWNER, limit=2, before=page[-1].sequence)
        assert [event.revision for event in rest] == [1]

    async def test_forget_returns_the_count(self, service: SettingsService) -> None:
        await service.set_setting(OWNER, "spotify", "default_market", "GB")
        assert await service.forget(OWNER) == 1
        assert (await service.get_all(OWNER)).revision == 0
