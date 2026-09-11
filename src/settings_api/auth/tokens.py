"""Turning a bearer token into an identity, or into one undifferentiated refusal.

This is the only module that decides who a request is for, and every part of that decision
is made from claims a signature covered. Four rules carry the weight, and each is a
vulnerability when it is dropped rather than a preference about how to spell things.

**The algorithm is pinned to RS256.** Leaving the list open is the classic JWT failure: a
caller sends ``alg: none`` and nothing checks the signature at all, or downgrades RS256 to
HS256 and signs with the public key it fetched from the JWKS endpoint -- an endpoint
published for anybody to fetch, so "only keyring has the key" was never true of that half
of it. There are tests for both.

**Expiry is checked against the injected clock.** PyJWT's own time checks are switched off
because they read the wall clock, which breaks the invariant the rest of the codebase is
built on (see :mod:`settings_api.core.clock`) and makes the expiry rule untestable without
waiting for a token to go stale. Switching off the ``exp`` check alone is not enough,
which is worth knowing before somebody puts it back: PyJWT also refuses a token whose
``iat`` is in the future by the *wall* clock, so a test that pinned the injected clock to
next Tuesday would watch every perfectly good token be refused, by a rule it never asked
for.

**The audience is read twice, and only the second reading decides anything.** PyJWT will
not check an audience it has not been told, and the only place to learn which one a token
claims is the token itself. So the unverified claims are read for that string, it is
handed to ``decode`` as the audience to verify, and everything is then decided from the
verified copy that comes back. The unverified value says what to check; nothing is decided
from it.

**A service may only present tokens from its own audience family.** This is the check the
whole service-facing surface rests on, and it is the reason
:meth:`TokenVerifier.verify_for_service` exists separately from
:meth:`TokenVerifier.verify_owner` rather than taking a flag. Without it, anything able to
reach this service with any service token could present anybody's token for any service
and read that person's settings -- the confused deputy, moved from inside one process into
the gap between two.

Every refusal is the same :class:`~settings_api.domain.errors.AuthenticationError`
carrying the same message, so two rejections differ only in their request id. Which rule
did the refusing goes to the logs, where the operator reads it and a forger does not.
:class:`~settings_api.domain.errors.KeyringUnreachableError` is the exception, and passes
through untouched: it is our failure rather than the caller's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import jwt

from settings_api.auth.jwks import BAD_TOKEN
from settings_api.core.logging import get_logger
from settings_api.domain.errors import AuthenticationError, NamespaceNotGrantedError
from settings_api.domain.namespaces import COMMON, granted_namespaces

if TYPE_CHECKING:
    from settings_api.auth.jwks import JwksClient
    from settings_api.core.clock import Clock
    from settings_api.domain.namespaces import ServiceGrant

logger = get_logger(__name__)

ALGORITHM = "RS256"
"""The only signature algorithm a token may be signed with. See the module docstring."""

REQUIRED_CLAIMS = ("exp", "iat", "iss", "sub", "aud")
"""Claims a token must carry, checked before any of them is believed.

PyJWT verifies most claims only when they are present, so a token that simply omits one is
a token that passes the check for it. Requiring them turns "no audience" from a token that
grants whatever the default is into a token that is refused.
"""

SERVICE_ACTOR_PREFIX = "service:"
"""How a service-mediated write is recorded in ``set_by``.

