"""The service-facing surface: two credentials, one namespace, and the confused deputy.

The properties named in the test names are the ones the whole surface rests on: a service
cannot read another service's namespace, cannot present a token from another audience
family, and gets its 403 even when it sends a matching ETag -- the permission check runs
before the 304 is considered.
"""

from __future__ import annotations

import json

from httpx import AsyncClient

from tests.conftest import (
    DOWNSTREAM_TOKEN,
    SPOTIFY_TOKEN,
    USER_API_TOKEN,
    auth,
    service_auth,
    set_setting,
    token,
)
from tests.fakes.keyring import mint

SPOTIFY_USER = mint(audience="spotify")
DOWNSTREAM_USER = mint(audience="downstream-tool")
USER_API_USER = mint(audience="user.health")


class TestResolve:
    async def test_a_service_reads_its_namespace_with_common_merged_underneath(
        self, client: AsyncClient
    ) -> None:
        await set_setting(client, token(), "common", "timezone", "Europe/Lisbon")
        await set_setting(client, token(), "spotify", "default_market", "PT")
        response = await client.get(
            "/v1/internal/settings/spotify",
            headers=service_auth(SPOTIFY_TOKEN, SPOTIFY_USER),
            params={"profile": "personal"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["namespace"] == "spotify"
        assert body["revision"] == 2
        assert body["settings"]["default_market"] == "PT"
        assert body["settings"]["timezone"] == "Europe/Lisbon"
        assert body["settings"]["default_profile"] == "personal"
        assert response.headers["ETag"] == '"account-a.2"'
        assert response.headers["Cache-Control"] == "private, max-age=60"

    async def test_the_fallbacks_travel_with_the_values(self, client: AsyncClient) -> None:
        # What lets a client behave correctly during an outage without vendoring a copy of
        # the catalogue -- a copy that would drift, silently, because the only time it is
        # read is during an outage.
        body = (
            await client.get(
                "/v1/internal/settings/spotify", headers=service_auth(SPOTIFY_TOKEN, SPOTIFY_USER)
            )
        ).json()
        assert body["fallbacks"]["default_profile"] == {
            "default": "personal",
            "on_unavailable": "refuse",
        }
        assert body["fallbacks"]["default_market"] == {
            "default": None,
            "on_unavailable": "use_default",
        }
        assert set(body["fallbacks"]) == set(body["settings"])

    async def test_a_service_cannot_read_another_services_namespace(
        self, client: AsyncClient
    ) -> None:
        response = await client.get(
            "/v1/internal/settings/spotify", headers=service_auth(DOWNSTREAM_TOKEN, DOWNSTREAM_USER)
        )
        assert response.status_code == 403
        assert "does not grant the spotify" in response.json()["detail"]

    async def test_a_service_cannot_present_a_token_from_another_family(
        self, client: AsyncClient
    ) -> None:
        # The confused deputy: spotify-api's static token plus a downstream-tool user token.
        response = await client.get(
            "/v1/internal/settings/spotify", headers=service_auth(SPOTIFY_TOKEN, DOWNSTREAM_USER)
        )
        assert response.status_code == 401

    async def test_the_persons_own_settings_token_is_refused_here(
        self, client: AsyncClient
    ) -> None:
        response = await client.get(
            "/v1/internal/settings/spotify", headers=service_auth(SPOTIFY_TOKEN, token())
        )
        assert response.status_code == 401

    async def test_an_unknown_namespace_is_404(self, client: AsyncClient) -> None:
        assert (
            await client.get(
                "/v1/internal/settings/nope", headers=service_auth(SPOTIFY_TOKEN, SPOTIFY_USER)
            )
        ).status_code == 404

    async def test_a_dotted_audience_in_the_family_is_accepted(self, client: AsyncClient) -> None:
        response = await client.get(
            "/v1/internal/settings/user", headers=service_auth(USER_API_TOKEN, USER_API_USER)
        )
        assert response.status_code == 200
        assert "erasure_mode" in response.json()["settings"]


class TestRevalidation:
    async def test_a_matching_if_none_match_is_304_with_headers_and_no_body(
        self, client: AsyncClient
    ) -> None:
        first = await client.get(
            "/v1/internal/settings/spotify", headers=service_auth(SPOTIFY_TOKEN, SPOTIFY_USER)
        )
        etag = first.headers["ETag"]
        again = await client.get(
            "/v1/internal/settings/spotify",
            headers={**service_auth(SPOTIFY_TOKEN, SPOTIFY_USER), "If-None-Match": etag},
        )
        assert again.status_code == 304
        assert again.content == b""
        assert again.headers["ETag"] == etag
        assert again.headers["Cache-Control"] == "private, max-age=60"

    async def test_a_star_matches_and_a_list_of_tags_is_honoured(self, client: AsyncClient) -> None:
        headers = service_auth(SPOTIFY_TOKEN, SPOTIFY_USER)
        assert (
            await client.get(
                "/v1/internal/settings/spotify", headers={**headers, "If-None-Match": "*"}
            )
        ).status_code == 304
        assert (
            await client.get(
                "/v1/internal/settings/spotify",
                headers={**headers, "If-None-Match": '"x.1", "account-a.0"'},
            )
        ).status_code == 304

    async def test_a_stale_tag_is_200_with_the_new_values(self, client: AsyncClient) -> None:
        headers = service_auth(SPOTIFY_TOKEN, SPOTIFY_USER)
        etag = (await client.get("/v1/internal/settings/spotify", headers=headers)).headers["ETag"]
        await set_setting(client, token(), "spotify", "default_market", "GB")
        response = await client.get(
            "/v1/internal/settings/spotify",
            headers={**headers, "If-None-Match": etag},
            params={"profile": "personal"},
        )
        assert response.status_code == 200
        assert response.json()["settings"]["default_market"] == "GB"

    async def test_the_permission_check_runs_before_the_304(self, client: AsyncClient) -> None:
        # A service whose grant does not cover the namespace must not be told its cached
        # copy is still good.
        response = await client.get(
            "/v1/internal/settings/spotify",
            headers={**service_auth(DOWNSTREAM_TOKEN, DOWNSTREAM_USER), "If-None-Match": "*"},
        )
        assert response.status_code == 403


class TestSetForUser:
    async def test_a_service_writes_within_its_own_namespace_and_the_provenance_says_so(
        self, client: AsyncClient
    ) -> None:
        response = await client.put(
            "/v1/internal/settings/spotify/default_market",
            json={"value": "PT"},
            headers=service_auth(SPOTIFY_TOKEN, SPOTIFY_USER),
            params={"profile": "personal"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "revision": 1,
            "changed": ["spotify.default_market"],
            "unchanged": False,
        }
        assert response.headers["ETag"] == '"account-a.1"'
        (event,) = (await client.get("/v1/settings/events", headers=auth(token()))).json()["events"]
        assert event["actor"] == "service:spotify-api"
        assert event["service"] == "spotify-api"
        # And the person sees it.
        mine = (
            await client.get(
                "/v1/settings/spotify/default_market",
                headers=auth(token()),
                params={"profile": "personal"},
            )
        ).json()
        assert mine["value"] == "PT"

    async def test_a_service_cannot_write_another_services_namespace(
        self, client: AsyncClient
    ) -> None:
        response = await client.put(
            "/v1/internal/settings/user/grace_days",
            json={"value": 7},
            headers=service_auth(DOWNSTREAM_TOKEN, DOWNSTREAM_USER),
        )
        assert response.status_code == 403

    async def test_an_owner_only_setting_is_403_even_with_a_valid_user_token(
        self, client: AsyncClient
    ) -> None:
        # Held back for the person, with a token minted for settings itself.
        response = await client.put(
            "/v1/internal/settings/search/store_query_history",
            json={"value": True},
            headers=service_auth("user-api-service-token-0123456789abcdef", mint(audience="user")),
        )
        # user-api is not granted search at all, so this is the grant refusing first;
        # the owner-only rule is proven at the service layer. Both are 403.
        assert response.status_code == 403

    async def test_a_bad_value_is_422_and_a_profile_is_422(self, client: AsyncClient) -> None:
        headers = service_auth(SPOTIFY_TOKEN, SPOTIFY_USER)
        assert (
            await client.put(
                "/v1/internal/settings/spotify/default_market",
                json={"value": "gb"},
                headers=headers,
                params={"profile": "personal"},
            )
        ).status_code == 422
        assert (
            await client.put(
                "/v1/internal/settings/spotify/default_market",
                json={"value": "GB", "profile": "work"},
                headers=headers,
            )
        ).status_code == 422

    async def test_the_persons_token_is_refused_on_the_write_too(self, client: AsyncClient) -> None:
        response = await client.put(
            "/v1/internal/settings/spotify/default_market",
            json={"value": "GB"},
            headers=service_auth(SPOTIFY_TOKEN, token()),
        )
        assert response.status_code == 401

    async def test_no_account_id_appears_anywhere_in_the_request(self, client: AsyncClient) -> None:
        # There is no parameter by which a service can name an account, so a cross-account
        # write is not forbidden -- it is inexpressible. The path has the namespace and
        # the key, and nothing else.
        response = await client.put(
            "/v1/internal/settings/spotify/default_market",
            json={"value": "GB", "account_id": "account-b"},
            headers=service_auth(SPOTIFY_TOKEN, SPOTIFY_USER),
        )
        assert response.status_code == 422
        assert json.dumps("account-b") not in response.text
