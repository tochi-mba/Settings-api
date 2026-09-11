"""Health and readiness responses."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LivenessResponse(BaseModel):
    """Whether this process is running. Nothing more.

    Deliberately says nothing about keyring or the database. A liveness check that
    reported on a dependency is a liveness check that fails an entire deployment when a
    dependency has a bad minute, and the orchestrator's response to that is to restart a
    process that was working perfectly.
    """

    status: str = Field(description="Always `alive` when this responds at all.")
    version: str = Field(description="The running version of the service.")
    uptime_seconds: float = Field(description="How long this process has been up.")


class DependencyStatus(BaseModel):
    """One dependency, and why it is unhappy if it is."""

    name: str = Field(description="What was checked.")
    ready: bool = Field(description="Whether it is usable right now.")
    detail: str | None = Field(
        default=None, description="Why not, when it is not. Absent when it is."
    )


class ReadinessResponse(BaseModel):
    """Whether this process can serve requests right now.

    Four things are checked, and each can fail on its own: the database is reachable,
    keyring's keys can be fetched, the catalogue loaded, and the policy file parsed. The
    last two cannot fail in a running process -- a bad catalogue or a bad policy stops it
    starting -- and are reported anyway, because "ready" meaning four specific things is
    more useful to an operator at three in the morning than "ready" meaning one unnamed
    thing.
    """

    ready: bool = Field(description="Whether every dependency is usable.")
    version: str = Field(description="The running version of the service.")
    settings_count: int = Field(description="How many settings this build's catalogue has.")
    checks: list[DependencyStatus] = Field(description="One entry per dependency.")
