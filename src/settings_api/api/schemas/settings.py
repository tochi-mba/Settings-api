"""Wire models for the settings surface.

Every request body sets ``extra="forbid"``, and on this service that is load-bearing
rather than tidy. The one field a caller might plausibly try to send and must never be
able to is ``profile``: one settings set per account is the property this whole service is
built around (ADR-0002), and a body that carried ``{"profile": "work"}`` and had it
silently ignored would be the single most dangerous kind of wrong -- the caller would
believe it had written a per-profile setting, and every service would read the other one.
Forbidden extras make it a 422 instead. There is a test named after exactly that.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from settings_api.api.schemas.common import SettingValue
from settings_api.domain.resolution import Source
from settings_api.domain.types import OnUnavailable, Origin, SettingType

NamespaceValues = dict[str, SettingValue]
"""One namespace's settings, by key."""

SettingsDocument = dict[str, NamespaceValues]
"""Settings by namespace and key. Note what is absent: any place to put a profile."""


class SettingsResponse(BaseModel):
    """Every namespace this token grants, resolved."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "revision": 4,
                    "settings": {
                        "common": {"timezone": "Europe/Lisbon", "units": "metric"},
                        "spotify": {"default_market": "PT"},
                    },
                }
            ]
        }
    )

    revision: int = Field(
        description=(
            "Bumped by every change that changes something. Also the ETag on this "
            "response, and what `If-Match` takes."
        )
    )
    settings: SettingsDocument = Field(description="The effective values, by namespace and key.")


class Bounds(BaseModel):
    """What a setting may be, as this deployment has it.

    These are the **narrowed** bounds where a policy narrowed them, not the catalogue's
    own. Reporting the catalogue's would offer a range and then refuse most of it.
    """

    minimum: int | None = Field(default=None, description="Lowest allowed whole number.")
    maximum: int | None = Field(default=None, description="Highest allowed whole number.")
    choices: list[str] | None = Field(default=None, description="Every allowed value.")
    max_chars: int | None = Field(default=None, description="Longest allowed string.")
    pattern: str | None = Field(
        default=None, description="Anchored regular expression the value must match."
    )
    max_items: int | None = Field(default=None, description="Most items a list may hold.")
    max_item_chars: int | None = Field(default=None, description="Longest allowed item in a list.")
    nullable: bool = Field(description="Whether null is a legal value.")


class SettingDescription(BaseModel):
    """One catalogue entry, with this account's current value beside it."""

    namespace: str
    key: str
    type: SettingType = Field(description="Which of the five shapes this setting holds.")
    summary: str = Field(description="One line, for choosing whether to touch this.")
    description: str = Field(description="What choosing each value actually means.")
    default: SettingValue = Field(
        description="What this deployment falls back to when nobody has chosen."
    )
    value: SettingValue = Field(description="The effective value for this account.")
    set: bool = Field(
        description=(
            "Whether this account has expressed a preference -- not whether the value "
            "differs from the default. Somebody who deliberately chose the value that "
            "happens to be the default has expressed a preference, and it survives a "
            "change of default."
        )
    )
    source: Source = Field(description="Where the effective value came from.")
    pinned: bool = Field(
        description=(
            "Whether this deployment's policy fixes the value. A write to a pinned "
            "setting is a 409, never a silent no-op."
        )
    )
    owner_writable_only: bool = Field(
        description="Whether only a token minted for settings itself may change this."
    )
    on_unavailable: OnUnavailable = Field(
        description=(
            "What a consuming service must do when this service is unreachable: fall "
            "back to the default, or refuse the operation."
        )
    )
    origin: Origin = Field(
        description=(
            "`existing` means the owning service already has this knob and wiring it up "
            "is a change at the call site. `proposed` means the owning service needs a "
            "change before this value does anything -- see docs/catalogue.md."
        )
    )
    deprecated_by: str | None = Field(
        default=None, description="The setting that replaces this one, if any."
    )
    bounds: Bounds = Field(description="What this deployment will accept.")


class SchemaResponse(BaseModel):
    """The whole catalogue, as this deployment has it."""

    revision: int = Field(description="This account's revision, as on every other read.")
    count: int = Field(description="How many settings are described.")
    settings: list[SettingDescription] = Field(
        description="Every setting in every namespace this token grants."
    )


class SettingResponse(BaseModel):
    """One setting, resolved."""

    namespace: str
    key: str
    value: SettingValue
    set: bool = Field(description="Whether this account has expressed a preference.")
    source: Source = Field(description="Where the effective value came from.")
    pinned: bool = Field(description="Whether policy fixes it.")


