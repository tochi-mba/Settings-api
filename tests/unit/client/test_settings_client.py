"""What the client promises six services, and the one design it deliberately avoids.

Four properties are pinned here, and each of them is something a consuming service would
otherwise have to get right for itself:

**The cache is keyed by the token, never by an unverified ``sub``.** That is the
vulnerability the client's module docstring describes, and
:class:`TestTheCacheIsKeyedByTheToken` is what stops somebody "optimising" it back in.

**Ten concurrent callers make one request.** A cold cache under load must not be a
thundering herd pointed at a service on everybody's critical path.

**An outage degrades in a declared order** -- this token's cached values, then the
per-setting fallback rule, then a refusal -- and a ``refuse`` key raises when it is *read*
rather than when the namespace is resolved.

**A 4xx is not an outage.** A misconfigured grant must surface as an error rather than
hiding behind defaults that happen to work.

The transport is a hand-written :class:`httpx.MockTransport`, not a mocking library: it is
a real transport serving real responses, so everything above it -- headers, status codes,
``If-None-Match`` handling, JSON parsing -- runs for real.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from settings_client import (
    HttpSettingsClient,
    SettingsClient,
    SettingsRefused,
    SettingsRejected,
    SettingsUnavailable,
)
from settings_client.models import Fallback, OnUnavailable, ResolvedSettings

if TYPE_CHECKING:
    from collections.abc import Callable

SERVICE_TOKEN = "spotify-service-token-0123456789abcdef"
TOKEN_A = "user-token-for-person-a"
TOKEN_B = "user-token-for-person-b"

BODY: dict[str, Any] = {
    "namespace": "spotify",
    "revision": 4,
    "settings": {"default_market": "PT", "timezone": "Europe/Lisbon"},
    "fallbacks": {
        "default_market": {"default": None, "on_unavailable": "use_default"},
        "timezone": {"default": "UTC", "on_unavailable": "use_default"},
    },
}
"""One namespace as settings-api actually returns it, fallbacks included."""

REFUSING_BODY: dict[str, Any] = {
    "namespace": "search",
    "revision": 2,
    "settings": {"disabled_providers": ["acme"], "max_content_chars": 40_000},
    "fallbacks": {
        "disabled_providers": {"default": [], "on_unavailable": "refuse"},
        "max_content_chars": {"default": 40_000, "on_unavailable": "use_default"},
    },
}


class Recorder:
    """A transport that serves canned responses and counts what it was asked.

    Hand-written rather than mocked, for the reason in the module docstring. The count is
    the assertion in every caching test -- those are all statements about *how many times
    we called settings-api*, so counting is the only way to make them.
    """

    def __init__(
        self,
        *,
        body: dict[str, Any] | None = None,
        etag: str = '"account-a.4"',
        status: int = 200,
    ) -> None:
        self.body = body if body is not None else BODY
        self.etag = etag
        self.status = status
        self.requests: list[httpx.Request] = []
        self.error: Exception | None = None
        self.handler: Callable[[httpx.Request], httpx.Response] | None = None

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.error is not None:
                raise self.error
            if self.handler is not None:
                return self.handler(request)
            if request.headers.get("If-None-Match") == self.etag:
                return httpx.Response(304, headers={"ETag": self.etag})
            return httpx.Response(self.status, json=self.body, headers={"ETag": self.etag})

        return httpx.MockTransport(handle)

    @property
    def count(self) -> int:
        return len(self.requests)


def build(recorder: Recorder, **overrides: Any) -> HttpSettingsClient:
    """A client wired to one recorder."""
    return HttpSettingsClient(
        base_url="http://settings.test",
        service_token=SERVICE_TOKEN,
        transport=recorder.transport(),
        **overrides,
    )


class TestBothCredentialsTravel:
    async def test_the_service_token_and_the_user_token_are_separate_headers(self) -> None:
        recorder = Recorder()
        client = build(recorder)
        await client.resolve("spotify", user_token=TOKEN_A)

        sent = recorder.requests[0]
        # Two headers because they are two different claims: which service is calling, and
        # who it is calling for. Overloading one would make it possible to send only one
        # and have it mean either.
        assert sent.headers["Authorization"] == f"Bearer {SERVICE_TOKEN}"
        assert sent.headers["X-Settings-User-Token"] == TOKEN_A
        await client.aclose()

    async def test_the_namespace_is_the_only_thing_in_the_path(self) -> None:
        recorder = Recorder()
        client = build(recorder)
        await client.resolve("spotify", user_token=TOKEN_A)

        # No account id anywhere. If one ever appears here it means the client started
        # deciding whose settings to ask for, which is the server's job.
        assert recorder.requests[0].url.path == "/v1/internal/settings/spotify"
        await client.aclose()


class TestTheCacheIsKeyedByTheToken:
    """The property that stops a forged token reading somebody else's cached settings."""

    async def test_a_second_call_with_the_same_token_makes_no_request(self) -> None:
        recorder = Recorder()
        client = build(recorder)

        first = await client.resolve("spotify", user_token=TOKEN_A)
        second = await client.resolve("spotify", user_token=TOKEN_A)

        assert recorder.count == 1
        assert second.values == first.values
        await client.aclose()

    async def test_a_different_token_never_reads_the_first_tokens_cache(self) -> None:
        recorder = Recorder()
        client = build(recorder)
        await client.resolve("spotify", user_token=TOKEN_A)

        await client.resolve("spotify", user_token=TOKEN_B)

        # THE test in this file. Keying by an unverified `sub` would make this one
        # request, and anything able to hand this service a forged token claiming
        # somebody else's sub would be served their settings with no request to
        # settings-api ever being made -- the server's authorisation bypassed by the
        # cache in front of it.
        assert recorder.count == 2
        assert recorder.requests[1].headers["X-Settings-User-Token"] == TOKEN_B
        await client.aclose()

    async def test_a_different_namespace_is_a_different_entry(self) -> None:
        recorder = Recorder()
        client = build(recorder)
        await client.resolve("spotify", user_token=TOKEN_A)
        await client.resolve("common", user_token=TOKEN_A)
        assert recorder.count == 2
        await client.aclose()


