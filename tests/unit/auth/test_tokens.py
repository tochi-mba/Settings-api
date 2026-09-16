"""Turning a bearer token into an identity, on either surface, or into one refusal.

The token rules themselves -- the pinned algorithm, the pinned issuer, every required claim,
the injected clock -- belong to :class:`keyring_client.TokenVerifier` and are tested
exhaustively in the keyring repository, against keyring's own signer. The few kept here prove
the adapter hands this service's issuer, clock and keys to that verifier rather than someone
else's.

The rest is what only this service decides: which namespaces a person's audience grants, that
a service may present only tokens from its own family, that namespaces on the internal surface
come from the grant, and that every refusal from every path is the same refusal.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from settings_api.auth.jwks import BAD_TOKEN, JwksClient
from settings_api.auth.tokens import (
    ALGORITHM,
    REQUIRED_CLAIMS,
    SERVICE_ACTOR_PREFIX,
    Identity,
    TokenVerifier,
)
from settings_api.domain.errors import AuthenticationError, KeyringUnreachableError
from settings_api.domain.namespaces import COMMON, ServiceGrant
from tests.fakes.clock import EPOCH
from tests.fakes.keyring import (
    ISSUER,
    JWKS_URL,
    ROTATED_KEY,
    FakeKeyring,
    forge_hs256,
    forge_unsigned,
    mint,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tests.fakes.clock import FakeClock

ALLOWED = ("common", "media", "search", "spotify", "user")
MEDIA = ServiceGrant(
    service="media-tool", audience_prefix="media-tool", namespaces=frozenset({"media"})
)


@pytest.fixture
async def verifier(keyring: FakeKeyring, clock: FakeClock) -> AsyncIterator[TokenVerifier]:
    jwks = JwksClient(url=JWKS_URL, clock=clock, transport=keyring.transport())
    yield TokenVerifier(
        jwks=jwks,
        issuer=ISSUER,
        audience_prefix="settings",
        allowed_namespaces=ALLOWED,
        clock=clock,
    )
    await jwks.aclose()


class TestTheSharedRulesAreWiredIn:
    def test_the_rules_are_the_familys(self) -> None:
        assert ALGORITHM == "RS256"
        assert set(REQUIRED_CLAIMS) == {"exp", "iat", "iss", "sub", "aud"}

    @pytest.mark.parametrize("forged", [forge_unsigned(), forge_hs256()], ids=["none", "hs256"])
    async def test_the_algorithm_confusion_attacks_are_refused(
        self, verifier: TokenVerifier, forged: str
    ) -> None:
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(forged)

    async def test_a_key_keyring_does_not_publish_is_refused(self, verifier: TokenVerifier) -> None:
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(mint(key=ROTATED_KEY))

    async def test_this_deployments_issuer_is_the_one_pinned(self, verifier: TokenVerifier) -> None:
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(mint(issuer="https://someone-else.test"))

    async def test_expiry_is_judged_on_this_services_injected_clock(
        self, verifier: TokenVerifier, clock: FakeClock
    ) -> None:
        token = mint(ttl_seconds=900)
        await verifier.verify_owner(token)

        clock.advance(timedelta(seconds=900))

        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(token)

    async def test_a_token_issued_ahead_of_the_wall_clock_is_accepted_on_the_injected_one(
        self, verifier: TokenVerifier, clock: FakeClock
    ) -> None:
        far_future = EPOCH + timedelta(days=3650)
        clock.advance(timedelta(days=3650))

        identity = await verifier.verify_owner(mint(issued_at=far_future))

        assert identity.account_id == "account-a"

    async def test_keyring_being_unreachable_is_not_turned_into_a_refusal(
        self, keyring: FakeKeyring, verifier: TokenVerifier
    ) -> None:
        # Turning it into an AuthenticationError would tell a person to log in again because
        # another service was briefly down.
        keyring.error = httpx.ConnectError("down")

        with pytest.raises(KeyringUnreachableError):
            await verifier.verify_owner(mint())


class TestThePersonFacingGrant:
    async def test_the_bare_audience_grants_every_namespace(self, verifier: TokenVerifier) -> None:
        identity = await verifier.verify_owner(mint(audience="settings"))

        assert identity.namespaces == frozenset(ALLOWED)
        assert identity.service is None
        assert identity.audience == "settings"

    async def test_a_dotted_audience_grants_that_namespace_and_common(
        self, verifier: TokenVerifier
    ) -> None:
        identity = await verifier.verify_owner(mint(audience="settings.search"))

        assert identity.namespaces == frozenset({"search", COMMON})
        assert identity.grants("search") is True
        assert identity.grants("user") is False

    @pytest.mark.parametrize(
        "audience", ["media-tool", "user", "settings.nope", "settings.", "settingsx"]
    )
    async def test_an_audience_outside_the_family_or_naming_an_unknown_namespace_is_refused(
        self, verifier: TokenVerifier, audience: str
    ) -> None:
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(mint(audience=audience))

    async def test_the_identity_is_the_verified_sub(self, verifier: TokenVerifier) -> None:
        identity = await verifier.verify_owner(mint(account_id="account-z"))

        assert identity.account_id == "account-z"
        assert identity.actor == "settings"


class TestTheServiceFacingGrant:
    @pytest.mark.parametrize("audience", ["media-tool", "media-tool.jobs"])
    async def test_a_token_from_the_services_own_family_is_accepted(
        self, verifier: TokenVerifier, audience: str
    ) -> None:
        identity = await verifier.verify_for_service(mint(audience=audience), grant=MEDIA)

        assert identity.service == "media-tool"
        assert identity.actor == f"{SERVICE_ACTOR_PREFIX}media-tool"

    async def test_namespaces_come_from_the_grant_not_the_token(
        self, verifier: TokenVerifier
    ) -> None:
        # A service's compartment is a deployment decision; letting the token widen it would
        # mean a service that could mint its own scopes.
        identity = await verifier.verify_for_service(
            mint(audience="media-tool.everything"), grant=MEDIA
        )

        assert identity.namespaces == frozenset({"media", COMMON})

    @pytest.mark.parametrize("audience", ["settings", "spotify", "user", "media-toolkit", "media"])
    async def test_a_token_from_another_family_is_refused(
        self, verifier: TokenVerifier, audience: str
    ) -> None:
        # THE confused-deputy test. A static service token plus any user token must not read
        # any account; media-tool may present only tokens minted for media-tool.
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_for_service(mint(audience=audience), grant=MEDIA)

    async def test_every_token_rule_still_applies(self, verifier: TokenVerifier) -> None:
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_for_service(forge_hs256(audience="media-tool"), grant=MEDIA)
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_for_service(mint(audience="media-tool", omit="exp"), grant=MEDIA)

    async def test_keyring_being_unreachable_is_still_a_503_here(
        self, keyring: FakeKeyring, verifier: TokenVerifier
    ) -> None:
        keyring.error = httpx.ConnectError("down")

        with pytest.raises(KeyringUnreachableError):
            await verifier.verify_for_service(mint(audience="media-tool"), grant=MEDIA)


class TestEveryRefusalIsIdentical:
    async def test_the_messages_from_every_path_form_a_set_of_one(
        self, verifier: TokenVerifier
    ) -> None:
        attempts = [
            forge_unsigned(),
            forge_hs256(),
            mint(key=ROTATED_KEY),
            mint(omit="aud"),
            mint(issuer="x"),
            mint(audience="media-tool"),
            mint(audience="settings.nope"),
            mint(kid="nobody"),
            "garbage",
        ]
        messages: set[str] = set()
        for token in attempts:
            with pytest.raises(AuthenticationError) as refusal:
                await verifier.verify_owner(token)
            messages.add(str(refusal.value))
        with pytest.raises(AuthenticationError) as refusal:
            await verifier.verify_for_service(mint(audience="settings"), grant=MEDIA)
        messages.add(str(refusal.value))

        # Which rule did the refusing goes to the logs, where the operator reads it and a
        # forger does not.
        assert messages == {BAD_TOKEN}


class TestIdentity:
    def test_it_is_frozen(self) -> None:
        identity = Identity(account_id="a", audience="settings", namespaces=frozenset())

        with pytest.raises(AttributeError):
            identity.account_id = "b"  # type: ignore[misc]
