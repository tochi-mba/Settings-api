"""The twelve person-facing operations, over HTTP.

Two things here are pinned as properties rather than as behaviours of one route: route
ORDER (the three literal paths must not be swallowed by ``/{namespace}``), and invariant 6
-- ``{"profile": "work"}`` in a body is a 422, never a field silently ignored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import AsyncClient

from tests.conftest import OTHER_ACCOUNT, auth, build_app, build_settings, set_setting, token
from tests.fakes.clock import FakeClock
from tests.fakes.keyring import FakeKeyring

OWNER = token()
SEARCH_ONLY = token(namespace="search")


@pytest.fixture
async def pinned_client(tmp_path: Path, keyring: FakeKeyring) -> Any:
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {"user": {"log_values": {"pin": False}}, "search": {"safe_search": {"pin": "strict"}}}
        )
    )
    app = build_app(build_settings(tmp_path, policy_path=path), FakeClock(), keyring)
    async with (
        LifespanManager(app),
        AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        yield http


class TestRouteOrder:
    """Starlette matches in declaration order; these literal paths sit under a parameter."""

    async def test_schema_is_the_schema_not_a_namespace_called_schema(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/v1/settings/schema", headers=auth(OWNER))
        assert response.status_code == 200
        assert "count" in response.json()

    async def test_export_is_the_export(self, client: AsyncClient) -> None:
        response = await client.get("/v1/settings/export", headers=auth(OWNER))
        assert response.status_code == 200
        assert "exported_at" in response.json()

    async def test_events_is_the_events(self, client: AsyncClient) -> None:
        response = await client.get("/v1/settings/events", headers=auth(OWNER))
        assert response.status_code == 200
        assert "events" in response.json()


class TestGetSettings:
    async def test_it_never_404s_for_an_account_that_has_written_nothing(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/v1/settings", headers=auth(token(OTHER_ACCOUNT)))
        assert response.status_code == 200
        body = response.json()
        assert body["revision"] == 0
        assert body["settings"]["spotify"]["default_market"] is None
        assert response.headers["ETag"] == f'"{OTHER_ACCOUNT}.0"'

    async def test_it_is_bounded_by_the_token(self, client: AsyncClient) -> None:
        body = (await client.get("/v1/settings", headers=auth(SEARCH_ONLY))).json()
        assert set(body["settings"]) == {"search", "common"}

    async def test_the_etag_moves_with_a_write_and_not_with_a_repeat(
        self, client: AsyncClient
    ) -> None:
        before = (await client.get("/v1/settings", headers=auth(OWNER))).headers["ETag"]
        await set_setting(client, OWNER)
        after = (await client.get("/v1/settings", headers=auth(OWNER))).headers["ETag"]
        await set_setting(client, OWNER)
        again = (await client.get("/v1/settings", headers=auth(OWNER))).headers["ETag"]
        assert before != after == again

    async def test_without_a_token_it_is_401(self, client: AsyncClient) -> None:
        assert (await client.get("/v1/settings")).status_code == 401


class TestGetNamespace:
    async def test_one_namespace(self, client: AsyncClient) -> None:
        body = (await client.get("/v1/settings/spotify", headers=auth(OWNER))).json()
        assert set(body["settings"]) == {"spotify"}

    async def test_unknown_is_404_and_ungranted_is_403(self, client: AsyncClient) -> None:
        # Two different answers, both safe: the catalogue is published to every token.
        assert (await client.get("/v1/settings/nope", headers=auth(SEARCH_ONLY))).status_code == 404
        assert (
            await client.get("/v1/settings/spotify", headers=auth(SEARCH_ONLY))
        ).status_code == 403


class TestGetSetting:
    async def test_it_explains_why_the_value_is_what_it_is(self, client: AsyncClient) -> None:
        body = (await client.get("/v1/settings/spotify/default_market", headers=auth(OWNER))).json()
        assert body == {
            "namespace": "spotify",
            "key": "default_market",
            "value": None,
            "set": False,
            "source": "default",
            "pinned": False,
        }
        await set_setting(client, OWNER)
        body = (await client.get("/v1/settings/spotify/default_market", headers=auth(OWNER))).json()
        assert (body["value"], body["set"], body["source"]) == ("GB", True, "account")

    async def test_an_unknown_key_is_404(self, client: AsyncClient) -> None:
        response = await client.get("/v1/settings/spotify/nope", headers=auth(OWNER))
        assert response.status_code == 404
        assert "describe_settings" in response.json()["detail"]


class TestDescribeSettings:
    async def test_every_setting_is_described_with_bounds(self, client: AsyncClient) -> None:
        response = await client.get("/v1/settings/schema", headers=auth(OWNER))
        body = response.json()
        assert body["count"] == 46 == len(body["settings"])
        assert response.headers["ETag"] == '"account-a.0"'
        market = next(
            s
            for s in body["settings"]
            if s["namespace"] == "spotify" and s["key"] == "default_market"
        )
        assert market["type"] == "str"
        assert market["set"] is False
        assert market["pinned"] is False
        assert market["bounds"]["pattern"] == "^[A-Z]{2}$"
        assert market["bounds"]["nullable"] is True
        assert market["on_unavailable"] == "use_default"
        assert market["origin"] == "existing"
        assert market["owner_writable_only"] is False
        enum = next(
            s for s in body["settings"] if s["key"] == "erasure_mode" and s["namespace"] == "user"
        )
        assert enum["bounds"]["choices"] == ["grace", "immediate", "tombstone"]

    async def test_a_pinned_setting_says_so(self, pinned_client: AsyncClient) -> None:
        body = (await pinned_client.get("/v1/settings/schema", headers=auth(OWNER))).json()
        flag = next(
            s for s in body["settings"] if s["namespace"] == "user" and s["key"] == "log_values"
        )
        assert flag["pinned"] is True
        assert flag["source"] == "policy"


class TestSetSetting:
    async def test_a_write_returns_the_revision_and_the_etag(self, client: AsyncClient) -> None:
        response = await client.put(
            "/v1/settings/spotify/default_market", json={"value": "GB"}, headers=auth(OWNER)
        )
        assert response.status_code == 200
        assert response.json() == {
            "revision": 1,
            "changed": ["spotify.default_market"],
            "unchanged": False,
        }
        assert response.headers["ETag"] == '"account-a.1"'

    async def test_an_identical_write_is_unchanged(self, client: AsyncClient) -> None:
        await set_setting(client, OWNER)
        body = await set_setting(client, OWNER)
        assert body == {"revision": 1, "changed": [], "unchanged": True}

    @pytest.mark.parametrize(
        ("namespace", "key", "value", "phrase"),
        [
            ("spotify", "default_market", "gb", "not in the form"),
            ("user", "log_values", "true", "must be true or false"),
            ("user", "grace_days", "7", "must be a whole number"),
            ("user", "grace_days", 9999, "may not be above"),
            ("search", "safe_search", "extreme", "must be one of"),
            ("search", "disabled_providers", "acme", "must be a list"),
        ],
    )
    async def test_a_bad_value_is_422_naming_the_rule_and_never_the_value(
        self, client: AsyncClient, namespace: str, key: str, value: Any, phrase: str
    ) -> None:
        response = await client.put(
            f"/v1/settings/{namespace}/{key}", json={"value": value}, headers=auth(OWNER)
        )
        assert response.status_code == 422
        assert phrase in response.json()["detail"]
        assert json.dumps(value) not in response.json()["detail"]

    async def test_a_float_is_refused_at_the_wire(self, client: AsyncClient) -> None:
        response = await client.put(
            "/v1/settings/user/grace_days", json={"value": 1.5}, headers=auth(OWNER)
        )
        assert response.status_code == 422
        assert "1.5" not in response.text

    async def test_a_credential_is_refused_naming_keyring(self, client: AsyncClient) -> None:
        response = await client.put(
            "/v1/settings/search/default_model",
            json={"value": "openai:ghp_" + "Ab3dEf7h" * 4},
            headers=auth(OWNER),
        )
        assert response.status_code == 422
        assert "keyring" in response.json()["detail"]
        assert "Ab3dEf7h" not in response.text

    async def test_a_pinned_setting_is_409_with_a_reason(self, pinned_client: AsyncClient) -> None:
        response = await pinned_client.put(
            "/v1/settings/user/log_values", json={"value": True}, headers=auth(OWNER)
        )
        assert response.status_code == 409
        assert "pinned" in response.json()["detail"]

    async def test_a_profile_in_the_body_is_422_not_ignored(self, client: AsyncClient) -> None:
        # Invariant 6. A caller whose profile field was ignored would believe it had
        # written a per-profile setting, and every service would read the other one.
        response = await client.put(
            "/v1/settings/spotify/default_market",
            json={"value": "GB", "profile": "work"},
            headers=auth(OWNER),
        )
        assert response.status_code == 422
        assert response.json()["errors"][0]["location"] == "body.profile"

    async def test_if_match_refuses_a_stale_write(self, client: AsyncClient) -> None:
        await set_setting(client, OWNER)
        stale = await client.put(
            "/v1/settings/spotify/default_market",
            json={"value": "PT"},
            headers={**auth(OWNER), "If-Match": '"account-a.0"'},
        )
        assert stale.status_code == 412
        fresh = await client.put(
            "/v1/settings/spotify/default_market",
            json={"value": "PT"},
            headers={**auth(OWNER), "If-Match": '"account-a.1"'},
        )
        assert fresh.status_code == 200

    async def test_ungranted_and_unknown(self, client: AsyncClient) -> None:
        assert (
            await client.put(
                "/v1/settings/spotify/default_market",
                json={"value": "GB"},
                headers=auth(SEARCH_ONLY),
            )
        ).status_code == 403
        assert (
            await client.put("/v1/settings/spotify/nope", json={"value": 1}, headers=auth(OWNER))
        ).status_code == 404
        assert (
            await client.put("/v1/settings/spotify/default_market", json={"value": "GB"})
        ).status_code == 401


class TestUpdateSettings:
    async def test_a_document_merges_and_is_all_or_nothing(self, client: AsyncClient) -> None:
        await set_setting(client, OWNER, "common", "timezone", "Europe/Lisbon")
        good = await client.put(
            "/v1/settings",
            json={"settings": {"spotify": {"default_market": "GB", "max_batch_size": 10}}},
            headers=auth(OWNER),
        )
        assert good.status_code == 200
        assert set(good.json()["changed"]) == {"spotify.default_market", "spotify.max_batch_size"}

        bad = await client.put(
            "/v1/settings",
            json={"settings": {"spotify": {"default_market": "PT", "max_batch_size": 9999}}},
            headers=auth(OWNER),
        )
        assert bad.status_code == 422
        values = (await client.get("/v1/settings", headers=auth(OWNER))).json()["settings"]
        assert (
            values["spotify"]["default_market"] == "GB"
        )  # the good key of the bad document was not written
        assert values["common"]["timezone"] == "Europe/Lisbon"  # merge, not replace

    async def test_unknown_keys_are_400_naming_every_one(self, client: AsyncClient) -> None:
        response = await client.put(
            "/v1/settings",
            json={"settings": {"spotify": {"nope": 1, "markte": "GB"}}},
            headers=auth(OWNER),
        )
        assert response.status_code == 400
        assert "spotify.markte, spotify.nope" in response.json()["detail"]
        assert "describe_settings" in response.json()["detail"]

    async def test_a_profile_at_the_top_level_is_422(self, client: AsyncClient) -> None:
        response = await client.put(
            "/v1/settings", json={"settings": {}, "profile": "work"}, headers=auth(OWNER)
        )
        assert response.status_code == 422

    async def test_if_match_is_honoured(self, client: AsyncClient) -> None:
        response = await client.put(
            "/v1/settings",
            json={"settings": {"spotify": {"default_market": "GB"}}},
            headers={**auth(OWNER), "If-Match": '"account-a.5"'},
        )
        assert response.status_code == 412


class TestResets:
    async def test_reset_setting_leaves_the_event(self, client: AsyncClient) -> None:
        await set_setting(client, OWNER)
        response = await client.delete("/v1/settings/spotify/default_market", headers=auth(OWNER))
        assert response.status_code == 200
        assert response.json()["changed"] == ["spotify.default_market"]
        events = (await client.get("/v1/settings/events", headers=auth(OWNER))).json()["events"]
        assert [event["action"] for event in events] == ["reset", "set"]

    async def test_reset_a_pinned_setting_is_409(self, pinned_client: AsyncClient) -> None:
        assert (
            await pinned_client.delete("/v1/settings/user/log_values", headers=auth(OWNER))
        ).status_code == 409

    async def test_reset_namespace(self, client: AsyncClient) -> None:
        await set_setting(client, OWNER)
        await set_setting(client, OWNER, "spotify", "max_batch_size", 10)
        response = await client.delete("/v1/settings/spotify", headers=auth(OWNER))
        assert response.status_code == 200
        assert set(response.json()["changed"]) == {
            "spotify.default_market",
            "spotify.max_batch_size",
        }

    async def test_reset_with_a_stale_if_match_is_412(self, client: AsyncClient) -> None:
        await set_setting(client, OWNER)
        headers = {**auth(OWNER), "If-Match": '"account-a.0"'}
        assert (
            await client.delete("/v1/settings/spotify/default_market", headers=headers)
        ).status_code == 412
        assert (await client.delete("/v1/settings/spotify", headers=headers)).status_code == 412


class TestForget:
    async def test_everything_goes_and_the_account_reads_as_new(self, client: AsyncClient) -> None:
        await set_setting(client, OWNER)
        await set_setting(client, OWNER, "common", "timezone", "Europe/Lisbon")
        response = await client.delete("/v1/settings", headers=auth(OWNER))
        assert response.status_code == 200
        assert response.json()["removed"] == 2
        assert "truncated" in response.json()["detail"]
        after = await client.get("/v1/settings", headers=auth(OWNER))
        assert after.headers["ETag"] == '"account-a.0"'
        assert (await client.get("/v1/settings/events", headers=auth(OWNER))).json()["events"] == []


class TestExportImport:
    async def test_round_trip(self, client: AsyncClient) -> None:
        await set_setting(client, OWNER)
        exported = (await client.get("/v1/settings/export", headers=auth(OWNER))).json()
        assert exported["settings"] == {"spotify": {"default_market": "GB"}}
        assert exported["version"] == 1
        assert exported["revision"] == 1

        await client.delete("/v1/settings/spotify/default_market", headers=auth(OWNER))
        imported = await client.post(
            "/v1/settings/import",
            json={"version": exported["version"], "settings": exported["settings"]},
            headers=auth(OWNER),
        )
        assert imported.status_code == 200
        assert imported.json()["changed"] == ["spotify.default_market"]
        events = (await client.get("/v1/settings/events", headers=auth(OWNER))).json()["events"]
        assert events[0]["action"] == "import"

    async def test_a_wrong_version_is_422(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/settings/import", json={"version": 99, "settings": {}}, headers=auth(OWNER)
        )
        assert response.status_code == 422
        assert "export format 1" in response.json()["detail"]

    async def test_an_unknown_key_is_400(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/settings/import",
            json={"version": 1, "settings": {"spotify": {"nope": 1}}},
            headers=auth(OWNER),
        )
        assert response.status_code == 400

    async def test_if_match_is_honoured_on_import(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/settings/import",
            json={"version": 1, "settings": {}},
            headers={**auth(OWNER), "If-Match": '"account-a.3"'},
        )
        assert response.status_code == 412


class TestEvents:
    async def test_paging_by_sequence_and_the_last_page_has_no_cursor(
        self, client: AsyncClient
    ) -> None:
        for market in ("GB", "PT", "ES", "FR", "DE"):
            await set_setting(client, OWNER, value=market)
        first = (await client.get("/v1/settings/events?limit=2", headers=auth(OWNER))).json()
        assert [event["revision"] for event in first["events"]] == [5, 4]
        assert first["next_before"] == first["events"][-1]["sequence"]
        second = (
            await client.get(
                f"/v1/settings/events?limit=2&before={first['next_before']}", headers=auth(OWNER)
            )
        ).json()
        assert [event["revision"] for event in second["events"]] == [3, 2]
        last = (
            await client.get(
                f"/v1/settings/events?limit=2&before={second['next_before']}", headers=auth(OWNER)
            )
        ).json()
        assert [event["revision"] for event in last["events"]] == [1]
        assert last["next_before"] is None

    async def test_an_event_names_what_changed_and_never_the_value(
        self, client: AsyncClient
    ) -> None:
        await set_setting(client, OWNER, value="GB")
        (event,) = (await client.get("/v1/settings/events", headers=auth(OWNER))).json()["events"]
        assert event["changed"] == ["spotify.default_market"]
        assert event["actor"] == "settings"
        assert event["service"] is None
        assert "GB" not in json.dumps(event)

    async def test_a_bad_limit_is_422(self, client: AsyncClient) -> None:
        assert (
            await client.get("/v1/settings/events?limit=0", headers=auth(OWNER))
        ).status_code == 422


class TestRenderingHelpers:
    def test_changed_names_tolerates_a_missing_or_malformed_detail(self) -> None:
        # Every event this service writes carries a list, so these arms are for a row
        # edited by hand -- which is a broken database, not a 500.
        from settings_api.api.routers.settings import _changed_names

        assert _changed_names(None) == []
        assert _changed_names({"changed": "not a list"}) == []
        assert _changed_names({"count": 1}) == []
        assert _changed_names({"changed": ["a.b", 7]}) == ["a.b", "7"]