class TestRevalidation:
    async def test_an_expired_entry_revalidates_with_if_none_match(self) -> None:
        recorder = Recorder()
        client = build(recorder, ttl_seconds=0)

        await client.resolve("spotify", user_token=TOKEN_A)
        again = await client.resolve("spotify", user_token=TOKEN_A)

        assert recorder.count == 2
        assert recorder.requests[1].headers["If-None-Match"] == recorder.etag
        # The 304 carried no body, so the values can only have come from the entry we
        # already held -- which is the whole point of revalidating rather than refetching.
        assert again.values["default_market"] == "PT"
        await client.aclose()

    async def test_a_304_refreshes_the_entry_rather_than_expiring_it_again(self) -> None:
        recorder = Recorder()
        client = build(recorder, ttl_seconds=1_000)
        await client.resolve("spotify", user_token=TOKEN_A)
        # Force one revalidation by emptying the TTL, then put it back.
        client._ttl = 0
        await client.resolve("spotify", user_token=TOKEN_A)
        client._ttl = 1_000
        await client.resolve("spotify", user_token=TOKEN_A)
        assert recorder.count == 2
        await client.aclose()

    async def test_a_200_replaces_what_was_held(self) -> None:
        recorder = Recorder()
        client = build(recorder, ttl_seconds=0)
        await client.resolve("spotify", user_token=TOKEN_A)

        recorder.body = {
            **BODY,
            "revision": 5,
            "settings": {**BODY["settings"], "default_market": "GB"},
        }
        recorder.etag = '"account-a.5"'
        refreshed = await client.resolve("spotify", user_token=TOKEN_A)

        assert refreshed.values["default_market"] == "GB"
        assert refreshed.revision == 5
        await client.aclose()


class TestSingleFlight:
    async def test_ten_concurrent_callers_make_one_request(self) -> None:
        recorder = Recorder()
        client = build(recorder)

        results = await asyncio.gather(
            *(client.resolve("spotify", user_token=TOKEN_A) for _ in range(10))
        )

        # A cold cache under load must not become a thundering herd pointed at a service
        # that is, by construction, on the critical path of every other service.
        assert recorder.count == 1
        assert all(result.values["default_market"] == "PT" for result in results)
        await client.aclose()

    async def test_concurrent_callers_for_different_people_are_not_collapsed(self) -> None:
        recorder = Recorder()
        client = build(recorder)
        await asyncio.gather(
            client.resolve("spotify", user_token=TOKEN_A),
            client.resolve("spotify", user_token=TOKEN_B),
        )
        # Single-flight must not become "one person's answer served to another".
        assert recorder.count == 2
        await client.aclose()


