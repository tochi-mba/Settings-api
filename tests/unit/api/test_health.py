"""Liveness never fails; readiness names what is wrong.

user-api once returned 500 from its liveness endpoint when a JWKS failure escaped. An
orchestrator reads that as "this process is broken" and restarts a process that was
working perfectly, during an outage of a different service, repeatedly. The first test
here is the one that would catch it.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi import FastAPI
from httpx import AsyncClient

from settings_api.domain.registry import BY_QUALIFIED
from tests.conftest import build_app, build_settings, container_of
from tests.fakes.clock import FakeClock
from tests.fakes.keyring import FakeKeyring


class TestLiveness:
    async def test_it_needs_no_token(self, client: AsyncClient) -> None:
        response = await client.get("/healthy")
        assert response.status_code == 200
        assert response.json()["status"] == "alive"
        assert response.json()["version"]
        assert response.json()["uptime_seconds"] >= 0

    async def test_it_is_still_200_when_keyring_is_completely_broken(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        keyring.error = RuntimeError("keyring is on fire")
        response = await client.get("/healthy")
        assert response.status_code == 200

    async def test_uptime_follows_the_injected_clock(
        self, client: AsyncClient, clock: FakeClock
    ) -> None:
        clock.advance(30)
        assert (await client.get("/healthy")).json()["uptime_seconds"] == 30


class TestReadiness:
    async def test_everything_working_is_200_with_four_named_checks(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert body["settings_count"] == len(BY_QUALIFIED) == 46
        assert [check["name"] for check in body["checks"]] == [
            "database",
            "keyring",
            "catalogue",
            "policy",
        ]
        assert all(check["ready"] for check in body["checks"])
        assert all(check["detail"] is None for check in body["checks"])

    async def test_keyring_down_is_503_naming_the_failing_check(
        self, client: AsyncClient, keyring: FakeKeyring
    ) -> None:
        keyring.error = httpx.ConnectError("down")
        response = await client.get("/ready")
        assert response.status_code == 503
        checks = {check["name"]: check for check in response.json()["checks"]}
        assert checks["keyring"]["ready"] is False
        assert "could not be fetched" in checks["keyring"]["detail"]
        # The others are still reported true: an operator is told which one.
        assert checks["database"]["ready"] is True
        assert checks["catalogue"]["ready"] is True

    async def test_a_closed_database_is_reported_not_raised(
        self, client: AsyncClient, app: FastAPI
    ) -> None:
        await container_of(app).database.aclose()
        response = await client.get("/ready")
        assert response.status_code == 503
        checks = {check["name"]: check for check in response.json()["checks"]}
        assert checks["database"]["ready"] is False
        assert "not usable" in checks["database"]["detail"]

    async def test_a_configured_policy_is_summarised(
        self, tmp_path: Path, keyring: FakeKeyring
    ) -> None:
        path = tmp_path / "policy.json"
        path.write_text(
            json.dumps({"user": {"grace_days": {"maximum": 60}, "log_values": {"pin": False}}})
        )
        settings = build_settings(tmp_path, policy_path=path)
        app = build_app(settings, FakeClock(), keyring)
        from asgi_lifespan import LifespanManager

        async with (
            LifespanManager(app),
            AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
        ):
            response = await http.get("/ready")
        checks = {check["name"]: check for check in response.json()["checks"]}
        assert checks["policy"] == {
            "name": "policy",
            "ready": True,
            "detail": "1 narrowed, 1 pinned",
        }
