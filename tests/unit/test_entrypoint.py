"""The server entry point reads configuration first, so a typo is a refusal to start."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import uvicorn

from settings_api import __main__ as entrypoint
from settings_api.core.config import UnknownSettingError


class Recorder:
    """A stand-in for uvicorn.run that remembers what it was asked to serve."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, app: str, **options: Any) -> None:
        self.calls.append((app, options))


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Recorder:
    recorded = Recorder()
    # __main__ does `import uvicorn` and calls `uvicorn.run`, so patching the attribute on
    # the uvicorn module itself is what it will see.
    monkeypatch.setattr(uvicorn, "run", recorded.run)
    monkeypatch.chdir(tmp_path)
    for name in list(__import__("os").environ):
        if name.upper().startswith("SETTINGS_API_"):
            monkeypatch.delenv(name)
    return recorded


def test_main_serves_the_factory_on_the_configured_host_and_port(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETTINGS_API_HOST", "0.0.0.0")  # noqa: S104 -- a test of configuration
    monkeypatch.setenv("SETTINGS_API_PORT", "9999")

    entrypoint.main()

    ((app, options),) = recorder.calls
    assert app == "settings_api.api.app:create_app"
    assert options["factory"] is True
    assert (options["host"], options["port"]) == ("0.0.0.0", 9999)  # noqa: S104
    assert options["log_config"] is None


def test_a_misspelled_variable_stops_the_server_before_it_starts(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETTINGS_API_PROT", "9999")
    with pytest.raises(UnknownSettingError, match="SETTINGS_API_PROT"):
        entrypoint.main()
    assert recorder.calls == []
