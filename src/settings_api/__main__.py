"""The server entry point.

Reads the configuration once, so a misspelled ``SETTINGS_API_`` variable is a refusal to
start rather than a default nobody noticed, then serves the app uvicorn builds from the
factory.
"""

from __future__ import annotations

import uvicorn

from settings_api.core.config import load_settings


def main() -> None:
    """Serve the API on the configured host and port."""
    settings = load_settings()
    uvicorn.run(
        "settings_api.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
