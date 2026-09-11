"""Liveness and readiness.

Two endpoints, and the difference between them is the difference between "restart this
process" and "do not send it traffic yet".

``/healthy`` says only that this process is running, and it **must never fail**. user-api
shipped a liveness endpoint that returned 500 when an unanticipated JWKS failure escaped,
which is the worst possible bug in this particular endpoint: an orchestrator reads a 500
as "this process is broken" and restarts a process that was working perfectly, during an
outage of a *different* service, repeatedly.

``/ready`` is where dependencies are reported, one line each, so an operator reading it at
three in the morning is told which of four things is wrong rather than being told "not
ready".
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from settings_api.api.dependencies import ContainerDep
from settings_api.api.schemas.health import (
    DependencyStatus,
    LivenessResponse,
    ReadinessResponse,
)
from settings_api.core.version import service_version
from settings_api.domain.registry import BY_QUALIFIED

router = APIRouter(tags=["health"])


@router.get(
    "/healthy",
    operation_id="check_liveness",
    summary="Check that the service process is running",
    description=(
        "Liveness only. It reports nothing about keyring, the database or anything else, "
        "because a liveness check that failed when a dependency did would have an "
        "orchestrator restart a healthy process during somebody else's outage. Needs no "
        "token. Use `check_readiness` to find out whether requests will actually work."
    ),
    response_model=LivenessResponse,
)
async def check_liveness(container: ContainerDep) -> LivenessResponse:
    """Report that this process is alive, whatever else is not."""
    return LivenessResponse(
        status="alive",
        version=service_version(),
        uptime_seconds=container.uptime_seconds,
    )


@router.get(
    "/ready",
    operation_id="check_readiness",
    summary="Check that every dependency this service needs is usable",
    description=(
        "Four checks, reported individually: the database answers, keyring's signing keys "
        "can be fetched, the catalogue loaded, and the operator policy parsed. Answers "
        "503 when any of them fails, so a load balancer can hold traffic back. Needs no "
        "token."
    ),
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def check_readiness(container: ContainerDep, response: Response) -> ReadinessResponse:
    """Report on each dependency, and answer 503 if any of them is unusable."""
    checks = [
        await _database_check(container),
        await _keyring_check(container),
        _catalogue_check(),
        _policy_check(container),
    ]
    ready = all(check.ready for check in checks)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        ready=ready,
        version=service_version(),
        settings_count=len(BY_QUALIFIED),
        checks=checks,
    )


async def _database_check(container: ContainerDep) -> DependencyStatus:
    """Whether the database answers a trivial read.

    The catch is wide and is confined to this function, for the reason in
    :meth:`settings_api.auth.jwks.JwksClient.healthy`: here the unexpected exception *is*
    the thing being reported, and a readiness check that raised would answer 500 while
    trying to say what is wrong.
    """
    try:
        await container.database.fetch_one("SELECT 1 AS ok")
    except Exception as exc:
        return DependencyStatus(
            name="database", ready=False, detail=f"the database is not usable: {type(exc).__name__}"
        )
    return DependencyStatus(name="database", ready=True)


async def _keyring_check(container: ContainerDep) -> DependencyStatus:
    """Whether a token could be verified right now."""
    healthy, detail = await container.jwks.healthy()
    return DependencyStatus(name="keyring", ready=healthy, detail=detail)


def _catalogue_check() -> DependencyStatus:
    """Whether the catalogue loaded.

    It cannot have failed in a running process -- a malformed catalogue raises at import
    and the process does not start -- and it is reported anyway. "Ready" meaning four
    specific things is more useful than "ready" meaning one unnamed thing, and an operator
    who can see the count knows immediately whether the build they are looking at is the
    one they deployed.
    """
    return DependencyStatus(name="catalogue", ready=bool(BY_QUALIFIED))


def _policy_check(container: ContainerDep) -> DependencyStatus:
    """Whether the operator policy parsed, and how much of it there is."""
    pinned = len(container.policy.pins)
    narrowed = len(container.policy.overrides)
    return DependencyStatus(
        name="policy",
        ready=True,
        detail=None if not (pinned or narrowed) else f"{narrowed} narrowed, {pinned} pinned",
    )