class TestOutages:
    async def test_a_cached_document_is_served_stale(self) -> None:
        recorder = Recorder()
        client = build(recorder, ttl_seconds=0)
        await client.resolve("spotify", user_token=TOKEN_A)

        recorder.error = httpx.ConnectError("settings-api is down")
        degraded = await client.resolve("spotify", user_token=TOKEN_A)

        # The person's own most recent values, which are strictly better than any default.
        assert degraded.stale is True
        assert degraded.values["default_market"] == "PT"
        await client.aclose()

    async def test_a_stale_read_does_not_settle_in_for_a_whole_ttl(self) -> None:
        recorder = Recorder()
        client = build(recorder, ttl_seconds=1_000)
        await client.resolve("spotify", user_token=TOKEN_A)
        client._ttl = 0
        recorder.error = httpx.ConnectError("down")
        await client.resolve("spotify", user_token=TOKEN_A)

        recorder.error = None
        recovered = await client.resolve("spotify", user_token=TOKEN_A)

        # The failed attempt left the entry's clock alone, so the very next call tries
        # again rather than serving stale values until the TTL happens to expire.
        assert recovered.stale is False
        await client.aclose()

    async def test_a_cold_client_falls_back_per_setting_once_it_knows_the_rules(self) -> None:
        recorder = Recorder(body=REFUSING_BODY, etag='"account-a.2"')
        client = build(recorder, ttl_seconds=0)
        # One successful call for a different person teaches the client this namespace's
        # fallbacks, which are facts about the deployment rather than about anybody.
        await client.resolve("search", user_token=TOKEN_A)

        recorder.error = httpx.ConnectError("down")
        degraded = await client.resolve("search", user_token=TOKEN_B)

        assert degraded.stale is True
        assert degraded.values["max_content_chars"] == 40_000
        assert "disabled_providers" in degraded.refused
        await client.aclose()

    async def test_reading_a_refused_key_raises_and_reading_another_does_not(self) -> None:
        recorder = Recorder(body=REFUSING_BODY, etag='"account-a.2"')
        client = build(recorder, ttl_seconds=0)
        await client.resolve("search", user_token=TOKEN_A)
        recorder.error = httpx.ConnectError("down")
        degraded = await client.resolve("search", user_token=TOKEN_B)

        # Raised on ACCESS rather than at resolve time, so a search that never needed the
        # provider list still works. An operation that does need it must not proceed on a
        # guess: the default is "every provider allowed", which is exactly the value the
        # person refused.
        assert degraded["max_content_chars"] == 40_000
        with pytest.raises(SettingsRefused) as refusal:
            degraded["disabled_providers"]
        assert refusal.value.key == "disabled_providers"
        await client.aclose()

    async def test_get_does_not_swallow_a_refusal(self) -> None:
        recorder = Recorder(body=REFUSING_BODY, etag='"account-a.2"')
        client = build(recorder, ttl_seconds=0)
        await client.resolve("search", user_token=TOKEN_A)
        recorder.error = httpx.ConnectError("down")
        degraded = await client.resolve("search", user_token=TOKEN_B)

        # `get(key, default)` exists for a key the CATALOGUE does not have, not as a way
        # to proceed on a guess -- which would be exactly the decision the refusal exists
        # to prevent.
        with pytest.raises(SettingsRefused):
            degraded.get("disabled_providers", [])
        await client.aclose()

    async def test_a_client_that_knows_nothing_says_so(self) -> None:
        recorder = Recorder()
        recorder.error = httpx.ConnectError("down")
        client = build(recorder)

        with pytest.raises(SettingsUnavailable, match="never had a successful response"):
            await client.resolve("spotify", user_token=TOKEN_A)
        await client.aclose()

    async def test_a_500_degrades_and_a_403_does_not(self) -> None:
        recorder = Recorder()
        client = build(recorder, ttl_seconds=0)
        await client.resolve("spotify", user_token=TOKEN_A)

        recorder.handler = lambda _request: httpx.Response(503, json={"detail": "keyring is down"})
        degraded = await client.resolve("spotify", user_token=TOKEN_A)
        assert degraded.stale is True

        # A 403 is a fact about this caller -- a grant that does not cover the namespace.
        # Degrading here would hide a misconfigured deployment behind defaults that
        # happened to work, which is the kind of thing found months later.
        recorder.handler = lambda _request: httpx.Response(
            403, json={"detail": "spotify-api is not granted the user namespace"}
        )
        with pytest.raises(SettingsRejected) as rejection:
            await client.resolve("user", user_token=TOKEN_A)
        assert rejection.value.status_code == 403
        assert "not granted" in rejection.value.detail
        await client.aclose()

    async def test_a_refusal_with_no_problem_body_still_says_something(self) -> None:
        recorder = Recorder()
        recorder.handler = lambda _request: httpx.Response(404, text="<html>nope</html>")
        client = build(recorder)
        with pytest.raises(SettingsRejected, match="nope"):
            await client.resolve("spotify", user_token=TOKEN_A)
        await client.aclose()

    async def test_a_refusal_whose_detail_is_not_a_string_falls_back_to_the_text(self) -> None:
        recorder = Recorder()
        recorder.handler = lambda _request: httpx.Response(404, json={"detail": 42})
        client = build(recorder)
        with pytest.raises(SettingsRejected, match="42"):
            await client.resolve("spotify", user_token=TOKEN_A)
        await client.aclose()


