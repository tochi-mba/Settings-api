"""Keyring's public keys, through the family's shared client.

Fetching, caching, the single-flight lock, the rate limit on unknown key ids, the floor after
a failed fetch, and the grace for cached keys through a keyring outage all live in
:class:`keyring_client.JwksClient`, in the keyring repository, where they are tested against
keyring's real JWKS endpoint. Six services used to carry six hand-copied versions of that
logic with six slightly different sets of rules; this module keeps the import path the rest
of this service uses, and nothing else.

Two messages are re-exported because they are contract here: every token refusal says
:data:`BAD_TOKEN`, and the readiness check reports :data:`KEYS_UNAVAILABLE` verbatim.

Nothing is fetched while starting up. Constructing the client does no network work, so a
keyring that is down does not stop this service starting -- these services are restarted
together, and a startup dependency would turn one outage into two.
"""

from __future__ import annotations

from keyring_client import BAD_TOKEN, KEYS_UNAVAILABLE, JwksClient

__all__ = ["BAD_TOKEN", "KEYS_UNAVAILABLE", "JwksClient"]
