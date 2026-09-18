"""Who a request is for, on both surfaces, and the ETag precondition parser.

The order of the two credentials on the internal surface is a property in its own right:
the service token is checked first, so an unauthenticated caller cannot use the endpoint
as a free oracle for whether arbitrary user tokens are valid.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from settings_api.api.dependencies import (
    IF_MATCH_HEADER,
    USER_TOKEN_HEADER,
    revision_from_if_match,
)
from settings_api.auth.tokens import Identity
from settings_api.domain.errors import RevisionMismatchError
from tests.conftest import DOWNSTREAM_TOKEN, SPOTIFY_TOKEN, auth, service_auth, token
from tests.fakes.keyring import FakeKeyring, mint

ME = Identity(account_id="account-a", audience="settings", namespaces=frozenset())


class TestThePersonFacingDependency:
    async def test_no_token_is_401_in_our_shape(self, client: AsyncClient) -> None:
        # HTTPBearer alone would answer a bare 403 with a plain JSON body -- a different
        # status and shape from every other failure this service produces.
        response = await client.get("/v1/settings")
        assert response.status_code == 401
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["detail"] == "a token is required"

    async def test_a_bad_token_is_401(self, client: AsyncClient) -> None:
        response = await client.get("/v1/settings", headers=auth("garbage"))
        assert response.status_code == 401

    async def test_a_good_token_binds_the_account_for_logging(self, client: AsyncClient) -> None:
        response = await client.get("/v1/settings", headers=auth(token()))
        assert response.status_code == 200


class TestTheServiceFacingDependency:
    async def test_both_credentials_are_required(self, client: AsyncClient) -> None:
        user = mint(audience="spotify")
        only_service = await client.get(
            "/v1/internal/settings/spotify", headers={"Authorization": f"Bearer {SPOTIFY_TOKEN}"}
        )
        only_user = await client.get(
            "/v1/internal/settings/spotify", headers={USER_TOKEN_HEADER: user}
        )
        assert only_service.status_code == 401
        assert only_service.json()["detail"] == "a user token is required"
        assert only_user.status_code == 401

    async def test_the_service_token_is_checked_before_the_user_token_is_touched(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        # A bad service token plus a VALID user token: refused without keyring ever being
        # asked for a key, so this endpoint cannot be used as a token oracle.
        response = await client.get(
            "/v1/internal/settings/spotify",
            headers=service_auth("not-a-service", mint(audience="spotify")),
        )
        assert response.status_code == 401
        assert keyring.fetches == 0

    async def test_a_good_pair_works(self, client: AsyncClient) -> None:
        response = await client.get(
            "/v1/internal/settings/spotify",
            headers=service_auth(SPOTIFY_TOKEN, mint(audience="spotify")),
        )
        assert response.status_code == 200

    async def test_the_users_token_must_be_from_the_callers_family(
        self, client: AsyncClient
    ) -> None:
        response = await client.get(
            "/v1/internal/settings/environments",
            headers=service_auth(DOWNSTREAM_TOKEN, mint(audience="spotify")),
        )
        assert response.status_code == 401


class TestRevisionFromIfMatch:
    def test_absent_means_no_precondition(self) -> None:
        assert revision_from_if_match(None, ME) is None

    @pytest.mark.parametrize("tag", ['"account-a.7"', "account-a.7", '  "account-a.7"  '])
    def test_a_well_formed_tag_yields_the_revision(self, tag: str) -> None:
        assert revision_from_if_match(tag, ME) == 7

    @pytest.mark.parametrize(
        "tag",
        [
            'W/"account-a.7"',
            '"account-b.7"',
            '"account-a"',
            '"account-a.x"',
            '"7"',
            '""',
            '"account-a.-1"',
        ],
        ids=["weak", "other account", "no dot", "non-numeric", "no account", "empty", "negative"],
    )
    def test_anything_else_is_a_failed_precondition(self, tag: str) -> None:
        # 412 rather than 400: treating it as malformed would invite a client to strip
        # the header and retry, which is exactly the write the precondition prevents.
        with pytest.raises(
            RevisionMismatchError, match="not one this account was given"
        ) as refusal:
            revision_from_if_match(tag, ME)
        assert "account-b" not in str(refusal.value)

    def test_the_header_names_are_the_standard_ones(self) -> None:
        assert IF_MATCH_HEADER == "If-Match"
        assert USER_TOKEN_HEADER == "X-Settings-User-Token"
