"""Every failure is RFC 9457, and the offending input never reaches the caller.

Driven against a throwaway app with one route per domain error, so the mapping is tested
directly rather than through whichever real route happens to raise each one. The
important test sends a sentinel in a body that fails validation and asserts the sentinel
appears nowhere in the response: FastAPI's own handler would have echoed it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict

from settings_api.api.errors import (
    _DOMAIN_STATUS,
    KEYRING_RETRY_AFTER,
    PROBLEM_BASE_URI,
    _slug_for,
    problem_response,
    register_exception_handlers,
    unhandled_problem_response,
)
from settings_api.api.middleware import RequestContextMiddleware
from settings_api.api.schemas.common import PROBLEM_CONTENT_TYPE
from settings_api.core.context import bind_request_id
from settings_api.domain.errors import DomainError, KeyringUnreachableError

SENTINEL = "Pacific-Chatham-sentinel-9Q"


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int


def throwaway() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    def make(kind: type[Exception], name: str) -> None:
        async def raiser() -> None:
            raise kind("the detail for " + kind.__name__)

        app.add_api_route(f"/domain/{kind.__name__}", raiser, methods=["GET"], name=name)

    for index, error_type in enumerate(_DOMAIN_STATUS):
        make(error_type, f"r{index}")

    @app.get("/keyring-down")
    async def keyring_down() -> None:
        msg = "keyring's signing keys could not be fetched"
        raise KeyringUnreachableError(msg)

    @app.get("/http")
    async def http() -> None:
        raise HTTPException(status_code=418, detail="a teapot")

    @app.post("/validated")
    async def validated(body: Body) -> dict[str, int]:
        return {"count": body.count}

    @app.get("/unhandled")
    async def unhandled() -> None:
        msg = f"secret path /var/{SENTINEL}"
        raise RuntimeError(msg)

    return app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=throwaway()), base_url="http://t") as http:
        yield http


class TestTheDomainMapping:
    @pytest.mark.parametrize(
        ("error_type", "status"),
        list(_DOMAIN_STATUS.items()),
        ids=lambda v: getattr(v, "__name__", v),
    )
    async def test_each_domain_error_maps_to_its_status(
        self, client: AsyncClient, error_type: type[Exception], status: int
    ) -> None:
        response = await client.get(f"/domain/{error_type.__name__}")
        assert response.status_code == status
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
        body = response.json()
        assert set(body) == {"type", "title", "status", "detail", "request_id"}
        assert body["status"] == status
        assert body["detail"] == f"the detail for {error_type.__name__}"
        assert body["type"].startswith(PROBLEM_BASE_URI)
        assert body["request_id"] == response.headers["X-Request-ID"]

    def test_every_domain_error_is_mapped(self) -> None:
        from settings_api.domain import errors

        defined = {
            obj
            for obj in vars(errors).values()
            if isinstance(obj, type) and issubclass(obj, DomainError) and obj is not DomainError
        }
        # KeyringUnreachableError has its own handler with a Retry-After.
        assert defined - set(_DOMAIN_STATUS) == {KeyringUnreachableError}


class TestKeyringDown:
    async def test_it_is_503_with_a_retry_after(self, client: AsyncClient) -> None:
        response = await client.get("/keyring-down")
        assert response.status_code == 503
        # Come back, not start over: a 401 would send a person through a login that
        # would not have fixed anything.
        assert response.headers["Retry-After"] == KEYRING_RETRY_AFTER
        assert response.json()["type"].endswith("/keyring-unreachable")


class TestValidation:
    async def test_the_offending_input_never_appears_in_the_response(
        self, client: AsyncClient
    ) -> None:
        response = await client.post("/validated", json={"count": SENTINEL})
        assert response.status_code == 422
        assert SENTINEL not in response.text
        body = response.json()
        assert body["detail"] == "the request failed validation"
        assert body["errors"] == [
            {"location": "body.count", "message": body["errors"][0]["message"]}
        ]
        assert "input" not in body["errors"][0]

    async def test_an_extra_field_is_named_by_location(self, client: AsyncClient) -> None:
        response = await client.post("/validated", json={"count": 1, "profile": SENTINEL})
        assert response.status_code == 422
        assert response.json()["errors"][0]["location"] == "body.profile"
        assert SENTINEL not in response.text


class TestOtherFailures:
    async def test_a_starlette_http_exception_is_reshaped(self, client: AsyncClient) -> None:
        response = await client.get("/http")
        assert response.status_code == 418
        assert response.json()["detail"] == "a teapot"
        assert response.json()["title"] == "Error"

    async def test_an_unhandled_exception_withholds_its_message(self, client: AsyncClient) -> None:
        response = await client.get("/unhandled")
        assert response.status_code == 500
        assert SENTINEL not in response.text
        assert "quote the request id" in response.json()["detail"]
        assert response.json()["request_id"] == response.headers["X-Request-ID"]

    async def test_a_route_that_does_not_exist_is_a_problem_too(self, client: AsyncClient) -> None:
        response = await client.get("/nowhere")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)


class TestBuildingProblems:
    def test_the_slug_and_title_for_an_unknown_status(self) -> None:
        assert _slug_for(418) == "error"
        with bind_request_id("r"):
            response = problem_response(status_code=418, detail="d")
        assert response.status_code == 418
        assert b'"title":"Error"' in response.body

    def test_an_explicit_type_and_title_are_honoured(self) -> None:
        response = problem_response(
            status_code=409, detail="d", problem_type="pinned", title="Pinned"
        )
        assert b'"type":"https://settings-api.invalid/problems/pinned"' in response.body
        assert b'"title":"Pinned"' in response.body

    def test_the_request_id_is_absent_outside_a_request(self) -> None:
        response = problem_response(status_code=400, detail="d")
        assert b"request_id" not in response.body

    def test_unhandled_logs_only_the_type_name(self, capsys: pytest.CaptureFixture[str]) -> None:
        from settings_api.core.config import LogFormat
        from settings_api.core.logging import configure_logging

        configure_logging(level="INFO", log_format=LogFormat.JSON)

        def explode() -> None:
            msg = f"value was {SENTINEL}"
            raise ValueError(msg)

        try:
            explode()
        except ValueError as exc:
            response = unhandled_problem_response(exc)
        out = capsys.readouterr().out
        assert response.status_code == 500
        assert '"error_type": "ValueError"' in out
        # The exception's own text carries the value; the log record must not.
        first_line = out.strip().splitlines()[0]
        assert SENTINEL not in first_line.split('"exception"')[0]