class SetSettingRequest(BaseModel):
    """One value to store."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"value": "Europe/Lisbon"}]},
    )

    value: SettingValue = Field(
        description=(
            'The value to store. Types are exact: `true` is a boolean and `"true"` is '
            "a string, and a setting declared as a boolean refuses the string."
        )
    )


class UpdateSettingsRequest(BaseModel):
    """A whole document of settings to apply, all of it or none of it."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "settings": {
                        "common": {"timezone": "Europe/Lisbon"},
                        "search": {"safe_search": "strict"},
                    }
                }
            ]
        },
    )

    settings: SettingsDocument = Field(
        description=(
            "Settings by namespace and key. This **merges**: settings not named here are "
            "left exactly as they are. Use `reset_setting` or `reset_namespace` to clear "
            "one. Every key is checked before anything is written, so a document with one "
            "bad key writes none of it."
        )
    )


class ImportSettingsRequest(BaseModel):
    """A document produced by `export_settings`, to apply to this account."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(description="The export format version. Must be one this build knows.")
    settings: SettingsDocument = Field(
        description=(
            "The settings to apply, by namespace and key. Validated exactly as a normal "
            "write: an import cannot store a value a write could not. Unknown keys are "
            "refused by name rather than dropped."
        )
    )


class WriteResponse(BaseModel):
    """What a write did."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"revision": 5, "changed": ["common.timezone"], "unchanged": False}]
        }
    )

    revision: int = Field(
        description=(
            "The account's revision afterwards. Unchanged when the write changed nothing, "
            "which is what makes an identical repeated write genuinely idempotent."
        )
    )
    changed: list[str] = Field(
        description="The settings that actually changed, as `namespace.key`."
    )
    unchanged: bool = Field(description="True when the request was accepted and changed nothing.")


class ExportResponse(BaseModel):
    """A portable copy of everything this person has chosen."""

    version: int = Field(description="The export format version.")
    exported_at: str = Field(description="When this was produced, in UTC.")
    revision: int = Field(description="The revision this was taken at.")
    settings: SettingsDocument = Field(
        description=(
            "Only what was actually chosen. Defaults are deliberately absent: an export "
            "that filled them in would freeze this deployment's defaults into wherever it "
            "was imported, so restoring a backup after a default changed would silently "
            "pin the old value with no way to tell which ones were meant."
        )
    )


class ForgetResponse(BaseModel):
    """What an erasure destroyed."""

    removed: int = Field(description="How many stored settings were destroyed.")
    detail: str = Field(description="What was done, in a sentence.")


class SettingFallback(BaseModel):
    """What a consuming service must do about one setting when this service is down.

    Returned alongside the values so a client is **self-sufficient after one successful
    fetch**. The alternative was for every consuming service to vendor a copy of the
    catalogue's defaults, which is a copy that drifts -- and it drifts silently, because
    the only time it is read is during an outage, when nobody is looking.
    """

    default: SettingValue = Field(description="What to fall back to, when falling back is allowed.")
    on_unavailable: OnUnavailable = Field(
        description=(
            "`use_default` means the default is the most conservative value and landing "
            "on it cannot weaken anything the person asked for. `refuse` means it is not: "
            "the default is permissive because the service needs it to be, so falling "
            "back would override an explicit restriction and the operation must fail "
            "instead."
        )
    )


class ResolvedSettingsResponse(BaseModel):
    """What a consuming service gets: one namespace, with `common` merged underneath."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "namespace": "spotify",
                    "revision": 4,
                    "settings": {
                        "timezone": "Europe/Lisbon",
                        "default_profile": "personal",
                        "default_market": "PT",
                    },
                    "fallbacks": {
                        "timezone": {"default": "UTC", "on_unavailable": "use_default"},
                        "default_profile": {
                            "default": "personal",
                            "on_unavailable": "refuse",
                        },
                        "default_market": {"default": None, "on_unavailable": "use_default"},
                    },
                }
            ]
        }
    )

    namespace: str = Field(description="The namespace that was asked for.")
    revision: int = Field(description="The account's revision. Cache against the ETag, not this.")
    settings: NamespaceValues = Field(
        description=(
            "The effective values: this namespace merged over `common`, with this "
            "namespace winning on a collision."
        )
    )
    fallbacks: dict[str, SettingFallback] = Field(
        description=(
            "Per key, what to do when this service cannot be reached. Cache it with the "
            "values; it changes only when the deployment's catalogue or policy changes, "
            "and having it is what lets a client behave correctly during an outage "
            "without shipping its own copy of the catalogue."
        )
    )