Prefixed rather than bare, so a stored provenance can never be mistaken for an audience --
and so nobody can arrange for a service to be named the same as an audience family.
"""


@dataclass(frozen=True, slots=True)
class Identity:
    """Who a request is for, and which compartments of their settings it may see."""

    account_id: str
    """The token's verified ``sub``, and the only identity this service ever learns.

    Keyring's opaque account id rather than an email address. Nothing here can turn it
    back into a person, which is what makes it safe to put in a log line and safe to use
    as the key everything else is scoped by.
    """

    audience: str
    """The verified ``aud``. Recorded as provenance on every owner write."""

    namespaces: frozenset[str]
    """Every namespace this request may read and write.

    Derived from the audience on the person-facing surface and from the calling service's
    configured grant on the internal one. There is no route by which a caller's own claim
    about what it may read reaches this value.
    """

    service: str | None = None
    """The calling service, on the internal surface. ``None`` when the person is calling."""

    @property
    def actor(self) -> str:
        """What to record as having made a change.

        The audience for an owner write, and ``service:<name>`` for a service-mediated
        one. Provenance the server derived rather than provenance the writer claimed: a
        caller can choose which token it presents and cannot choose what this says about
        it.
        """
        if self.service is None:
            return self.audience
        return f"{SERVICE_ACTOR_PREFIX}{self.service}"

    def grants(self, namespace: str) -> bool:
        """Whether this request may touch ``namespace``."""
        return namespace in self.namespaces


@dataclass(frozen=True, slots=True)
class VerifiedClaims:
    """What a token says, after a signature and every required claim have been checked."""

    account_id: str
    audience: str


class TokenVerifier:
    """Checks tokens keyring minted, against keys keyring published."""

    def __init__(
        self,
        *,
        jwks: JwksClient,
        issuer: str,
        audience_prefix: str,
        allowed_namespaces: tuple[str, ...],
        clock: Clock,
    ) -> None:
        self._jwks = jwks
        self._issuer = issuer
        self._audience_prefix = audience_prefix
        self._allowed_namespaces = allowed_namespaces
        self._clock = clock

    async def verify_owner(self, token: str) -> Identity:
        """Check a person-facing token and work out which namespaces it grants.

        Raises:
            AuthenticationError: for a bad signature, the wrong audience family, the wrong
                issuer, an expired token, a missing claim, a namespace this deployment
                does not have, a malformed token or a key id that is not keyring's. One
                error with one message for all of them, on purpose.
            KeyringUnreachableError: keyring's keys could not be fetched, so whether this
                token is good is not something we know. Deliberately not caught: turning
                it into an authentication failure would tell a person to log in again
                because another service was briefly down.
        """
        claims = await self._claims(token)
        try:
            namespaces = granted_namespaces(
                claims.audience,
                prefix=self._audience_prefix,
                allowed=self._allowed_namespaces,
            )
        except NamespaceNotGrantedError as exc:
            # An audience belonging to another service, or one naming a namespace this
            # deployment does not have. Both are a token we will not act on, and both are
            # told what every other refusal is told.
            logger.info("token_rejected", reason="audience")
            raise AuthenticationError(BAD_TOKEN) from exc

        return Identity(
            account_id=claims.account_id,
            audience=claims.audience,
            namespaces=namespaces,
        )

    async def verify_for_service(self, token: str, *, grant: ServiceGrant) -> Identity:
        """Check an end user's token presented by a service, for that service.

        **The audience check here is the confused-deputy defence.** The token must belong
        to the calling service's own audience family, so media-tool may present only
        tokens minted for media-tool. Without it, a service token -- which is a static
        string a deployment configures, not something a person mints -- would be enough to
        read anybody's settings, because the service could pair it with any user token it
        could get hold of.

        The namespaces come from the configured grant rather than from the audience. A
        service's compartment is a deployment decision, and letting the token widen it
        would mean a service that could mint its own scopes.

        Raises:
            AuthenticationError: every refusal in :meth:`verify_owner`, plus a token
                minted for a different service. Undifferentiated, for the same reason.
            KeyringUnreachableError: unchanged, and still a 503.
        """
        claims = await self._claims(token)
        if not grant.accepts_audience(claims.audience):
            logger.info("token_rejected", reason="foreign_audience", service=grant.service)
            raise AuthenticationError(BAD_TOKEN)

        return Identity(
            account_id=claims.account_id,
            audience=claims.audience,
            namespaces=grant.namespaces | {COMMON},
            service=grant.service,
        )

    async def _claims(self, token: str) -> VerifiedClaims:
        """Every rule that is about the token itself rather than about what it grants."""
        kid = _kid_of(token)
        key = await self._jwks.key_for(kid)

        # The unverified read has one job, and it is not to decide anything: PyJWT checks
        # an audience only against one it has been handed, so it is handed the one the
        # token claims and then made to prove the signed claim really says that. Every
        # decision below is taken from `claims`, which is the verified copy.
        asserted_audience = _audience_of(token)

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                audience=asserted_audience,
                issuer=self._issuer,
                options={
                    "require": list(REQUIRED_CLAIMS),
                    # Both of the time checks a keyring token can trip are off, for the
                    # one reason given in the module docstring: each of them reads the
                    # wall clock. Expiry is re-checked below against the injected one.
                    # `iat` is not re-checked at all, because PyJWT's only rule for it is
                    # "not issued in the future", which decides nothing `exp` has not
                    # decided already -- but it stays required, since a token without one
                    # is not a token keyring minted.
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
        except jwt.InvalidTokenError as exc:
            logger.info("token_rejected", reason="decode", kid=kid)
            raise AuthenticationError(BAD_TOKEN) from exc

        if self._clock.now().timestamp() >= float(claims["exp"]):
            logger.info("token_rejected", reason="expired", kid=kid)
            raise AuthenticationError(BAD_TOKEN)

        account_id: str = claims["sub"]
        audience: str = claims["aud"]
        return VerifiedClaims(account_id=account_id, audience=audience)


def _kid_of(token: str) -> str:
    """The key id out of the token's unverified header.

    Read before anything has been checked, because it is what chooses the key that would
    do the checking. That is the whole reason :mod:`settings_api.auth.jwks` rate limits
    what an unrecognised id may provoke: this is the one value an unauthenticated caller
    gets to put in front of the verifier.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        logger.info("token_rejected", reason="header")
        raise AuthenticationError(BAD_TOKEN) from exc

    kid = header.get("kid")
    if not isinstance(kid, str):
        logger.info("token_rejected", reason="kid")
        raise AuthenticationError(BAD_TOKEN)
    return kid


def _audience_of(token: str) -> str:
    """The audience a token claims, read without verifying any of it.

    A single string and never a list. A token naming several audiences is one whose holder
    is entitled somewhere else as well, and since what this service grants is derived from
    the audience, "which of them did you mean" would have to be answered by guessing.
    """
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError as exc:
        logger.info("token_rejected", reason="payload")
        raise AuthenticationError(BAD_TOKEN) from exc

    audience = claims.get("aud")
    if not isinstance(audience, str):
        logger.info("token_rejected", reason="audience_shape")
        raise AuthenticationError(BAD_TOKEN)
    return audience
