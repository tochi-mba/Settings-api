"""Exclusive account vs profile scope: one value per setting, at exactly one level.

A setting is either one value for the person or one value per keyring profile, never
both. Overlay of the same key would be a second settings system. These tests pin the
write rule Lucy relies on: extra ``profile=`` on an account-scoped key is ignored, so
a session can always pass the profile it is in; a profile-scoped write without a
profile is a 422 that names the keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from settings_api.domain.errors import (
    InvalidSettingValueError,
    SettingScopeError,
    UnknownSettingsError,
)
from settings_api.domain.types import ACCOUNT_PROFILE, SettingScope
from settings_api.events.sql_log import SqlEventLog
from settings_api.settings.service import EXPORT_VERSION, V1_PROFILE_FALLBACK, SettingsService
from settings_api.settings.sql_store import SqlSettingsStore
from settings_api.settings.store import MAX_PROFILES_PER_ACCOUNT, Change
from tests.fakes.clock import FakeClock
from tests.unit.settings.test_service import OWNER, SPOTIFY_SERVICE, build
from tests.unit.settings.test_sql_store import apply as store_apply


@pytest.fixture
def service(
    store: SqlSettingsStore, events: SqlEventLog, tmp_path: Path, clock: FakeClock
) -> SettingsService:
    return build(store, events, tmp_path, clock)


class TestExclusiveReadsAndWrites:
    async def test_a_profile_scoped_write_without_a_profile_is_422_naming_the_fix(
        self, service: SettingsService
    ) -> None:
        with pytest.raises(SettingScopeError, match=r"spotify\.default_market is profile-scoped"):
            await service.set_setting(OWNER, "spotify", "default_market", "GB")

    async def test_an_account_scoped_write_ignores_an_extra_profile(
        self, service: SettingsService
    ) -> None:
        await service.set_setting(OWNER, "common", "timezone", "Europe/Lisbon", profile="work")
        stored = await service.get_setting(OWNER, "common", "timezone")
        assert stored.value == "Europe/Lisbon"
        assert stored.set_by_account is True
        # The same value is visible under every profile, because it is not per-profile.
        assert (
            await service.get_setting(OWNER, "common", "timezone", profile="personal")
        ).value == "Europe/Lisbon"

    async def test_two_profiles_hold_independent_values_for_a_profile_scoped_key(
        self, service: SettingsService
    ) -> None:
        await service.set_setting(OWNER, "spotify", "default_market", "GB", profile="personal")
        await service.set_setting(OWNER, "spotify", "default_market", "PT", profile="work")
        assert (
            await service.get_setting(OWNER, "spotify", "default_market", profile="personal")
        ).value == "GB"
        assert (
            await service.get_setting(OWNER, "spotify", "default_market", profile="work")
        ).value == "PT"
        # Omitting the profile leaves a profile-scoped setting at its default.
        unread = await service.get_setting(OWNER, "spotify", "default_market")
        assert unread.value is None
        assert unread.set_by_account is False

    async def test_an_orphaned_account_row_for_a_profile_key_is_ignored_on_read(
        self, service: SettingsService, store: SqlSettingsStore
    ) -> None:
        # Exclusive scopes, not overlay: a leftover `*` row for a key that is now
        # profile-scoped must not leak into a named profile's read.
        await store_apply(
            store,
            Change(
                namespace="spotify",
                key="default_market",
                value="ES",
                profile=ACCOUNT_PROFILE,
            ),
        )
        unread = await service.get_setting(OWNER, "spotify", "default_market", profile="personal")
        assert unread.value is None
        exported = await service.export(OWNER)
        assert exported.settings == {}
        assert exported.profiles == {}

    async def test_an_account_row_stored_under_a_profile_name_is_ignored(
        self, service: SettingsService, store: SqlSettingsStore
    ) -> None:
        # The inverse orphan: an account-scoped key filed under a real profile name,
        # which a restore would never write. Exclusive scopes ignore it on read and
        # export rather than treating it as overlay.
        await store_apply(
            store,
            Change(
                namespace="common",
                key="timezone",
                value="Europe/Lisbon",
                profile="work",
            ),
        )
        assert (await service.get_setting(OWNER, "common", "timezone")).value == "UTC"
        exported = await service.export(OWNER)
        assert exported.settings == {}
        assert exported.profiles == {}

    async def test_a_row_for_a_key_the_catalogue_no_longer_has_is_skipped_on_read(
        self, service: SettingsService, store: SqlSettingsStore
    ) -> None:
        await store_apply(
            store,
            Change(namespace="spotify", key="ghost", value="x", profile=ACCOUNT_PROFILE),
        )
        document = await service.get_all(OWNER)
        assert "ghost" not in document.values()["spotify"]

    async def test_an_unknown_export_version_is_refused(self, service: SettingsService) -> None:
        with pytest.raises(InvalidSettingValueError, match="export formats"):
            await service.import_document(OWNER, {}, version=99)

    async def test_a_v1_import_of_only_account_keys_does_not_invent_a_profile(
        self, service: SettingsService
    ) -> None:
        written = await service.import_document(
            OWNER, {"common": {"timezone": "Europe/Lisbon"}}, version=1
        )
        assert written.any_change is True
        exported = await service.export(OWNER)
        assert exported.settings == {"common": {"timezone": "Europe/Lisbon"}}
        assert exported.profiles == {}

    async def test_a_v1_import_names_unknown_keys(self, service: SettingsService) -> None:
        with pytest.raises(UnknownSettingsError, match=r"spotify\.nope"):
            await service.import_document(OWNER, {"spotify": {"nope": 1}}, version=1)

    async def test_a_mixed_document_writes_each_key_at_its_own_level(
        self, service: SettingsService
    ) -> None:
        await service.update(
            OWNER,
            {
                "common": {"timezone": "Europe/Lisbon"},
                "spotify": {"default_market": "GB"},
            },
            profile="work",
        )
        assert (await service.get_setting(OWNER, "common", "timezone")).value == "Europe/Lisbon"
        assert (
            await service.get_setting(OWNER, "spotify", "default_market", profile="work")
        ).value == "GB"
        assert (
            await service.get_setting(OWNER, "spotify", "default_market", profile="personal")
        ).value is None

    async def test_a_document_of_profile_keys_without_a_profile_names_every_one(
        self, service: SettingsService
    ) -> None:
        with pytest.raises(
            SettingScopeError,
            match=r"spotify\.default_market, spotify\.max_batch_size are",
        ):
            await service.update(OWNER, {"spotify": {"default_market": "GB", "max_batch_size": 10}})

    async def test_reset_namespace_without_a_profile_clears_only_account_keys(
        self, service: SettingsService
    ) -> None:
        await service.set_setting(OWNER, "search", "max_content_chars", 5_000)
        await service.set_setting(OWNER, "search", "safe_search", "strict", profile="personal")
        written = await service.reset_namespace(OWNER, "search")
        assert "search.max_content_chars" in written.changed
        assert "search.safe_search" not in written.changed
        assert (
            await service.get_setting(OWNER, "search", "safe_search", profile="personal")
        ).value == "strict"

    async def test_reset_namespace_with_a_profile_clears_only_that_profiles_keys(
        self, service: SettingsService
    ) -> None:
        await service.set_setting(OWNER, "search", "safe_search", "strict", profile="personal")
        await service.set_setting(OWNER, "search", "safe_search", "off", profile="work")
        written = await service.reset_namespace(OWNER, "search", profile="personal")
        assert written.changed == ("search.safe_search",)
        assert (
            await service.get_setting(OWNER, "search", "safe_search", profile="work")
        ).value == "off"

    async def test_v1_import_lands_profile_keys_on_personal(self, service: SettingsService) -> None:
        written = await service.import_document(
            OWNER,
            {
                "common": {"timezone": "Europe/Lisbon"},
                "spotify": {"default_market": "GB"},
            },
            version=1,
        )
        assert written.any_change is True
        assert (await service.get_setting(OWNER, "common", "timezone")).value == "Europe/Lisbon"
        assert (
            await service.get_setting(
                OWNER, "spotify", "default_market", profile=V1_PROFILE_FALLBACK
            )
        ).value == "GB"

    async def test_v2_export_splits_account_from_profile_rows(
        self, service: SettingsService
    ) -> None:
        await service.update(
            OWNER,
            {
                "common": {"timezone": "Europe/Lisbon"},
                "spotify": {"default_market": "GB"},
            },
            profile="work",
        )
        exported = await service.export(OWNER)
        assert exported.version == EXPORT_VERSION == 2
        assert exported.settings == {"common": {"timezone": "Europe/Lisbon"}}
        assert exported.profiles == {"work": {"spotify": {"default_market": "GB"}}}

    async def test_a_star_is_not_a_profile_name(self, service: SettingsService) -> None:
        with pytest.raises(InvalidSettingValueError, match="reserved for account-scoped"):
            await service.set_setting(OWNER, "spotify", "default_market", "GB", profile="*")

    async def test_a_service_resolves_the_named_profile(self, service: SettingsService) -> None:
        await service.set_setting(OWNER, "spotify", "default_market", "PT", profile="work")
        document = await service.resolve_for(SPOTIFY_SERVICE, "spotify", profile="work")
        assert document.namespaces["spotify"]["default_market"].value == "PT"


class TestTheStoreCapsNamedProfiles:
    async def test_a_thirty_third_profile_name_is_refused(self, store: SqlSettingsStore) -> None:
        for index in range(MAX_PROFILES_PER_ACCOUNT):
            await store_apply(
                store,
                Change(
                    namespace="spotify",
                    key="default_market",
                    value="GB",
                    profile=f"p{index:02d}",
                ),
            )
        with pytest.raises(InvalidSettingValueError, match="at most 32 profiles"):
            await store_apply(
                store,
                Change(
                    namespace="spotify",
                    key="default_market",
                    value="PT",
                    profile="p32",
                ),
            )


class TestScopeOnTheEntry:
    def test_each_scope_explains_the_effect(self) -> None:
        for scope in SettingScope:
            assert scope.detail
