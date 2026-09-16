"""Builders shared by the configuration tests.

``clean_env`` lives here rather than in a directory ``conftest.py`` so the sibling clock
and container tests do not inherit an autouse wipe of ``SETTINGS_API_*``.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from settings_api.core.config import ENV_PREFIX, Settings

TOKEN_A = "spotify-service-token-0123456789abcdef"
TOKEN_B = "media-service-token-0123456789abcdefghij"
"""Two tokens long enough to be accepted, and unlike each other. Their length is asserted
in :func:`test_the_sample_tokens_are_long_enough_to_be_accepted` rather than assumed, so a
change to the minimum shows up as that failure instead of as every service test failing."""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``SETTINGS_API_*`` variable that the test did not set itself.

    Settings read the process environment, so a variable left in the shell that started
    pytest would silently change what these tests are asserting about -- and the tests
    about *unknown* variables would then depend on what the operator happened to export.
    """
    for name in list(os.environ):
        if name.upper().startswith(ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)


def make_settings(**overrides: Any) -> Settings:
    """Settings through the constructor, with the ``.env`` file out of the way.

    ``_env_file=None`` because the model config names ``.env``: without it these tests
    would pass or fail depending on a file in whatever directory pytest was started from.
    """
    values: dict[str, Any] = {"_env_file": None, **overrides}
    return Settings(**values)


def service(
    *,
    token: str = TOKEN_A,
    audience_prefix: str = "spotify",
    namespaces: tuple[str, ...] = ("spotify",),
) -> dict[str, Any]:
    """One entry of the ``services`` document, as JSON would deliver it."""
    return {"token": token, "audience_prefix": audience_prefix, "namespaces": list(namespaces)}
