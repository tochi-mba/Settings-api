"""Namespaces, and who may read them.

A namespace is one service's compartment, and this module is the whole of the rule that
keeps it one. It is the same shape as user-api's scopes -- an audience family, one grant
per token -- applied to a different question.

## Two surfaces, two ways of deciding

**Person-facing.** The audience is ``settings`` or ``settings.<namespace>``.
``settings`` grants every namespace the deployment recognises; ``settings.search`` grants
exactly ``search``, which is a genuinely useful compartment: an assistant that may set
search preferences and may not touch erasure policy. :func:`granted_namespaces` decides
it, from the verified ``aud`` and from nothing else.

**Service-facing.** A configured :class:`ServiceGrant` says which namespaces the service
may see. A downstream service cannot read ``user.erasure_mode``: a service learns only
what it needs
to do its job, and the blast radius of one compromised service token is one namespace.

## Why `common` is readable by everyone

``common.timezone`` and ``common.locale`` are answers every service needs and none of them
owns. Giving each service its own copy would mean a person answering "what time zone are
you in" once per service and getting it wrong in one of them. So ``common`` is merged
underneath every namespace read, with the namespace winning on a key collision -- which
cannot currently happen, and is defined anyway so that adding a key to ``common`` can
never silently change what a namespace resolves to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from settings_api.domain.errors import NamespaceNotGrantedError, UnknownNamespaceError

if TYPE_CHECKING:
    from collections.abc import Iterable

COMMON = "common"
"""The namespace every service may read, and the one no service owns."""

AUDIENCE_SEPARATOR = "."


@dataclass(frozen=True, slots=True)
class ServiceGrant:
    """One consuming service, and the compartment it may see.

    The token is deliberately absent: this is the *decision*, and the secret that proves a
    caller is entitled to it lives in :mod:`settings_api.auth.service_tokens`. Keeping
    them apart is what lets the interesting rule -- ``accepts_audience`` -- be tested
    without a token anywhere near it.
    """

    service: str
    audience_prefix: str
    namespaces: frozenset[str]

    def grants(self, namespace: str) -> bool:
        """Whether this service may read and write ``namespace``.

        ``common`` is always granted. It is the block every service needs and none owns,
        and a deployment that had to remember to add it to six grants would eventually
        forget it in one.
        """
        return namespace == COMMON or namespace in self.namespaces

    def accepts_audience(self, audience: str) -> bool:
        """Whether a user token with this audience was minted for *this* service.

        **This is the check the whole service-facing surface rests on.** Without it,
        anything able to reach this service with any service token could present any
        user's token for any service and read that person's settings: the confused deputy,
        moved from inside one process into the gap between two. A consuming service may
        present only tokens minted for itself.

        The prefix must match either exactly or as an audience family, so a service whose
        prefix is ``example-tool`` accepts ``example-tool`` and ``example-tool.jobs`` and
        refuses ``example-toolkit`` -- the separator is required, which is what stops one
        service's prefix from being a prefix of another's name.
        """
        return audience == self.audience_prefix or audience.startswith(
            self.audience_prefix + AUDIENCE_SEPARATOR
        )

    def require(self, namespace: str) -> None:
        """Refuse a namespace this service was not granted.

        Raises:
            NamespaceNotGrantedError: naming the service and the namespace. Specific
                because it is a fact about the caller's own grant, and a service that
                could not tell "refused" from "empty" would cache the emptiness.
        """
        if not self.grants(namespace):
            msg = f"{self.service} is not granted the {namespace} namespace"
            raise NamespaceNotGrantedError(msg)


def granted_namespaces(audience: str, *, prefix: str, allowed: Iterable[str]) -> frozenset[str]:
    """Work out which namespaces a person-facing token's audience grants.

    Args:
        audience: the token's verified ``aud`` claim.
        prefix: the audience family this service answers to, normally ``settings``.
        allowed: every namespace this deployment recognises.

    Returns:
        The granted namespaces. The bare prefix grants all of them; ``prefix.name``
        grants that one and, because it is readable by everyone, ``common``.

    Raises:
        NamespaceNotGrantedError: the audience belongs to another service, is malformed,
            or names a namespace this deployment does not have. The caller turns every one
            of these into the same undifferentiated 401, so the distinction is for the
            logs rather than for the wire.
    """
    every = frozenset(allowed)
    if audience == prefix:
        return every

    head, separator, namespace = audience.partition(AUDIENCE_SEPARATOR)
    if not separator or head != prefix:
        # Includes the case that matters most: a token minted for another service presented
        # on the person-facing surface. Verifying it against our own audience would have
        # failed anyway, but this is where it is named.
        msg = f"audience {audience!r} is not in the {prefix!r} family"
        raise NamespaceNotGrantedError(msg)

    if namespace not in every:
        # Refused outright rather than treated as granting nothing. A typo in a mint
        # request would otherwise produce a token that works, reads nothing, and looks
        # like a correctly configured assistant that has simply not been told anything.
        msg = f"audience {audience!r} names a namespace this deployment does not recognise"
        raise NamespaceNotGrantedError(msg)

    return frozenset({namespace, COMMON})


def require_known(namespace: str, *, known: Iterable[str]) -> None:
    """Refuse a namespace that is not in the catalogue at all.

    Checked before the grant, so a caller that misspells a namespace it *does* hold is
    told it misspelled it rather than told it lacks permission -- which is the difference
    between a caller that fixes its typo and one that goes looking for a permissions
    problem it does not have.

    Raises:
        UnknownNamespaceError: naming what this build does have.
    """
    names = frozenset(known)
    if namespace not in names:
        msg = f"no namespace {namespace!r}; this build has {', '.join(sorted(names))}"
        raise UnknownNamespaceError(msg)


def require_granted(namespace: str, *, granted: Iterable[str]) -> None:
    """Refuse a namespace this token does not grant.

    Raises:
        NamespaceNotGrantedError: naming the namespace. A fact about the caller's own
            token, so being specific leaks nothing.
    """
    if namespace not in frozenset(granted):
        msg = f"this token does not grant the {namespace} namespace"
        raise NamespaceNotGrantedError(msg)
