"""The fake matches the real client, and the value objects behave.

``FakeSettingsClient`` exists to be substituted for ``HttpSettingsClient`` in a consuming
service's tests, so it must satisfy the same Protocol and degrade the same way -- including
the outage case, which is the one most services forget to test and the one their users
notice.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport

from settings_client import SettingsClient, SettingsRefused, SettingsRejected, SettingsUnavailable
from settings_client.models import Fallback, OnUnavailable, ResolvedSettings
from settings_client.testing import FakeSettingsClient, asgi_client


class TestTheFake:
    def test_it_satisfies_the_protocol(self) -> None:
        assert isinstance(FakeSettingsClient(), SettingsClient)

    async def test_seeded_values_read_back_with_the_revision(self) -> None:
        fake = FakeSettingsClient(values={"spotify": {"default_market": "PT"}})
        fake.seed("spotify", {"max_batch_size": 10})
        resolved = await fake.resolve("spotify", user_token="t")
        assert resolved["default_market"] == "PT"
        assert resolved["max_batch_size"] == 10
        assert resolved.revision == 0
        assert resolved.stale is False
        assert fake.resolves == 1

    async def test_a_write_is_recorded_and_bumps_the_revision(self) -> None:
        fake = FakeSettingsClient()
        assert await fake.set("spotify", "default_market", "GB", user_token="t") == 1
        assert fake.writes == [("spotify", "default_market", "GB")]
        assert (await fake.resolve("spotify", user_token="t"))["default_market"] == "GB"

    async def test_unavailable_with_known_fallbacks_degrades_like_the_real_client(self) -> None:
        fake = FakeSettingsClient(
            fallbacks={
                "search": {
                    "disabled_providers": Fallback(default=[], on_unavailable=OnUnavailable.REFUSE),
                    "max_content_chars": Fallback(
                        default=40_000, on_unavailable=OnUnavailable.USE_DEFAULT
                    ),
                }
            }
        )
        fake.unavailable = True
        resolved = await fake.resolve("search", user_token="t")
        assert resolved.stale is True
        assert resolved.revision is None
        assert resolved["max_content_chars"] == 40_000
        with pytest.raises(SettingsRefused):
            resolved["disabled_providers"]

    async def test_unavailable_with_nothing_known_raises(self) -> None:
        fake = FakeSettingsClient()
        fake.unavailable = True
        with pytest.raises(SettingsUnavailable):
            await fake.resolve("search", user_token="t")
        with pytest.raises(SettingsUnavailable):
            await fake.set("search", "safe_search", "strict", user_token="t")

    async def test_seed_fallback_declares_one_key(self) -> None:
        fake = FakeSettingsClient()
        fake.seed_fallback(
            "search",
            "safe_search",
            Fallback(default="moderate", on_unavailable=OnUnavailable.USE_DEFAULT),
        )
        fake.unavailable = True
        assert (await fake.resolve("search", user_token="t"))["safe_search"] == "moderate"

    async def test_a_rejected_namespace_refuses_reads_and_writes(self) -> None:
        fake = FakeSettingsClient()
        fake.rejects["user"] = (403, "not granted")
        with pytest.raises(SettingsRejected, match="not granted") as rejection:
            await fake.resolve("user", user_token="t")
        assert rejection.value.status_code == 403
        with pytest.raises(SettingsRejected):
            await fake.set("user", "grace_days", 7, user_token="t")

    async def test_aclose_is_a_no_op(self) -> None:
        await FakeSettingsClient().aclose()


class TestAsgiClient:
    def test_it_is_the_real_client_over_an_in_process_transport(self) -> None:
        from fastapi import FastAPI

        client = asgi_client(FastAPI(), service_token="s" * 32, ttl_seconds=5)
        assert isinstance(client, SettingsClient)
        assert isinstance(client._http._transport, ASGITransport)
        assert client._ttl == 5


class TestResolvedSettings:
    def test_it_is_frozen(self) -> None:
        resolved = ResolvedSettings(namespace="a", values={}, fallbacks={})
        with pytest.raises(AttributeError):
            resolved.stale = True  # type: ignore[misc]

    def test_refused_carries_namespace_and_key(self) -> None:
        error = SettingsRefused("search", "disabled_providers")
        assert (error.namespace, error.key) == ("search", "disabled_providers")
        assert "must not be guessed at" in str(error)
