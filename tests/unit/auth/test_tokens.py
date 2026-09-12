"""Turning a bearer token into an identity, or into one undifferentiated refusal.

The attacks come first, because they are the reason the algorithm is pinned: ``alg: none``
with an empty signature, and RS256 downgraded to HS256 signed with the public key anybody
can fetch from the JWKS endpoint. Then every required claim individually missing, then the
clock, then the audience rules on each surface.

One test collects the message from every refusal path into a set and asserts its size is
one. Two spellings of "no" are two answers a forger can tell apart.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

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
from tests.fakes.clock import EPOCH, FakeClock
from tests.fakes.keyring import (
    ISSUER,
    JWKS_URL,
    ROTATED_KEY,
    FakeKeyring,
    forge_hs256,
    forge_unsigned,
    mint,
)

ALLOWED = ("common", "media", "search", "spotify", "user")
MEDIA = ServiceGrant(
    service="media-tool", audience_prefix="media-tool", namespaces=frozenset({"media"})
)


def build(keyring: FakeKeyring, clock: FakeClock) -> TokenVerifier:
    jwks = JwksClient(
        url=JWKS_URL, clock=clock, cache_seconds=3600, min_refetch_seconds=60, timeout_seconds=5
    )
    jwks._client = httpx.AsyncClient(transport=keyring.transport())
    return TokenVerifier(
        jwks=jwks,
        issuer=ISSUER,
        audience_prefix="settings",
        allowed_namespaces=ALLOWED,
        clock=clock,
    )


@pytest.fixture
def verifier(keyring: FakeKeyring, clock: FakeClock) -> TokenVerifier:
    return build(keyring, clock)


class TestTheAttacks:
    async def test_alg_none_is_refused(self, verifier: TokenVerifier) -> None:
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(forge_unsigned())

    async def test_hs256_signed_with_the_published_public_key_is_refused(
        self, verifier: TokenVerifier
    ) -> None:
        # The classic downgrade. The JWKS endpoint is published for anybody to fetch, so
        # "only keyring has the key" was never true of the public half.
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(forge_hs256())

    async def test_a_token_signed_with_a_key_keyring_does_not_publish_is_refused(
        self, verifier: TokenVerifier
    ) -> None:
        # Signed with a real RSA key -- just not one in the JWKS document.
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(mint(key=ROTATED_KEY))

    def test_the_algorithm_is_pinned(self) -> None:
        assert ALGORITHM == "RS256"


class TestRequiredClaims:
    @pytest.mark.parametrize("claim", REQUIRED_CLAIMS)
    async def test_omitting_any_one_is_refused(self, verifier: TokenVerifier, claim: str) -> None:
        # PyJWT verifies most claims only when present, so "no audience" would otherwise be
        # a token that PASSES the audience check.
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(mint(omit=claim))

    def test_the_list_is_the_five_keyring_always_sets(self) -> None:
        assert set(REQUIRED_CLAIMS) == {"exp", "iat", "iss", "sub", "aud"}


class TestTheOtherRules:
    async def test_the_wrong_issuer_is_refused(self, verifier: TokenVerifier) -> None:
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(mint(issuer="https://someone-else.test"))

    async def test_an_expired_token_is_refused_on_the_injected_clock(
        self, verifier: TokenVerifier, clock: FakeClock
    ) -> None:
        token = mint(ttl_seconds=900)
        await verifier.verify_owner(token)
        clock.advance(timedelta(seconds=900))
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(token)

    async def test_a_token_issued_in_the_wall_clocks_future_is_still_accepted(
        self, verifier: TokenVerifier, clock: FakeClock
    ) -> None:
        # Why verify_iat is off: PyJWT's only iat rule is "not in the future BY THE WALL
        # CLOCK", so a test pinning the injected clock to 2026 would watch every good token
        # refused by a rule it never asked for. Every token in this suite is issued at the
        # fake EPOCH, which may be ahead of or behind the real clock -- and is accepted.
        far_future = EPOCH + timedelta(days=3650)
        clock.advance(timedelta(days=3650))
        identity = await verifier.verify_owner(mint(issued_at=far_future))
        assert identity.account_id == "account-a"

    @pytest.mark.parametrize("token", ["", "not.a.token", "a.b", "x" * 100, "eyJ.eyJ.sig"])
    async def test_a_malformed_token_is_refused(self, verifier: TokenVerifier, token: str) -> None:
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(token)

    async def test_a_header_without_a_kid_is_refused(self, verifier: TokenVerifier) -> None:
        import jwt

        from tests.fakes.keyring import private_pem

        claims: dict[str, Any] = {
            "iss": ISSUER,
            "sub": "a",
            "aud": "settings",
            "iat": 0,
            "exp": 2**31,
        }
        token = jwt.encode(claims, private_pem(), algorithm="RS256")
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(token)

    async def test_a_kid_that_is_not_a_string_is_refused(self, verifier: TokenVerifier) -> None:
        # PyJWT will not encode a numeric kid, so -- as with the forgeries -- the token is
        # assembled by hand, exactly as an attacker would.
        import base64
        import json

        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "RS256", "typ": "JWT", "kid": 12345}).encode()
        ).rstrip(b"=")
        payload = base64.urlsafe_b64encode(b'{"iss":"x","sub":"a","aud":"settings"}').rstrip(b"=")
        token = (header + b"." + payload + b".c2ln").decode()
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(token)

    async def test_a_payload_that_is_not_decodable_is_refused(
        self, verifier: TokenVerifier
    ) -> None:
        # A well-formed header naming a real kid, so the key lookup succeeds, followed by a
        # payload that is not base64 JSON at all. The audience read is where it fails.
        import base64
        import json

        from tests.fakes.keyring import thumbprint

        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "RS256", "typ": "JWT", "kid": thumbprint()}).encode()
        ).rstrip(b"=")
        # Valid base64 of bytes that are not JSON: PyJWT's header read tolerates it (a JWS
        # payload is opaque bytes) and the audience read, which parses it as JWT claims,
        # does not.
        payload = base64.urlsafe_b64encode(b"not json at all").rstrip(b"=")
        token = (header + b"." + payload + b".c2ln").decode()
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(token)

    async def test_an_audience_that_is_a_list_is_refused(self, verifier: TokenVerifier) -> None:
        # A token naming several audiences is one whose holder is entitled somewhere else
        # too, and "which did you mean" would have to be answered by guessing.
        import jwt

        from tests.fakes.keyring import private_pem, thumbprint

        claims: dict[str, Any] = {
            "iss": ISSUER,
            "sub": "a",
            "aud": ["settings", "media-tool"],
            "iat": 0,
            "exp": 2**31,
        }
        token = jwt.encode(claims, private_pem(), algorithm="RS256", headers={"kid": thumbprint()})
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(token)

    async def test_an_unknown_kid_is_refused(self, verifier: TokenVerifier) -> None:
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_owner(mint(kid="nobody"))

    async def test_keyring_being_unreachable_passes_through_untouched(
        self, keyring: FakeKeyring, verifier: TokenVerifier
    ) -> None:
        # Deliberately NOT an AuthenticationError: turning it into one would tell a person
        # to log in again because another service was briefly down.
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
        # A service's compartment is a deployment decision; letting the token widen it
        # would mean a service that could mint its own scopes.
        identity = await verifier.verify_for_service(
            mint(audience="media-tool.everything"), grant=MEDIA
        )
        assert identity.namespaces == frozenset({"media", COMMON})

    @pytest.mark.parametrize("audience", ["settings", "spotify", "user", "media-toolkit", "media"])
    async def test_a_token_from_another_family_is_refused(
        self, verifier: TokenVerifier, audience: str
    ) -> None:
        # THE confused-deputy test. A static service token plus any user token must not
        # read any account; media-tool may present only tokens minted for media-tool.
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_for_service(mint(audience=audience), grant=MEDIA)

    async def test_the_persons_own_settings_token_is_refused_on_this_surface(
        self, verifier: TokenVerifier
    ) -> None:
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_for_service(mint(audience="settings"), grant=MEDIA)

    async def test_every_token_rule_still_applies(self, verifier: TokenVerifier) -> None:
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_for_service(forge_hs256(audience="media-tool"), grant=MEDIA)
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await verifier.verify_for_service(mint(audience="media-tool", omit="exp"), grant=MEDIA)


class TestEveryRefusalIsIdentical:
    async def test_the_messages_from_every_path_form_a_set_of_one(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        verifier = build(keyring, clock)
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
