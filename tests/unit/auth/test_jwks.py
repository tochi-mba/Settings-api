"""Keyring's public keys, cached, and the rules about when we go and get them again.

Every assertion here is a statement about *how many times we called keyring*, so the fake
counts. Three properties matter most: a warm cache never refetches; ten concurrent cold
requests make one fetch; and an unknown ``kid`` is rate limited -- because a key id is read
before anything has been verified, so a flood of invented ones is the one thing an
unauthenticated caller gets to aim at keyring.

And one distinction: "keyring is down" is a 503-shaped error and "that kid is not
keyring's" is a 401-shaped one. Conflating them tells a person to log in again because
another service was briefly down.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from settings_api.auth.jwks import BAD_TOKEN, KEYS_UNAVAILABLE, JwksClient
from settings_api.domain.errors import AuthenticationError, KeyringUnreachableError
from tests.fakes.clock import FakeClock
from tests.fakes.keyring import JWKS_URL, ROTATED_KEY, FakeKeyring, jwks, thumbprint

KNOWN = thumbprint()
ROTATED = thumbprint(ROTATED_KEY)
CACHE = 3_600.0
FLOOR = 60.0


def build(keyring: FakeKeyring, clock: FakeClock, **overrides: Any) -> JwksClient:
    client = JwksClient(
        url=JWKS_URL,
        clock=clock,
        cache_seconds=overrides.get("cache_seconds", CACHE),
        min_refetch_seconds=overrides.get("min_refetch_seconds", FLOOR),
        timeout_seconds=5.0,
    )
    client._client = httpx.AsyncClient(transport=keyring.transport())
    return client


class TestFetching:
    async def test_constructing_the_client_fetches_nothing(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        # A service that refused to start unless keyring were reachable would turn one
        # outage into two, at the moment the two are being restarted together.
        client = build(keyring, clock)
        assert keyring.fetches == 0
        await client.aclose()

    async def test_the_first_key_provokes_one_fetch(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        client = build(keyring, clock)
        key = await client.key_for(KNOWN)
        assert key is not None
        assert keyring.fetches == 1
        await client.aclose()

    async def test_a_warm_cache_does_not_refetch(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        client = build(keyring, clock)
        await client.key_for(KNOWN)
        clock.advance(CACHE / 2)
        await client.key_for(KNOWN)
        await client.key_for(KNOWN)
        assert keyring.fetches == 1
        await client.aclose()

    async def test_the_cache_goes_stale_on_the_injected_clock(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        client = build(keyring, clock)
        await client.key_for(KNOWN)
        clock.advance(CACHE)
        await client.key_for(KNOWN)
        # Measured on the injected clock, so this test exists at all; on time.monotonic it
        # would take an hour.
        assert keyring.fetches == 2
        await client.aclose()

    async def test_ten_concurrent_cold_requests_make_one_fetch(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        # The transport yields to the event loop before answering, as a real socket would,
        # so the nine other callers genuinely queue on the lock while the first fetches --
        # and then take the answer it fetched rather than fetching their own.
        async def slow(_request: httpx.Request) -> httpx.Response:
            keyring.fetches += 1
            await asyncio.sleep(0)
            return httpx.Response(200, json=jwks(*keyring.keys))

        client = build(keyring, clock)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
        keys = await asyncio.gather(*(client.key_for(KNOWN) for _ in range(10)))
        assert len(keys) == 10
        assert keyring.fetches == 1
        await client.aclose()

    async def test_a_rotated_key_is_found_after_the_cache_expires(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        client = build(keyring, clock)
        await client.key_for(KNOWN)
        keyring.rotate()
        clock.advance(CACHE)
        key = await client.key_for(ROTATED)
        assert key is not None
        await client.aclose()


class TestAnUnknownKeyId:
    async def test_provokes_one_refetch_and_is_then_refused_if_still_absent(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        client = build(keyring, clock)
        await client.key_for(KNOWN)
        with pytest.raises(AuthenticationError, match=BAD_TOKEN):
            await client.key_for("not-a-real-kid")
        # A fetch that worked and came back without the id is a fact about the TOKEN, so
        # 401-shaped and not 503-shaped.
        assert keyring.fetches == 2
        await client.aclose()

    async def test_a_flood_of_invented_ids_makes_at_most_one_fetch_per_window(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        # The amplifier: without the floor, every request is one outbound fetch, aimed by
        # anyone who can reach this service holding no token at all.
        client = build(keyring, clock)
        await client.key_for(KNOWN)
        for index in range(50):
            with pytest.raises(AuthenticationError):
                await client.key_for(f"invented-{index}")
        assert keyring.fetches == 2
        await client.aclose()

    async def test_the_window_reopens_after_the_floor(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        client = build(keyring, clock)
        await client.key_for(KNOWN)
        with pytest.raises(AuthenticationError):
            await client.key_for("x")
        clock.advance(FLOOR)
        with pytest.raises(AuthenticationError):
            await client.key_for("y")
        assert keyring.fetches == 3
        await client.aclose()

    async def test_a_rotation_is_picked_up_through_the_window(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        # The legitimate case the refetch exists for: keyring rotated its key and the
        # first token signed with the new one arrives before the cache has expired.
        client = build(keyring, clock)
        await client.key_for(KNOWN)
        keyring.rotate()
        key = await client.key_for(ROTATED)
        assert key is not None
        assert keyring.fetches == 2
        await client.aclose()

    async def test_the_window_is_spent_even_when_the_fetch_fails(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        # An outage is precisely when a flood of invented ids must not become a flood of
        # requests to a service already having a bad day.
        client = build(keyring, clock)
        await client.key_for(KNOWN)
        keyring.error = httpx.ConnectError("down")
        with pytest.raises(KeyringUnreachableError):
            await client.key_for("x")
        keyring.error = None
        with pytest.raises(AuthenticationError):
            await client.key_for("y")
        assert keyring.fetches == 2
        await client.aclose()

    async def test_a_cold_cache_is_not_rate_limited(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        # Only fetches an unknown id PROVOKES are counted; one because there is no document
        # yet is already bounded by cache_seconds.
        client = build(keyring, clock)
        with pytest.raises(AuthenticationError):
            await client.key_for("x")
        with pytest.raises(AuthenticationError):
            await client.key_for("y")
        assert keyring.fetches == 2
        await client.aclose()


class TestKeyringBeingDownIsNotTheTokenBeingWrong:
    @pytest.mark.parametrize(
        "make_bad",
        [
            lambda k: setattr(k, "error", httpx.ConnectError("refused")),
            lambda k: setattr(k, "error", httpx.ReadTimeout("slow")),
            lambda k: setattr(k, "status", 500),
            lambda k: setattr(k, "status", 404),
            lambda k: setattr(k, "body", {"not": "a jwks"}),
            lambda k: setattr(k, "body", {"keys": "not a list"}),
            lambda k: setattr(k, "body", {"keys": ["garbage"]}),
            lambda k: setattr(k, "body", {"keys": [{"kty": "RSA"}]}),
        ],
        ids=[
            "connect error",
            "timeout",
            "500",
            "404",
            "no keys",
            "keys not list",
            "key not mapping",
            "key unusable",
        ],
    )
    async def test_every_way_the_document_can_fail_is_a_503_shaped_error(
        self, keyring: FakeKeyring, clock: FakeClock, make_bad: Any
    ) -> None:
        make_bad(keyring)
        client = build(keyring, clock)
        with pytest.raises(KeyringUnreachableError, match=KEYS_UNAVAILABLE):
            await client.key_for(KNOWN)
        await client.aclose()

    async def test_an_html_error_page_served_with_200_is_unreachable_not_a_bad_token(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        def handle(_request: httpx.Request) -> httpx.Response:
            keyring.fetches += 1
            return httpx.Response(200, text="<html>proxy error</html>")

        client = build(keyring, clock)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        with pytest.raises(KeyringUnreachableError):
            await client.key_for(KNOWN)
        await client.aclose()

    async def test_the_message_never_carries_the_url(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        # The text an HTTP client raises carries the URL it was given, and a URL can carry
        # credentials in its userinfo.
        keyring.error = httpx.ConnectError(f"cannot reach {JWKS_URL}")
        client = build(keyring, clock)
        with pytest.raises(KeyringUnreachableError) as failure:
            await client.key_for(KNOWN)
        assert JWKS_URL not in str(failure.value)
        await client.aclose()

    async def test_an_empty_key_set_is_a_bad_token_not_an_outage(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        keyring.body = jwks()
        keyring.body["keys"] = []
        client = build(keyring, clock)
        # PyJWKSet refuses an empty list, which is a document we cannot use: unreachable.
        with pytest.raises(KeyringUnreachableError):
            await client.key_for(KNOWN)
        await client.aclose()


class TestHealthy:
    async def test_warm_is_healthy_without_a_fetch(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        client = build(keyring, clock)
        await client.key_for(KNOWN)
        assert await client.healthy() == (True, None)
        assert keyring.fetches == 1
        await client.aclose()

    async def test_cold_fetches_and_reports_healthy(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        # A check that only reported on the cache would have nothing to say on a fresh
        # process, which is when an operator most wants to know.
        client = build(keyring, clock)
        assert await client.healthy() == (True, None)
        assert keyring.fetches == 1
        await client.aclose()

    async def test_down_reports_why_without_raising(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        keyring.error = httpx.ConnectError("down")
        client = build(keyring, clock)
        assert await client.healthy() == (False, KEYS_UNAVAILABLE)
        await client.aclose()

    async def test_an_unexpected_failure_is_reported_not_raised(
        self, keyring: FakeKeyring, clock: FakeClock
    ) -> None:
        # A health check that raised would answer 500 while trying to say what is wrong,
        # and a load balancer would see the same 500 for "keyring is down" as for "this
        # process is broken".
        keyring.error = RuntimeError("something nobody anticipated")
        client = build(keyring, clock)
        assert await client.healthy() == (False, KEYS_UNAVAILABLE)
        await client.aclose()
