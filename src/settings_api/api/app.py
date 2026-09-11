"""The application factory.

A factory rather than a module-level app: tests build an app per case with their own
settings, and nothing is constructed as a side effect of importing this module.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from settings_api.api.errors import register_exception_handlers
from settings_api.api.middleware import RequestContextMiddleware
from settings_api.api.routers import ROUTERS
from settings_api.core.config import Settings, load_settings
from settings_api.core.container import Container
from settings_api.core.logging import configure_logging, get_logger
from settings_api.core.version import service_version
from settings_api.domain.registry import BY_QUALIFIED

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger(__name__)

API_DESCRIPTION = """
One place a person decides how every service in this family behaves **for them**: which
country their music searches resolve against, how long their downloads sit on the server,
which model answers their questions, what "delete" means for their notes, and which
providers must never see their queries.

Read a namespace to find out what somebody has chosen. Write to it when they ask you to.

**Four things to know before you call anything.**

*These are decisions, not preferences you may tidy up.* Do not change a setting on your
own initiative. Ask the person, then set what they asked for. A setting an assistant
adjusted helpfully is worse than a setting nobody ever set.

*Call `describe_settings` first.* It lists every setting, its type, this deployment's
bounds, the current value, and whether operator policy has pinned it. Guessing at a key
name gets you a 400 that names the ones that exist; guessing at a value gets you a 422.

*Changes are never retroactive.* Switching `user.erasure_mode` to `immediate` does not
destroy what is already waiting out a grace period, and switching away from `tombstone`
does not schedule what is already tombstoned.

*It is not where secrets go.* A value that looks like an API key, a token or a private key
is refused with a 422 naming keyring, which is the service built for exactly that. There
is no override.

**No endpoint takes an account id, and there is no such thing as a per-profile setting.**
Whose settings you are reading comes from your token. One settings set per account,
however many keyring profiles that account has -- so "which profile do you mean when I
don't say" is itself one account-level answer.
""".strip()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: configuration to use. Loaded from the environment when omitted, which is
            what the server entry point does; tests pass their own.
    """
    settings = settings or load_settings()
    configure_logging(level=settings.log_level, log_format=settings.log_format)

    app = FastAPI(
        title=settings.app_name,
        description=API_DESCRIPTION,
        version=service_version(),
        lifespan=_lifespan,
        # Route summaries and operation ids are the contract an MCP bridge generates tool
        # names and descriptions from, so they are written for a model to read.
        openapi_tags=[
            {"name": "health", "description": "Liveness and dependency checks."},
            {
                "name": "settings",
                "description": (
                    "Read and change what this person has decided about how the services "
                    "in this family behave for them."
                ),
            },
            {
                "name": "internal",
                "description": (
                    "For consuming services, holding both their own token and the end "
                    "user's. Never expose these as assistant tools."
                ),
            },
        ],
    )
    app.state.settings = settings

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    for router in ROUTERS:
        app.include_router(router)

    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the container on startup and shut it down cleanly on the way out."""
    container = start(app)
    try:
        yield
    finally:
        await stop(container)


def start(app: FastAPI) -> Container:
    """Wire the application's dependencies and begin the retired-key sweep.

    Deliberately does not reach keyring. Nothing is fetched until the first token arrives,
    so a keyring that is down does not stop this service from starting -- these services
    are restarted together, and a startup dependency would turn one outage into two.
    """
    # A container already on the app is one a test built with its own clock and its own
    # keyring transport. Honoured rather than replaced, because create_app building its
    # own is what keeps production wiring in one place -- and a suite that could not
    # substitute the clock could not test a ninety-day retention window without waiting.
    container = getattr(app.state, "prebuilt", None) or Container.build(app.state.settings)
    app.state.container = container
    container.start_sweeper()

    logger.info(
        "service_started",
        environment=container.settings.environment,
        database=str(container.database.path),
        settings_count=len(BY_QUALIFIED),
        services=list(container.services.configured),
    )
    return container


async def stop(container: Container) -> None:
    """Release everything the application holds open.

    Logged before closing rather than after, so a shutdown that hangs still leaves a
    record of having been asked to stop.
    """
    logger.info("service_stopping")
    await container.aclose()
