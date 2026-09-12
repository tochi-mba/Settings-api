"""Every request gets an id, every record carries it, and a crash still returns one.

The last point is the reason the unhandled-exception path is handled here rather than by
Starlette's outermost middleware: that runs after the request-id binding has unwound and
would return a response with no id on it -- the one thing the caller is told to quote.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from settings_api.api.errors import register_exception_handlers
from settings_api.api.middleware import (
    MAX_SUPPLIED_REQUEST_ID,
    REQUEST_ID_HEADER,
    RESPONSE_TIME_HEADER,
    RequestContextMiddleware,
)
from settings_api.core.config import LogFormat
from settings_api.core.context import get_request_id
from settings_api.core.logging import configure_logging


def throwaway() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    @app.get("/ok")
    async def ok() -> dict[str, str | None]:
        return {"seen": get_request_id()}

    @app.get("/boom")
    async def boom() -> None:
        msg = "the-handler-exploded-with-this-text"
        raise RuntimeError(msg)

    return app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    configure_logging(level="INFO", log_format=LogFormat.JSON)
    async with AsyncClient(transport=ASGITransport(app=throwaway()), base_url="http://t") as http:
        yield http


def records(out: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in out.strip().splitlines() if line.startswith("{")]


class TestTheRequestId:
    async def test_one_is_generated_and_visible_to_the_handler(self, client: AsyncClient) -> None:
        response = await client.get("/ok")
        generated = response.headers[REQUEST_ID_HEADER]
        assert len(generated) == 32
        assert response.json()["seen"] == generated

    async def test_a_supplied_one_is_honoured_so_a_trace_can_span_services(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/ok", headers={REQUEST_ID_HEADER: "trace-abc"})
        assert response.headers[REQUEST_ID_HEADER] == "trace-abc"
        assert response.json()["seen"] == "trace-abc"

    async def test_a_supplied_one_is_truncated(self, client: AsyncClient) -> None:
        # It ends up in every log record for this request.
        response = await client.get("/ok", headers={REQUEST_ID_HEADER: "x" * 500})
        assert response.headers[REQUEST_ID_HEADER] == "x" * MAX_SUPPLIED_REQUEST_ID

    async def test_an_empty_supplied_id_is_replaced(self, client: AsyncClient) -> None:
        response = await client.get("/ok", headers={REQUEST_ID_HEADER: ""})
        assert len(response.headers[REQUEST_ID_HEADER]) == 32


class TestTiming:
    async def test_the_response_time_header_is_a_number_of_milliseconds(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/ok")
        assert float(response.headers[RESPONSE_TIME_HEADER]) >= 0


class TestLogging:
    async def test_a_success_is_logged_with_method_path_and_status(
        self, client: AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        response = await client.get("/ok")
        completed = [
            r for r in records(capsys.readouterr().out) if r.get("event") == "request_completed"
        ]
        assert len(completed) == 1
        record = completed[0]
        assert (record["method"], record["path"], record["status_code"]) == ("GET", "/ok", 200)
        assert record["request_id"] == response.headers[REQUEST_ID_HEADER]
        assert isinstance(record["duration_ms"], float)

    async def test_a_crash_still_returns_a_problem_with_the_request_id(
        self, client: AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        response = await client.get("/boom", headers={REQUEST_ID_HEADER: "crash-1"})
        assert response.status_code == 500
        assert response.headers[REQUEST_ID_HEADER] == "crash-1"
        assert response.json()["request_id"] == "crash-1"
        assert "the-handler-exploded-with-this-text" not in response.text

        events = [r["event"] for r in records(capsys.readouterr().out)]
        assert "request_failed" in events
        assert "unhandled_exception" in events
        assert "request_completed" in events
