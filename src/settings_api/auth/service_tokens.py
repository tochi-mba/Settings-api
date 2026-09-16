"""Which service is calling, and what it was granted.

The service-facing surface takes **two** credentials, and this module handles the first:

* ``Authorization: Bearer <service token>`` proves *which service* is calling. That is this
  module.
* ``X-Settings-User-Token: <the end user's token>`` proves *who it is calling for*. That is
  :meth:`settings_api.auth.tokens.TokenVerifier.verify_for_service`, and the account id comes
  from that token's ``sub`` and from nowhere else.

Both are required, and the split is not decoration. Overloading one header would make it
possible to send only one and have it mean either.

The comparison itself -- constant time, over bytes, with no early return, so the timing of a
refusal says nothing about where in the configured list a guessed token sits -- is
:class:`keyring_client.ServiceAuthenticator`'s, shared with keyring's own internal surface.
This adapter maps the name that comparison yields to the grant this deployment configured for
it, and keeps the refusal in this service's vocabulary.

A configuration where two services share a token is refused at startup, in
:mod:`settings_api.core.config`: whichever name matched would decide which *audience family* a
user token must belong to, so the weaker of two grants would be reachable with the other's
token.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from keyring_client import BAD_SERVICE
from keyring_client import AuthenticationError as ServiceRefusedError
from keyring_client import ServiceAuthenticator as KeyringServiceAuthenticator

from settings_api.core.logging import get_logger
from settings_api.domain.errors import AuthenticationError
from settings_api.domain.namespaces import ServiceGrant

if TYPE_CHECKING:
    from collections.abc import Mapping

    from settings_api.core.config import ServiceConfig

__all__ = ["BAD_SERVICE", "ServiceAuthenticator"]

logger = get_logger(__name__)


class ServiceAuthenticator:
    """Turns a service token into the grant that service was configured with."""

    def __init__(self, *, services: Mapping[str, ServiceConfig]) -> None:
        # The secrets leave their `SecretStr` wrappers once, here, and go straight into the
        # shared authenticator, which renders nothing.
        self._tokens = KeyringServiceAuthenticator(
            {name: service.token.get_secret_value() for name, service in services.items()},
            logger=logger,
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
        return self._tokens.configured

    def identify(self, token: str) -> ServiceGrant:
        """Return the grant belonging to this service token.

        Raises:
            AuthenticationError: the token matches no configured service. A deployment with no
                services configured refuses everything here, which is the right behaviour for
                a service nobody has been told to trust yet.
        """
        try:
            name = self._tokens.identify(token)
        except ServiceRefusedError as exc:
            raise AuthenticationError(BAD_SERVICE) from exc
        return self._grants[name]
