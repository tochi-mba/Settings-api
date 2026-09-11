"""The person-facing surface: twelve operations, all under ``/v1/settings``.

Every ``operation_id`` here is public API. They become MCP tool names, so renaming one
breaks every client with a tool bound to it, and a contract test pins the exact set.

**Route order matters.** Starlette matches in declaration order, and ``/v1/settings/schema``
would otherwise be matched by ``/v1/settings/{namespace}`` and answered with "no namespace
called schema". The three literal paths are declared first, and there is a test that reads
each of them rather than a comment asking the next person to remember.

**No route accepts an account id or a profile.** Which person's settings these are comes
from the verified token; every request body sets ``extra="forbid"`` so a stray
``"profile"`` is a 422 rather than a field that is silently ignored. See ADR-0002.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Response, status

from settings_api.api.dependencies import (
    ContainerDep,
    IdentityDep,
    IfMatchDep,
    revision_from_if_match,
)
from settings_api.api.schemas.common import Problem
from settings_api.api.schemas.events import EventResponse, EventsResponse
from settings_api.api.schemas.settings import (
    Bounds,
    ExportResponse,
    ForgetResponse,
    ImportSettingsRequest,
    SchemaResponse,
    SetSettingRequest,
    SettingDescription,
    SettingResponse,
    SettingsResponse,
    UpdateSettingsRequest,
    WriteResponse,
)
from settings_api.domain.errors import InvalidSettingValueError
from settings_api.domain.resolution import Resolved
from settings_api.settings.service import EXPORT_VERSION, Document, Written

router = APIRouter(prefix="/v1/settings", tags=["settings"])

_PROBLEM: dict[str, Any] = {"model": Problem}

NamespacePath = Annotated[
    str,
    Path(description="Which compartment, e.g. `common`, `search`, `user`.", max_length=64),
]
KeyPath = Annotated[str, Path(description="Which setting within that namespace.", max_length=64)]

ETAG_HEADER = "ETag"

DO_NOT_DECIDE = (
    "Do not change these on your own initiative. They are the person's decision about "
    "their own data; ask, then set what they asked for."
)
"""Written once and attached to every write description, because a model reads these and
acts on them. A settings service an assistant tidies up unprompted is worse than no
settings service."""

NOT_RETROACTIVE = (
    "Changes are never retroactive. Switching `user.erasure_mode` to `immediate` does not "
    "destroy what is already waiting out a grace period, and switching away from "
    "`tombstone` does not schedule what is already tombstoned."
)


# -- Reads: the literal paths first, because Starlette matches in order ------------------


@router.get(
    "/schema",
    operation_id="describe_settings",
    summary="List every setting, what it may be, and what it is set to now",
    description=(
        "Call this before writing anything. It returns every setting in every namespace "
        "your token grants: the type, this deployment's default and bounds, what the "
        "value currently resolves to, whether this person has actually chosen it, and "
        "whether operator policy has pinned it. The bounds are this deployment's, "
        "narrowed where policy narrowed them, so a value inside them is a value a write "
        "will accept.\n\n"
        "`origin: proposed` means the owning service needs a change before that setting "
        "does anything yet; `on_unavailable` says what that service does when this one is "
        "unreachable."
    ),
    response_model=SchemaResponse,
    responses={status.HTTP_401_UNAUTHORIZED: _PROBLEM},
)
async def describe_settings(
    container: ContainerDep, identity: IdentityDep, response: Response
) -> SchemaResponse:
    """Describe the catalogue as this deployment has it, with current values."""
    document = await container.service.describe(identity)
    response.headers[ETAG_HEADER] = document.etag
    described = [
        _describe(item) for entries in document.namespaces.values() for item in entries.values()
    ]
    return SchemaResponse(revision=document.revision, count=len(described), settings=described)


@router.get(
    "/export",
    operation_id="export_settings",
    summary="Take a portable copy of every choice this person has made",
    description=(
        "Returns only what was actually chosen, never the defaults, so importing it "
        "elsewhere restores the decisions rather than freezing this deployment's "
        "defaults into another one. Feed the result straight to `import_settings`. "
        "Being able to leave with your own settings is a privacy property, not a feature."
    ),
    response_model=ExportResponse,
    responses={status.HTTP_401_UNAUTHORIZED: _PROBLEM},
)
async def export_settings(container: ContainerDep, identity: IdentityDep) -> ExportResponse:
    """Produce a portable document of everything this person has chosen."""
    exported = await container.service.export(identity)
    return ExportResponse(
        version=exported.version,
        exported_at=exported.exported_at,
        revision=exported.revision,
        settings=exported.settings,
    )


@router.get(
    "/events",
    operation_id="read_settings_events",
    summary="Read this person's history of their own settings changes",
    description=(
        "Newest first. Page with `?before=` and the `sequence` of the oldest event you "
        "have -- never with a timestamp, because two changes in the same instant share "
        "one and a timestamp cursor would drop one of them.\n\n"
        "Each event says which settings changed and who changed them. It never says what "
        "they were changed to: the log is a record of decisions, not a second copy of "
        "them."
    ),
    response_model=EventsResponse,
    responses={status.HTTP_401_UNAUTHORIZED: _PROBLEM},
)
async def read_settings_events(
    container: ContainerDep,
    identity: IdentityDep,
    limit: Annotated[int, Query(ge=1, le=200, description="How many events to return.")] = 50,
    before: Annotated[
        int | None,
        Query(description="Return events older than this `sequence`. Omit for the newest page."),
    ] = None,
) -> EventsResponse:
    """One page of the change log."""
    events = await container.service.read_events(identity, limit=limit, before=before)
    rendered = [
        EventResponse(
            sequence=event.sequence,
            at=event.at,
            action=event.action,
            revision=event.revision,
            actor=event.actor,
            namespace=event.namespace,
            key=event.key,
            service=event.service,
            changed=_changed_names(event.detail),
        )
        for event in events
    ]
    # Present only when this page was full, because a short page is the last page and a
    # cursor on it would send a caller round once more to learn nothing.
    next_before = rendered[-1].sequence if len(rendered) == limit else None
    return EventsResponse(events=rendered, next_before=next_before)


@router.get(
    "",
    operation_id="get_settings",
    summary="Read every setting this token grants, resolved",
    description=(
        "The whole picture: every namespace your token grants, with each setting's "
        "effective value. **This never returns 404.** An account that has expressed no "
        "preference has the default preference, so a person who has never touched "
        "anything gets a full document of defaults.\n\n"
        "The `ETag` on the response is what `If-Match` and `If-None-Match` take."
    ),
    response_model=SettingsResponse,
    responses={status.HTTP_401_UNAUTHORIZED: _PROBLEM},
)
async def get_settings(
    container: ContainerDep, identity: IdentityDep, response: Response
) -> SettingsResponse:
    """Read everything this token grants."""
    document = await container.service.get_all(identity)
    return _settings_response(document, response)


@router.get(
    "/{namespace}",
    operation_id="get_namespace",
    summary="Read one namespace, resolved",
    description=(
        "One service's compartment. 404 if this build has no such namespace; 403 if it "
        "has one and your token does not grant it -- two different answers, because a "
        "caller told the wrong one goes looking for the wrong problem."
    ),
    response_model=SettingsResponse,
    responses={
        status.HTTP_401_UNAUTHORIZED: _PROBLEM,
        status.HTTP_403_FORBIDDEN: _PROBLEM,
        status.HTTP_404_NOT_FOUND: _PROBLEM,
    },
)
async def get_namespace(
    namespace: NamespacePath,
    container: ContainerDep,
    identity: IdentityDep,
    response: Response,
) -> SettingsResponse:
    """Read one namespace."""
    document = await container.service.get_namespace(identity, namespace)
    return _settings_response(document, response)


@router.get(
    "/{namespace}/{key}",
    operation_id="get_setting",
    summary="Read one setting, and find out why it is what it is",
    description=(
        "Returns the value plus three things that explain it: `set` says whether this "
        "person has actually chosen it, `source` says whether the value came from the "
        "catalogue, from operator policy or from them, and `pinned` says whether policy "
        "has fixed it so a write would be refused."
    ),
    response_model=SettingResponse,
    responses={
        status.HTTP_401_UNAUTHORIZED: _PROBLEM,
        status.HTTP_403_FORBIDDEN: _PROBLEM,
        status.HTTP_404_NOT_FOUND: _PROBLEM,
    },
)
async def get_setting(
    namespace: NamespacePath,
    key: KeyPath,
    container: ContainerDep,
    identity: IdentityDep,
) -> SettingResponse:
    """Read one setting."""
    resolved = await container.service.get_setting(identity, namespace, key)
    return SettingResponse(
        namespace=resolved.namespace,
        key=resolved.key,
        value=resolved.value,
        set=resolved.set_by_account,
        source=resolved.source,
        pinned=resolved.pinned,
    )


# -- Writes -----------------------------------------------------------------------------


@router.put(
    "",
    operation_id="update_settings",
    summary="Apply a whole document of settings at once",
    description=(
        "Applies every setting in the document and **leaves the rest exactly as they "
        "are** -- this merges rather than replaces, so sending two keys does not discard "
        "everything else. Use `reset_setting` or `reset_namespace` to clear something.\n\n"
        "All-or-nothing: every key is checked before anything is written, so one bad "
        "value means none of the document is applied. Unknown keys are refused by name "
        "rather than dropped.\n\n"
        "Pass `If-Match` with the ETag from a read to be refused with 412 if anything "
        f"changed in between.\n\n{DO_NOT_DECIDE}\n\n{NOT_RETROACTIVE}"
    ),
    response_model=WriteResponse,
    responses={
        status.HTTP_400_BAD_REQUEST: _PROBLEM,
        status.HTTP_401_UNAUTHORIZED: _PROBLEM,
        status.HTTP_403_FORBIDDEN: _PROBLEM,
        status.HTTP_404_NOT_FOUND: _PROBLEM,
        status.HTTP_409_CONFLICT: _PROBLEM,
        status.HTTP_412_PRECONDITION_FAILED: _PROBLEM,
        status.HTTP_422_UNPROCESSABLE_CONTENT: _PROBLEM,
    },
)
async def update_settings(
    body: UpdateSettingsRequest,
    container: ContainerDep,
    identity: IdentityDep,
    response: Response,
    if_match: IfMatchDep = None,
) -> WriteResponse:
    """Apply a document of settings as one transaction."""
    written = await container.service.update(
        identity, body.settings, if_revision=revision_from_if_match(if_match, identity)
    )
    return _write_response(written, response)


@router.put(
    "/{namespace}/{key}",
    operation_id="set_setting",
    summary="Set one setting",
    description=(
        'Types are exact: a setting declared as a boolean refuses the string `"true"`, '
        'and one declared as a whole number refuses `"5"`. A value that looks like an '
        "API key, a token or a private key is refused outright with a 422 naming keyring "
        "-- credentials belong there, and there is no override.\n\n"
        "Setting a value that is already stored changes nothing, bumps no revision and "
        f"records no event.\n\n{DO_NOT_DECIDE}\n\n{NOT_RETROACTIVE}"
    ),
    response_model=WriteResponse,
    responses={
        status.HTTP_401_UNAUTHORIZED: _PROBLEM,
        status.HTTP_403_FORBIDDEN: _PROBLEM,
        status.HTTP_404_NOT_FOUND: _PROBLEM,
        status.HTTP_409_CONFLICT: {
            "model": Problem,
            "description": (
                "This deployment's policy pins this setting. The detail says so; the "
                "value was not changed."
            ),
        },
        status.HTTP_412_PRECONDITION_FAILED: _PROBLEM,
        status.HTTP_422_UNPROCESSABLE_CONTENT: _PROBLEM,
    },
)
async def set_setting(
    namespace: NamespacePath,
    key: KeyPath,
    body: SetSettingRequest,
    container: ContainerDep,
    identity: IdentityDep,
    response: Response,
    if_match: IfMatchDep = None,
) -> WriteResponse:
    """Set one setting."""
    written = await container.service.set_setting(
        identity,
        namespace,
        key,
        body.value,
        if_revision=revision_from_if_match(if_match, identity),
    )
    return _write_response(written, response)


@router.post(
    "/import",
    operation_id="import_settings",
    summary="Apply a document produced by export_settings",
    description=(
        "Validated exactly as a normal write, so an import cannot store a value a write "
        "could not, and applied as one transaction. Unknown keys are refused by name and "
        "point at `describe_settings` -- never silently dropped, because a caller that "
        f"believes it restored a setting it did not is worse off than one refused.\n\n"
        f"{DO_NOT_DECIDE}"
    ),
    response_model=WriteResponse,
    responses={
        status.HTTP_400_BAD_REQUEST: _PROBLEM,
        status.HTTP_401_UNAUTHORIZED: _PROBLEM,
        status.HTTP_403_FORBIDDEN: _PROBLEM,
        status.HTTP_404_NOT_FOUND: _PROBLEM,
        status.HTTP_409_CONFLICT: _PROBLEM,
        status.HTTP_412_PRECONDITION_FAILED: _PROBLEM,
        status.HTTP_422_UNPROCESSABLE_CONTENT: _PROBLEM,
    },
)
async def import_settings(
    body: ImportSettingsRequest,
    container: ContainerDep,
    identity: IdentityDep,
    response: Response,
    if_match: IfMatchDep = None,
) -> WriteResponse:
    """Apply an exported document to this account."""
    _check_export_version(body.version)
    written = await container.service.import_document(
        identity, body.settings, if_revision=revision_from_if_match(if_match, identity)
    )
    return _write_response(written, response)


@router.delete(
    "/{namespace}/{key}",
    operation_id="reset_setting",
    summary="Put one setting back to its default",
    description=(
        "Removes this person's stored choice, so the setting resolves to whatever the "
        "deployment's default is -- now and after any future change to that default.\n\n"
        "**Not an erasure.** The stored row goes; the event saying it was reset stays, "
        "because that event is the entire record that the preference ever existed. Use "
        "`forget_settings` to destroy everything."
    ),
    response_model=WriteResponse,
    responses={
        status.HTTP_401_UNAUTHORIZED: _PROBLEM,
        status.HTTP_403_FORBIDDEN: _PROBLEM,
        status.HTTP_404_NOT_FOUND: _PROBLEM,
        status.HTTP_409_CONFLICT: _PROBLEM,
        status.HTTP_412_PRECONDITION_FAILED: _PROBLEM,
    },
)
async def reset_setting(
    namespace: NamespacePath,
    key: KeyPath,
    container: ContainerDep,
    identity: IdentityDep,
    response: Response,
    if_match: IfMatchDep = None,
) -> WriteResponse:
    """Reset one setting to its resolved default."""
    written = await container.service.reset_setting(
        identity, namespace, key, if_revision=revision_from_if_match(if_match, identity)
    )
    return _write_response(written, response)


@router.delete(
    "/{namespace}",
    operation_id="reset_namespace",
    summary="Put every setting in one namespace back to its default",
    description=(
        "Clears this person's stored choices for one service's compartment. Settings that "
        "policy has pinned, and ones only the person may write, are skipped rather than "
        "refusing the whole request -- a caller clearing a namespace is not asking about "
        "any particular key in it."
    ),
    response_model=WriteResponse,
    responses={
        status.HTTP_401_UNAUTHORIZED: _PROBLEM,
        status.HTTP_403_FORBIDDEN: _PROBLEM,
        status.HTTP_404_NOT_FOUND: _PROBLEM,
        status.HTTP_412_PRECONDITION_FAILED: _PROBLEM,
    },
)
async def reset_namespace(
    namespace: NamespacePath,
    container: ContainerDep,
    identity: IdentityDep,
    response: Response,
    if_match: IfMatchDep = None,
) -> WriteResponse:
    """Reset a whole namespace to its defaults."""
    written = await container.service.reset_namespace(
        identity, namespace, if_revision=revision_from_if_match(if_match, identity)
    )
    return _write_response(written, response)


@router.delete(
    "",
    operation_id="forget_settings",
    summary="Destroy every setting this service holds for this person",
    description=(
        "Erasure, not a reset. Every stored value, the account row and the whole change "
        "log are deleted, and the write-ahead log is truncated so the bytes are gone from "
        "the disk rather than merely unlinked from the table.\n\n"
        "There is no undo and no grace period. Afterwards this person reads as somebody "
        "who has never set anything, which is exactly what they are."
    ),
    response_model=ForgetResponse,
    responses={status.HTTP_401_UNAUTHORIZED: _PROBLEM},
)
async def forget_settings(container: ContainerDep, identity: IdentityDep) -> ForgetResponse:
    """Destroy everything stored for this account."""
    removed = await container.erasure.forget(identity)
    return ForgetResponse(
        removed=removed,
        detail=(
            "every stored setting, the account record and the change log were destroyed, "
            "and the write-ahead log was truncated"
        ),
    )


# -- Rendering --------------------------------------------------------------------------


def _settings_response(document: Document, response: Response) -> SettingsResponse:
    """Render a resolved document, with its ETag on the response."""
    response.headers[ETAG_HEADER] = document.etag
    return SettingsResponse(revision=document.revision, settings=document.values())


def _write_response(written: Written, response: Response) -> WriteResponse:
    """Render what a write did, with the new ETag on the response."""
    response.headers[ETAG_HEADER] = written.etag
    return WriteResponse(
        revision=written.revision,
        changed=list(written.changed),
        unchanged=not written.any_change,
    )


def _describe(item: Resolved) -> SettingDescription:
    """Render one resolved setting as a catalogue entry with its current value."""
    definition = item.definition
    return SettingDescription(
        namespace=definition.namespace,
        key=definition.key,
        type=definition.value_type,
        summary=definition.summary,
        description=definition.description,
        default=definition.default,
        value=item.value,
        set=item.set_by_account,
        source=item.source,
        pinned=item.pinned,
        owner_writable_only=definition.owner_writable_only,
        on_unavailable=definition.on_unavailable,
        origin=definition.origin,
        deprecated_by=definition.deprecated_by,
        bounds=Bounds(
            minimum=definition.minimum,
            maximum=definition.maximum,
            choices=list(definition.choices) or None,
            max_chars=definition.max_chars,
            pattern=definition.pattern,
            max_items=definition.max_items,
            max_item_chars=definition.max_item_chars,
            nullable=definition.nullable,
        ),
    )


def _changed_names(detail: dict[str, object] | None) -> list[str]:
    """The qualified names an event recorded. Never values -- there are none to find."""
    if detail is None:
        return []
    changed = detail.get("changed")
    if not isinstance(changed, list):
        return []
    return [str(name) for name in changed]


def _check_export_version(version: int) -> None:
    """Refuse a document from a format this build does not understand.

    Raises:
        InvalidSettingValueError: rendered as a 422. A document from a newer format may
            mean things this build would misread, and guessing is how an import silently
            sets the wrong values.
    """
    if version != EXPORT_VERSION:
        msg = f"this build reads export format {EXPORT_VERSION}; the document says {version}"
        raise InvalidSettingValueError(msg)