class TestWrites:
    async def test_a_write_sends_both_credentials_and_returns_the_revision(self) -> None:
        recorder = Recorder()
        recorder.handler = lambda _request: httpx.Response(
            200, json={"revision": 9, "changed": ["spotify.default_market"], "unchanged": False}
        )
        client = build(recorder)

        revision = await client.set("spotify", "default_market", "GB", user_token=TOKEN_A)

        assert revision == 9
        assert recorder.requests[0].headers["X-Settings-User-Token"] == TOKEN_A
        await client.aclose()

    async def test_a_write_drops_the_cached_entry_for_that_token(self) -> None:
        recorder = Recorder()
        client = build(recorder)
        await client.resolve("spotify", user_token=TOKEN_A)

        recorder.handler = lambda _request: httpx.Response(
            200, json={"revision": 5, "changed": ["spotify.default_market"], "unchanged": False}
        )
        await client.set("spotify", "default_market", "GB", user_token=TOKEN_A)
        recorder.handler = None
        await client.resolve("spotify", user_token=TOKEN_A)

        # Dropped rather than patched: the write response says what the revision became,
        # not what every other setting in the namespace resolved to.
        assert recorder.count == 3
        await client.aclose()

    async def test_a_refused_write_says_what_was_wrong(self) -> None:
        recorder = Recorder()
        recorder.handler = lambda _request: httpx.Response(
            409, json={"detail": "user.log_values is pinned by this deployment's policy"}
        )
        client = build(recorder)
        with pytest.raises(SettingsRejected, match="pinned"):
            await client.set("user", "log_values", value=True, user_token=TOKEN_A)
        await client.aclose()

    async def test_a_write_during_an_outage_is_never_silently_dropped(self) -> None:
        recorder = Recorder()
        recorder.error = httpx.ConnectError("down")
        client = build(recorder)
        # Never queued and never swallowed: the person is standing there having just asked
        # for it, and a write that reported success and did nothing is the worst outcome.
        with pytest.raises(SettingsUnavailable, match="could not be reached to write"):
            await client.set("spotify", "default_market", "GB", user_token=TOKEN_A)
        await client.aclose()


class TestTheCacheIsBounded:
    async def test_the_least_recently_used_entry_is_evicted(self) -> None:
        recorder = Recorder()
        client = build(recorder, max_cached_tokens=2, ttl_seconds=1_000)

        await client.resolve("spotify", user_token="token-1")
        await client.resolve("spotify", user_token="token-2")
        await client.resolve("spotify", user_token="token-1")  # token-1 is now newest
        await client.resolve("spotify", user_token="token-3")  # evicts token-2

        before = recorder.count
        await client.resolve("spotify", user_token="token-1")
        assert recorder.count == before, "token-1 was the most recently used and must survive"
        await client.resolve("spotify", user_token="token-2")
        assert recorder.count == before + 1, "token-2 was evicted and must be refetched"
        await client.aclose()

    async def test_the_bound_exists_because_tokens_keep_arriving(self) -> None:
        recorder = Recorder()
        client = build(recorder, max_cached_tokens=4)
        for index in range(50):
            await client.resolve("spotify", user_token=f"token-{index}")
        # A service sees a new token every fifteen minutes per person. Unbounded, this
        # dictionary is a slow leak that only shows up in production.
        assert len(client._entries) <= 4
        await client.aclose()


class TestTheShape:
    def test_the_real_client_satisfies_the_protocol(self) -> None:
        client = HttpSettingsClient(base_url="http://settings.test", service_token=SERVICE_TOKEN)
        assert isinstance(client, SettingsClient)

    def test_reading_a_key_this_namespace_does_not_have_is_a_key_error(self) -> None:
        resolved = ResolvedSettings(namespace="spotify", values={"a": 1}, fallbacks={})
        with pytest.raises(KeyError):
            resolved["nope"]
        with pytest.raises(KeyError):
            resolved.get("nope")

    def test_get_returns_a_supplied_default_for_an_absent_key(self) -> None:
        resolved = ResolvedSettings(namespace="spotify", values={"a": 1}, fallbacks={})
        assert resolved.get("nope", "fallback") == "fallback"

    def test_contains_covers_refused_keys_too(self) -> None:
        resolved = ResolvedSettings(
            namespace="search",
            values={"a": 1},
            fallbacks={"b": Fallback(default=[], on_unavailable=OnUnavailable.REFUSE)},
            refused=frozenset({"b"}),
        )
        # A refused key EXISTS -- it just cannot be read. A service checking membership
        # before reading must not conclude the setting is absent.
        assert "b" in resolved
        assert "a" in resolved
        assert "c" not in resolved
