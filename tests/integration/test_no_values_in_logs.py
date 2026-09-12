"""A setting value appears in no log record, on the success path and on every failure path.

The sentinel is a legal value the credential detector accepts, so a refusal here means the
logging rule failed and not the detector. Every captured line is parsed as JSON and
searched recursively, so an occurrence nested inside a structure cannot hide from a
substring check on the raw text.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from settings_api.core.config import LogFormat
from settings_api.core.logging import configure_logging
from settings_api.domain.secrets import looks_like_a_credential
from tests.conftest import auth, token

SENTINEL = "Pacific/Chatham"


def mentions(record: object, needle: str) -> bool:
    if isinstance(record, str):
        return needle in record
    if isinstance(record, dict):
        return any(mentions(k, needle) or mentions(v, needle) for k, v in record.items())
    if isinstance(record, list):
        return any(mentions(item, needle) for item in record)
    return False


def records(out: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.strip().splitlines() if line.startswith("{")]


def test_the_sentinel_is_a_value_the_service_accepts() -> None:
    assert looks_like_a_credential(SENTINEL) is None


@pytest.fixture
def json_logs(app: FastAPI) -> None:
    configure_logging(level="DEBUG", log_format=LogFormat.JSON)


async def test_the_value_is_in_no_record_on_success(
    client: AsyncClient, json_logs: None, capsys: pytest.CaptureFixture[str]
) -> None:
    owner = token()
    ok = await client.put(
        "/v1/settings/common/timezone", json={"value": SENTINEL}, headers=auth(owner)
    )
    assert ok.status_code == 200
    await client.get("/v1/settings", headers=auth(owner))
    await client.get("/v1/settings/common/timezone", headers=auth(owner))
    await client.get("/v1/settings/schema", headers=auth(owner))
    await client.get("/v1/settings/export", headers=auth(owner))

    logged = records(capsys.readouterr().out)
    assert logged, "no log records were captured; the test proves nothing"
    assert not [r for r in logged if mentions(r, SENTINEL)]


async def test_the_value_is_in_no_record_on_a_validation_failure(
    client: AsyncClient, json_logs: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # Sent where it does not fit: a whole-number setting. FastAPI's own handler would
    # echo the input into the response; ours must not, and neither may the log.
    response = await client.put(
        "/v1/settings/user/grace_days", json={"value": SENTINEL}, headers=auth(token())
    )
    assert response.status_code == 422
    assert SENTINEL not in response.text
    assert not [r for r in records(capsys.readouterr().out) if mentions(r, SENTINEL)]


async def test_the_value_is_in_no_record_on_a_wire_validation_failure(
    client: AsyncClient, json_logs: None, capsys: pytest.CaptureFixture[str]
) -> None:
    response = await client.put(
        "/v1/settings/common/timezone",
        json={"value": SENTINEL, "profile": SENTINEL},
        headers=auth(token()),
    )
    assert response.status_code == 422
    assert SENTINEL not in response.text
    assert not [r for r in records(capsys.readouterr().out) if mentions(r, SENTINEL)]


async def test_the_value_is_in_no_record_on_a_403_or_404(
    client: AsyncClient, json_logs: None, capsys: pytest.CaptureFixture[str]
) -> None:
    narrow = token(namespace="search")
    assert (
        await client.put("/v1/settings/common/nope", json={"value": SENTINEL}, headers=auth(narrow))
    ).status_code == 404
    assert (
        await client.put(
            "/v1/settings/spotify/default_market", json={"value": SENTINEL}, headers=auth(narrow)
        )
    ).status_code == 403
    assert not [r for r in records(capsys.readouterr().out) if mentions(r, SENTINEL)]


async def test_the_value_is_in_no_record_on_an_unhandled_failure(
    client: AsyncClient, app: FastAPI, json_logs: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # Break the store underneath the app so the write path raises something nobody
    # anticipated, with the value in flight. The 500 must not carry it and nor may the log.
    from tests.conftest import container_of

    container = container_of(app)
    await container.database.aclose()

    response = await client.put(
        "/v1/settings/common/timezone", json={"value": SENTINEL}, headers=auth(token())
    )
    assert response.status_code == 500
    assert SENTINEL not in response.text
    out = capsys.readouterr().out
    logged = records(out)
    assert [r for r in logged if r.get("event") == "unhandled_exception"]
    assert not [r for r in logged if mentions(r, SENTINEL)]
