"""FastAPI dependency wiring.

The container is built once at startup and parked on the app; these turn it into typed
parameters so handlers never reach into application state themselves.

Two identities are resolved here, and the difference between them is the whole shape of
this service's security model.

:data:`IdentityDep` is the **person calling**. One bearer token, minted by keyring with an
audience in the ``settings`` family, and the namespaces come from that audience.

:data:`ServiceIdentityDep` is a **service calling on a person's behalf**, and it takes two
credentials:

* ``Authorization: Bearer <service token>`` proves which service is calling. Compared in
  constant time against every configured service, without an early return.
* ``X-Settings-User-Token: <the end user's token>`` proves who it is calling for, and the
  account id comes from its ``sub`` and from nowhere else.

Both are required, and the user token's audience must belong to the calling service's own
family. Without that last check, anything able to reach this service with any service
token could read anybody's settings -- the confused deputy, moved from inside one process
into the gap between two.

**No account id appears in any path, ever**, on either surface. There is no
``/v1/settings/{account_id}`` and no query parameter naming a subject, so a cross-account
read is not forbidden -- it is inexpressible. This is keyring's ``/v1/internal`` trick
applied to a whole service.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from settings_api.auth.tokens import Identity
from settings_api.core.container import Container
from settings_api.core.context import set_account_id, set_service
from settings_api.domain.errors import AuthenticationError, RevisionMismatchError

USER_TOKEN_HEADER = "X-Settings-User-Token"  # noqa: S105 -- a header name
"""Where the end user's signed token travels on the internal surface.

A separate header from ``Authorization``, because the two credentials are genuinely
different things: one says which service is calling, the other says who it is calling for.
Overloading a single header would make it possible to send only one and have it mean
either.
"""

IF_MATCH_HEADER = "If-Match"
IF_NONE_MATCH_HEADER = "If-None-Match"

bearer_scheme = HTTPBearer(
    auto_error=False,
    description=(
        "On `/v1/settings`, a short-lived token from keyring minted with audience "
        "`settings` or `settings.<namespace>`. On `/v1/internal`, the calling service's "
        "own service token."
    ),
)
"""``auto_error=False`` so a missing header raises our error, in our problem+json shape.

Left to itself, HTTPBearer raises a bare 403 with a plain JSON body -- a different status
and a different shape from every other failure this service produces, which a caller then
has to special-case.
"""

MISSING_CREDENTIALS = "a token is required"
MISSING_USER_TOKEN = "a user token is required"  # noqa: S105 -- a message, not a token


def get_container(request: Request) -> Container:
    """Return the container assembled during startup."""
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def get_identity(
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Identity:
    """Turn the bearer token into the only identity this service ever learns.

    Binds the account into the request context as a side effect, so every log record
    produced by the rest of the request says who it was for without any handler passing it
    along. Context variables are task-local, so one person's request can never see
    another's binding -- there is a concurrency test for exactly that.

    Raises:
        AuthenticationError: no token, or one that is not accepted -- expired, forged, for
            another service, from another issuer, or naming a namespace this deployment
            does not have. All of them look identical on the wire.
        KeyringUnreachableError: the public keys could not be fetched, so the token could
            not be checked either way. A 503, not a 401: the token may be perfectly good.
    """
    if credentials is None:
        raise AuthenticationError(MISSING_CREDENTIALS)

    identity = await container.verifier.verify_owner(credentials.credentials)
    set_account_id(identity.account_id)
    return identity


IdentityDep = Annotated[Identity, Depends(get_identity)]


async def get_service_identity(
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    user_token: Annotated[str | None, Header(alias=USER_TOKEN_HEADER)] = None,
) -> Identity:
    """Resolve which service is calling, and which person it is calling for.

    The service token is checked first, so an unauthenticated caller cannot use this
    endpoint to have arbitrary user tokens verified -- which would make this a free token
    oracle for anybody who could reach it.

    Raises:
        AuthenticationError: either credential is missing, the service token matches no
            configured service, or the user token is not accepted -- including the case
            that matters most, a user token minted for a *different* service. One message
            for all of them.
        KeyringUnreachableError: unchanged, and still a 503.
    """
    if credentials is None:
        raise AuthenticationError(MISSING_CREDENTIALS)
    grant = container.services.identify(credentials.credentials)

    if user_token is None:
        raise AuthenticationError(MISSING_USER_TOKEN)

    identity = await container.verifier.verify_for_service(user_token, grant=grant)
    set_account_id(identity.account_id)
    set_service(grant.service)
    return identity


ServiceIdentityDep = Annotated[Identity, Depends(get_service_identity)]

IfMatchDep = Annotated[
    str | None,
    Header(
        alias=IF_MATCH_HEADER,
        description=(
            "Optimistic concurrency. Pass the ETag from a previous read and the write is "
            "refused with 412 if anything has changed since."
        ),
    ),
]

IfNoneMatchDep = Annotated[
    str | None,
    Header(
        alias=IF_NONE_MATCH_HEADER,
        description="Pass the ETag from a previous read to get a 304 when nothing has changed.",
    ),
]


def revision_from_if_match(if_match: str | None, identity: Identity) -> int | None:
    """Turn an ``If-Match`` entity tag into the revision the caller expects.

    Returns ``None`` when the header is absent, which means the caller is not doing
    optimistic concurrency and the write proceeds.

    A malformed tag, or one belonging to a different account, is a **412 rather than a
    400**. The distinction is deliberate: the caller stated a precondition, and the honest
    answer to a precondition this server cannot satisfy is that it was not satisfied.
    Treating it as a malformed request would invite a client to strip the header and retry
    -- which is exactly the write the precondition existed to prevent.

    Raises:
        RevisionMismatchError: the tag is malformed or names another account.
    """
    if if_match is None:
        return None

    tag = if_match.strip()
    if tag.startswith("W/"):
        # A weak validator says "semantically equivalent", which is not a claim anybody
        # should act on when the next step is an overwrite.
        raise RevisionMismatchError(_bad_tag(tag))
    unquoted = tag.strip('"')

    account, separator, revision = unquoted.rpartition(".")
    if not separator or account != identity.account_id or not revision.isdigit():
        raise RevisionMismatchError(_bad_tag(tag))
    return int(revision)


def _bad_tag(tag: str) -> str:
    """Why an entity tag was refused. Names the shape, never the tag's account part."""
    return (
        "the If-Match entity tag is not one this account was given; "
        f"re-read the settings and use the ETag from that response (got {len(tag)} characters)"
    )
