"""Wire models for the change log."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from settings_api.events.log import Action


class EventResponse(BaseModel):
    """One recorded change."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "sequence": 12,
                    "at": "2026-03-04T09:15:00+00:00",
                    "action": "set",
                    "namespace": "spotify",
                    "key": "default_market",
                    "revision": 5,
                    "actor": "settings",
                    "changed": ["spotify.default_market"],
                }
            ]
        }
    )

    sequence: int = Field(
        description=(
            "Page with this, never with a timestamp: two changes in the same tick share one."
        )
    )
    at: datetime = Field(description="When it happened, in UTC.")
    action: Action = Field(description="What happened.")
    revision: int = Field(description="The account's revision as of this event.")
    actor: str = Field(
        description=(
            "The audience of the token that did it, or `service:<name>` when a service "
            "was acting for the person. Derived from the token by the server, never "
            "claimed by the writer."
        )
    )
    namespace: str | None = Field(
        default=None, description="Present when the change was within one namespace."
    )
    key: str | None = Field(default=None, description="Present when one setting changed.")
    service: str | None = Field(
        default=None, description="The calling service, when one was mediating."
    )
    changed: list[str] = Field(
        description=(
            "Which settings changed, as `namespace.key`. **Never the values.** The log is "
            "a record of what you decided, not a second copy of the decisions."
        )
    )


class EventsResponse(BaseModel):
    """One page of the change log, newest first."""

    events: list[EventResponse] = Field(description="The page.")
    next_before: int | None = Field(
        default=None,
        description=("Pass as `?before=` for the next page, or null when this is the last one."),
    )
