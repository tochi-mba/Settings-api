"""Which service is calling, and what it was granted.

The service-facing surface takes **two** credentials, and this module handles the first of
them. The shape is keyring's, applied to a different question:

* ``Authorization: Bearer <service token>`` proves *which service* is calling. That is
  this module.
* ``X-Settings-User-Token: <the end user's token>`` proves *who it is calling for*. That
  is :meth:`settings_api.auth.tokens.TokenVerifier.verify_for_service`, and the account id
  comes from that token's ``sub`` and from nowhere else.

Both are required, and the split is not decoration. Overloading one header would make it
possible to send only one and have it mean either. There is deliberately no request
parameter that names an account: a service that could name one could name anybody's.

## The comparison does not stop early

The loop below compares against every configured service and does not break on a match. A
comparison that returned as soon as it found one would leak, in its timing, roughly where
in the list the caller sits -- which over enough requests is a way of learning how many
services a deployment has and which position a guessed token is closest to. The individual
comparisons are :func:`hmac.compare_digest`, so neither the match nor the loop reveals
anything in the time it takes.

A configuration where two services share a token is refused at startup, in
:mod:`settings_api.core.config`, and the reason belongs here: whichever name matched would
decide which *audience family* is acceptable for the user token, so the weaker of the two
grants would be reachable with the other's token. That is the confused-deputy hole
reopened from the inside.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from settings_api.core.logging import get_logger
from settings_api.domain.errors import AuthenticationError
from settings_api.domain.namespaces import ServiceGrant

if TYPE_CHECKING:
    from collections.abc import Mapping

    from settings_api.core.config import ServiceConfig

logger = get_logger(__name__)

BAD_SERVICE = "service credentials were not accepted"
"""One message for every way a service token can be wrong. As with user tokens, two
spellings of "no" are two answers somebody can tell apart while guessing."""


class ServiceAuthenticator:
    """Turns a service token into the grant that service was configured with."""

    def __init__(self, *, services: Mapping[str, ServiceConfig]) -> None:
        # The secrets are pulled out of their `SecretStr` wrappers once, here, rather than
        # per request. The wrapper exists to keep a token out of a repr and out of a log
        # line, and this object is never rendered.
        self._tokens: tuple[tuple[str, str], ...] = tuple(
            (name, service.token.get_secret_value()) for name, service in services.items()
        )
        self._grants: dict[str, ServiceGrant] = {
            name: ServiceGrant(
                service=name,
                audience_prefix=service.audience_prefix,
                namespaces=frozenset(service.namespaces),
            )
            for name, service in services.items()
        }

    @property
    def configured(self) -> tuple[str, ...]:
        """Every configured service name, for the readiness check and for diagnostics."""
        return tuple(sorted(self._grants))

    def identify(self, token: str) -> ServiceGrant:
        """Return the grant belonging to this service token.

        Raises:
            AuthenticationError: the token matches no configured service. A deployment
                with no services configured refuses everything here, which is the right
                behaviour for a service nobody has been told to trust yet.
        """
        matched: str | None = None
        for name, configured in self._tokens:
            # No `break`. See the module docstring.
            if hmac.compare_digest(token, configured):
                matched = name

        if matched is None:
            logger.info("service_token_rejected")
            raise AuthenticationError(BAD_SERVICE)
        return self._grants[matched]
