"""The client every service in this family uses to read one person's settings.

Four lines at the call site::

    from settings_client import HttpSettingsClient

    settings = HttpSettingsClient(
        base_url=config.settings_api_base_url, service_token=config.settings_api_token
    )
    resolved = await settings.resolve("spotify", user_token=caller.token)
    market = resolved["default_market"]

Everything else -- caching, ``If-None-Match`` revalidation, single-flight, and what to do
when settings-api is down -- is handled inside, so that the consuming services do not
each get the same four things slightly wrong.
"""

from __future__ import annotations

from settings_client.client import (
    DEFAULT_MAX_CACHED_TOKENS,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TTL_SECONDS,
    USER_TOKEN_HEADER,
    HttpSettingsClient,
    SettingsClient,
)
from settings_client.errors import (
    SettingsClientError,
    SettingsRefused,
    SettingsRejected,
    SettingsUnavailable,
)
from settings_client.models import Fallback, OnUnavailable, ResolvedSettings, Value

__all__ = [
    "DEFAULT_MAX_CACHED_TOKENS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TTL_SECONDS",
    "USER_TOKEN_HEADER",
    "Fallback",
    "HttpSettingsClient",
    "OnUnavailable",
    "ResolvedSettings",
    "SettingsClient",
    "SettingsClientError",
    "SettingsRefused",
    "SettingsRejected",
    "SettingsUnavailable",
    "Value",
]

__version__ = "0.2.0"
