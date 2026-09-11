"""Wire models shared across endpoints.

Separate from the domain types on purpose: the HTTP contract is public -- its operation
ids become MCP tool names -- and must be able to evolve without dragging the domain with
it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

PROBLEM_CONTENT_TYPE = "application/problem+json"

SettingValue = bool | int | str | list[str] | None
"""Every shape a setting value may take on the wire.

The order matters and is the reason coercion refusals work. pydantic's smart union tries
an exact type match before it tries to convert, so ``"true"`` stays a string and ``5``
stays an integer -- which is what makes ``store_query_history: "false"`` a 422 rather than
a setting somebody believes is off and is not. A float and an object match nothing here
and are refused outright, which is the whole of "a setting is not arbitrary JSON".
"""


class FieldError(BaseModel):
    """One field-level validation failure."""

    location: str = Field(description="Dotted path to the offending field.")
    message: str = Field(description="What is wrong with it.")


class Problem(BaseModel):
    """An error, in the shape RFC 9457 defines.

    Every failure this service produces uses it, so a client -- or a model calling this as
    a tool -- has exactly one error shape to understand.

    ``detail`` names the rule that failed and **never echoes the offending value**. That
    is not politeness: a 422 body is logged by the caller, shown in a transcript, and
    often handed back to a model, and the value that failed validation is a decision that
    person made about their own data.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "type": "https://settings-api.invalid/problems/validation-failed",
                    "title": "Validation failed",
                    "status": 422,
                    "detail": "spotify.default_market is not in the form this setting requires",
                    "request_id": "5c1f9f0f7f2f4e6c8a1b2c3d4e5f6a7b",
                }
            ]
        }
    )

    type: str = Field(description="A URI identifying the problem kind.")
    title: str = Field(description="Short, human-readable summary of the problem kind.")
    status: int = Field(description="The HTTP status code.")
    detail: str = Field(description="Explanation specific to this occurrence.")
    request_id: str | None = Field(
        default=None,
        description="Correlates this response with the server logs for the same request.",
    )
    errors: list[FieldError] | None = Field(
        default=None,
        description="Per-field detail, present only for validation failures.",
    )
