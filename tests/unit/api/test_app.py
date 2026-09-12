"""The application factory, and the one seam the suite depends on.

``create_app`` deliberately builds its own container, so tests could not inject a clock --
except that ``start`` honours a container already parked on the app. That seam is what
lets the suite test a ninety-day retention window without waiting for one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from settings_api.api.app import API_DESCRIPTION, create_app, start, stop
from settings_api.core.container import Container
from tests.conftest import build_settings
from tests.fakes.clock import FakeClock


class TestCreateApp:
    def test_it_is_titled_and_described_for_a_model(self, tmp_path: Path) -> None:
        app = create_app(build_settings(tmp_path, app_name="settings-test"))
        assert app.title == "settings-test"
        assert app.description == API_DESCRIPTION
        # The description is wrapped at 90 columns in the source; compare on words.
        flat = " ".join(API_DESCRIPTION.split())
        assert "Do not change a setting on your own initiative" in flat
        assert "no such thing as a per-profile setting" in flat

    def test_every_router_is_mounted(self, tmp_path: Path) -> None:
        # Starlette wraps an included router rather than flattening its routes into
        # app.routes, so the generated contract is the honest place to look.
        app = create_app(build_settings(tmp_path))
        paths = set(app.openapi()["paths"])
        assert {"/healthy", "/ready", "/v1/settings", "/v1/internal/settings/{namespace}"} <= paths
        assert len(paths) == 11

    def test_the_openapi_document_generates_with_the_three_tags(self, tmp_path: Path) -> None:
        schema = create_app(build_settings(tmp_path)).openapi()
        assert [tag["name"] for tag in schema["tags"]] == ["health", "settings", "internal"]

    def test_settings_are_loaded_from_the_environment_when_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in list(__import__("os").environ):
            if name.upper().startswith("SETTINGS_API_"):
                monkeypatch.delenv(name)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SETTINGS_API_DATABASE_PATH", str(tmp_path / "env.db"))
        monkeypatch.setenv("SETTINGS_API_APP_NAME", "from-env")
        app = create_app()
        assert app.title == "from-env"
        assert app.state.settings.database_path == tmp_path / "env.db"


class TestStartAndStop:
    async def test_start_honours_a_prebuilt_container(self, tmp_path: Path) -> None:
        settings = build_settings(tmp_path)
        app = create_app(settings)
        clock = FakeClock()
        prebuilt = Container.build(settings, clock=clock)
        app.state.prebuilt = prebuilt

        container = start(app)
        try:
            assert container is prebuilt
            assert app.state.container is prebuilt
            assert container._sweeper_task is not None
        finally:
            await stop(container)
        assert container._sweeper_task is None

    async def test_start_builds_its_own_when_there_is_none(self, tmp_path: Path) -> None:
        settings = build_settings(tmp_path)
        app = create_app(settings)
        container = start(app)
        try:
            assert isinstance(container, Container)
            assert container.settings is settings
        finally:
            await stop(container)

    async def test_the_lifespan_runs_start_and_stop(self, tmp_path: Path) -> None:
        app: FastAPI = create_app(build_settings(tmp_path))
        async with LifespanManager(app):
            container = app.state.container
            assert container._sweeper_task is not None
            await asyncio.sleep(0)
        assert container._sweeper_task is None
