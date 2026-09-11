"""The endpoints other services call.

This is the boundary that matters most in the whole service, and it is worth being
explicit about why it looks the way it does.

A consuming service -- spotify-api, say -- needs one person's preferences. If it could
authenticate as itself and then name whoever it liked, then anything able to reach this
service could read anybody's settings: the confused deputy, moved from inside one process
into the gap between two. So a caller here must present **both** credentials:

* its own service token, proving it is a service this deployment is willing to talk to,
  and
* the end user's short-lived signed token, proving that person authorised this.

The account comes from the *user's* token. There is no parameter by which a service can
name an account, which means there is no request a compromised service can make to obtain
settings it was not given a user token for. And the user token's audience must belong to
the calling service's own family, so a token minted for one service cannot be replayed at
another.

What comes back is one namespace with ``common`` merged underneath it, and nothing else. A
service learns only what it needs to do its job, so the blast radius of one compromised
service token is one namespace: media-tool cannot read ``user.erasure_mode``.

## Why services may write at all

``set_setting_for_user`` exists to serve a concrete case rather than for symmetry.
user-api already has ``PUT /v1/user/settings``, its ``operation_id`` is public API, and
MCP clients have tools bound to it; breaking that is not on the table. So a service may
write, **only within its own namespaces** and **only while holding that person's own
token**. It is the person's decision travelling through a service, not the service's
decision. Settings marked ``owner_writable_only`` are refused even then -- see ADR-0004.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Response, status

from settings_api.api.dependencies import (
    ContainerDep,
    IfNoneMatchDep,
    ServiceIdentityDep,
)
from settings_api.api.schemas.common import Problem
from settings_api.api.schemas.settings import (
    ResolvedSettingsResponse,
    SetSettingRequest,
    SettingFallback,
    WriteResponse,
)
from settings_api.domain.types import Value
from settings_api.settings.service import Document

router = APIRouter(prefix="/v1/internal", tags=["internal"])

_PROBLEM: dict[str, Any] = {"model": Problem}

ETAG_HEADER = "ETag"
CACHE_CONTROL_HEADER = "Cache-Control"

NamespacePath = Annotated[
    str, Path(description="The namespace this service is asking for.", max_length=64)
]
KeyPath = Annotated[str, Path(description="Which setting in that namespace.", max_length=64)]


@router.get(
    "/settings/{namespace}",
    operation_id="resolve_settings",
    summary="Resolve one namespace for one user, for a calling service",
    description=(
        "Called by another service, never by a person. Requires both the calling "
        "service's own token and the end user's short-lived token; the account is taken "
        "from the user's token, so a service cannot ask for settings it was not given a "
        "token for.\n\n"
        "Returns the effective values for this namespace with `common` merged underneath "
        "it, this namespace winning on a collision. A namespace this service was not "
        "granted is 403.\n\n"
        "Cache against the `ETag` and revalidate with `If-None-Match`; a 304 costs "
        "nothing and is the intended steady state."
    ),
    response_model=ResolvedSettingsResponse,
    responses={
        status.HTTP_304_NOT_MODIFIED: {
            "description": "Nothing has changed since the ETag you sent. No body."
        },
        status.HTTP_401_UNAUTHORIZED: _PROBLEM,
        status.HTTP_403_FORBIDDEN: _PROBLEM,
        status.HTTP_404_NOT_FOUND: _PROBLEM,
        status.HTTP_503_SERVICE_UNAVAILABLE: _PROBLEM,
    },
)
async def resolve_settings(
    namespace: NamespacePath,
    container: ContainerDep,
    identity: ServiceIdentityDep,
    response: Response,
    if_none_match: IfNoneMatchDep = None,
) -> ResolvedSettingsResponse | Response:
    """Resolve one namespace for the person whose token the caller presented."""
    document = await container.service.resolve_for(identity, namespace)
    cache_control = f"private, max-age={container.settings.cache_ttl_seconds}"

    if _matches(if_none_match, document.etag):
        # 304 carries no body, and the headers a cache needs travel with it. Building the
        # document first is deliberate rather than wasteful: the permission checks are in
        # that call, and answering 304 before them would tell a service whose grant had
        # been revoked that its cached copy was still good.
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={ETAG_HEADER: document.etag, CACHE_CONTROL_HEADER: cache_control},
        )

    response.headers[ETAG_HEADER] = document.etag
    response.headers[CACHE_CONTROL_HEADER] = cache_control
    return ResolvedSettingsResponse(
        namespace=namespace,
        revision=document.revision,
        settings=_values(document, namespace),
        fallbacks=_fallbacks(document, namespace),
    )


@router.put(
    "/settings/{namespace}/{key}",
    operation_id="set_setting_for_user",
    summary="Set one setting for a user, on their behalf",
    description=(
        "The write-through path: a person changing a setting inside another service's own "
        "interface, with that service passing the change along. Requires both "
        "credentials, and the calling service may only write within the namespaces it was "
        "granted -- media-tool cannot write `user.*`, and cannot write anything at all for "
        "somebody whose token it does not hold.\n\n"
        "Settings marked `owner_writable_only` are refused with a 403 even here: those "
        "have to be changed by the person, with a token minted for settings itself."
    ),
    response_model=WriteResponse,
    responses={
        status.HTTP_401_UNAUTHORIZED: _PROBLEM,
        status.HTTP_403_FORBIDDEN: {
            "model": Problem,
            "description": (
                "The namespace is not in this service's grant, or the setting is one only "
                "the person may change."
            ),
        },
        status.HTTP_404_NOT_FOUND: _PROBLEM,
        status.HTTP_409_CONFLICT: _PROBLEM,
        status.HTTP_422_UNPROCESSABLE_CONTENT: _PROBLEM,
        status.HTTP_503_SERVICE_UNAVAILABLE: _PROBLEM,
    },
)
async def set_setting_for_user(
    namespace: NamespacePath,
    key: KeyPath,
    body: SetSettingRequest,
    container: ContainerDep,
    identity: ServiceIdentityDep,
    response: Response,
) -> WriteResponse:
    """Set one setting for the person whose token the caller presented."""
    written = await container.service.set_setting(identity, namespace, key, body.value)
    response.headers[ETAG_HEADER] = written.etag
    return WriteResponse(
        revision=written.revision,
        changed=list(written.changed),
        unchanged=not written.any_change,
    )


def _values(document: Document, namespace: str) -> dict[str, Value]:
    """The merged values for one namespace, as a plain mapping."""
    return {key: item.value for key, item in document.namespaces[namespace].items()}


def _fallbacks(document: Document, namespace: str) -> dict[str, SettingFallback]:
    """What to do about each key when this service cannot be reached.

    Built from the **narrowed** definition, so a deployment that changed a default in
    policy hands out that default rather than the catalogue's. A client falling back to a
    value this deployment does not use would be a client that behaves differently during
    an outage than it would have if the call had succeeded.
    """
    return {
        key: SettingFallback(
            default=item.definition.default,
            on_unavailable=item.definition.on_unavailable,
        )
        for key, item in document.namespaces[namespace].items()
    }


def _matches(if_none_match: str | None, etag: str) -> bool:
    """Whether a caller's ``If-None-Match`` names the entity tag we are holding.

    ``*`` matches anything that exists, which is what RFC 9110 says it means and which a
    caching client may legitimately send. A list of tags is honoured because a client that
    has held several revisions may send them all.
    """
    if if_none_match is None:
        return False
    candidates = [candidate.strip() for candidate in if_none_match.split(",")]
    return "*" in candidates or etag in candidates
