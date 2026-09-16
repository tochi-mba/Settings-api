"""Turning a bearer token into an identity, for either of this service's two surfaces.

Every rule about the token itself -- RS256 only, the issuer pinned, every claim keyring sets
required, expiry checked against the injected clock, one refusal for everything -- belongs to
:class:`keyring_client.TokenVerifier`, which every service in the family shares and which is
tested against keyring's own signer. What is left here is what only this service decides.

**Which audiences each surface answers to.** The person-facing surface answers to the
``settings`` family, and the namespaces a token grants come from its verified audience
(:func:`~settings_api.domain.namespaces.granted_namespaces`). The service-facing surface
answers only to the calling service's own family (:meth:`ServiceGrant.accepts_audience`), and
that is the check the whole internal surface rests on: without it, a static service token plus
anybody's user token would read anybody's settings -- the confused deputy, moved into the gap
between two processes. There, the namespaces come from the configured grant and never from
the token, because a service that could widen its own compartment could mint its own scopes.

**The vocabulary.** The library's errors are translated into this service's domain errors at
this boundary, so nothing above it knows a library was involved. Every refusal becomes
:class:`~settings_api.domain.errors.AuthenticationError` carrying :data:`BAD_TOKEN`, whichever
rule did the refusing; the reason goes to the log. Keyring being unreachable stays
:class:`~settings_api.domain.errors.KeyringUnreachableError` -- a 503 rather than a 401,
because telling a person to log in again when keyring blipped is advice that does not help.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from keyring_client import ALGORITHM, REQUIRED_CLAIMS, AudienceFamily
from keyring_client import AuthenticationError as TokenRefusedError
from keyring_client import KeyringUnreachableError as KeysUnavailableError
from keyring_client import TokenVerifier as KeyringTokenVerifier

from settings_api.auth.jwks import BAD_TOKEN, KEYS_UNAVAILABLE
from settings_api.core.logging import get_logger
from settings_api.domain.errors import (
    AuthenticationError,
    KeyringUnreachableError,
    NamespaceNotGrantedError,
)
from settings_api.domain.namespaces import COMMON, granted_namespaces

if TYPE_CHECKING:
    from keyring_client import AudiencePolicy, VerifiedToken

    from settings_api.auth.jwks import JwksClient
    from settings_api.core.clock import Clock
    from settings_api.domain.namespaces import ServiceGrant

__all__ = ["ALGORITHM", "REQUIRED_CLAIMS", "SERVICE_ACTOR_PREFIX", "Identity", "TokenVerifier"]

logger = get_logger(__name__)

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

    Keyring's opaque account id rather than an email address. Nothing here can turn it back
    into a person, which is what makes it safe to put in a log line and safe to use as the key
    everything else is scoped by.
    """

    audience: str
    """The verified ``aud``. Recorded as provenance on every owner write."""

    namespaces: frozenset[str]
    """Every namespace this request may read and write.

    Derived from the audience on the person-facing surface and from the calling service's
    configured grant on the internal one. There is no route by which a caller's own claim about
    what it may read reaches this value.
    """

    service: str | None = None
    """The calling service, on the internal surface. ``None`` when the person is calling."""

    @property
    def actor(self) -> str:
        """What to record as having made a change.

        The audience for an owner write, and ``service:<name>`` for a service-mediated one.
        Provenance the server derived rather than provenance the writer claimed: a caller can
        choose which token it presents and cannot choose what this says about it.
        """
        if self.service is None:
            return self.audience
        return f"{SERVICE_ACTOR_PREFIX}{self.service}"

    def grants(self, namespace: str) -> bool:
        """Whether this request may touch ``namespace``."""
        return namespace in self.namespaces


@dataclass(frozen=True, slots=True)
class _GrantAudience:
    """A service grant, as the audience policy the shared verifier applies.

    The rule stays in :meth:`ServiceGrant.accepts_audience`, in the domain, where it is written
    down and tested on its own; this only hands it to the verifier.
    """

    grant: ServiceGrant

    def accepts(self, audience: str) -> bool:
        return self.grant.accepts_audience(audience)


class TokenVerifier:
    """Checks tokens keyring minted, and decides what each surface lets them do."""

    def __init__(
        self,
        *,
        jwks: JwksClient,
        issuer: str,
        audience_prefix: str,
        allowed_namespaces: tuple[str, ...],
        clock: Clock,
    ) -> None:
        self._verifier = KeyringTokenVerifier(jwks=jwks, issuer=issuer, clock=clock, logger=logger)
        self._family = AudienceFamily(audience_prefix)
        self._audience_prefix = audience_prefix
        self._allowed_namespaces = allowed_namespaces

    async def verify_owner(self, token: str) -> Identity:
        """Check a person-facing token and work out which namespaces it grants.

        Raises:
            AuthenticationError: any refusal the shared verifier makes, an audience outside
                the ``settings`` family, or one naming a namespace this deployment does not
                have. One message for all of them.
            KeyringUnreachableError: keyring's keys could not be fetched, so whether this token
                is good is not something we know.
        """
        verified = await self._verify(token, self._family)
        try:
            namespaces = granted_namespaces(
                verified.audience,
                prefix=self._audience_prefix,
                allowed=self._allowed_namespaces,
            )
        except NamespaceNotGrantedError as exc:
            # An audience naming a namespace this deployment does not have. Refused rather
            # than treated as granting nothing, and told what every other refusal is told.
            logger.info("token_rejected", reason="namespace")
            raise AuthenticationError(BAD_TOKEN) from exc

        return Identity(
            account_id=verified.account_id,
            audience=verified.audience,
            namespaces=namespaces,
        )

    async def verify_for_service(self, token: str, *, grant: ServiceGrant) -> Identity:
        """Check an end user's token presented by a service, for that service.

        Raises:
            AuthenticationError: every refusal in :meth:`verify_owner`'s token rules, plus a
                token minted for a different service. Undifferentiated, for the same reason.
            KeyringUnreachableError: unchanged, and still a 503.
        """
        verified = await self._verify(token, _GrantAudience(grant))
        return Identity(
            account_id=verified.account_id,
            audience=verified.audience,
            namespaces=grant.namespaces | {COMMON},
            service=grant.service,
        )

    async def _verify(self, token: str, audience: AudiencePolicy) -> VerifiedToken:
        """Run the shared rules, and translate their verdict into this service's vocabulary."""
        try:
            return await self._verifier.verify(token, audience=audience)
        except TokenRefusedError as exc:
            raise AuthenticationError(BAD_TOKEN) from exc
        except KeysUnavailableError as exc:
            raise KeyringUnreachableError(KEYS_UNAVAILABLE) from exc
