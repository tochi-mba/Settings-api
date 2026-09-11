"""Substitutes, so a consuming service's tests need no network and no settings-api.

Two of them, for two different jobs.

:class:`FakeSettingsClient` is what almost every test in a consuming service wants: an
in-memory object satisfying :class:`~settings_client.client.SettingsClient`, with values
you set in the test and outage behaviour you can switch on. It is hand-written and
satisfies the real Protocol, so a change to the interface fails to type-check rather than
passing quietly.

:func:`asgi_client` is for the few tests that should exercise the **real** client against
the **real** settings-api, in-process over ASGI. That is what stops the fake and the
service drifting apart, and it is what ``tests/contract/`` in the settings-api repository
uses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from settings_client.client import HttpSettingsClient
from settings_client.errors import SettingsRejected, SettingsUnavailable
from settings_client.models import Fallback, OnUnavailable, ResolvedSettings, Value

if TYPE_CHECKING:
    from collections.abc import Mapping


def asgi_client(
    app: Any,
    *,
    service_token: str,
    base_url: str = "http://settings.test",
    **overrides: Any,
) -> HttpSettingsClient:
    """A real :class:`HttpSettingsClient` talking to a real ASGI app, in-process.

    No sockets, no ports, no fixture that has to start a server and wait for it. The
    client's caching, revalidation and single-flight are all exercised for real; only the
    transport is swapped.
    """
    return HttpSettingsClient(
        base_url=base_url,
        service_token=service_token,
        transport=httpx.ASGITransport(app=app),
        **overrides,
    )


class FakeSettingsClient:
    """An in-memory stand-in that satisfies the real Protocol.

    Set values with :meth:`seed`, then read them as the real client would. Turn
    :attr:`unavailable` on to make every call behave as though settings-api were down --
    which is the case most consuming services forget to test, and the one their users
    notice.
    """

    def __init__(
        self,
        values: Mapping[str, Mapping[str, Value]] | None = None,
        fallbacks: Mapping[str, Mapping[str, Fallback]] | None = None,
    ) -> None:
        self._values: dict[str, dict[str, Value]] = {
            namespace: dict(entries) for namespace, entries in (values or {}).items()
        }
        self._fallbacks: dict[str, dict[str, Fallback]] = {
            namespace: dict(entries) for namespace, entries in (fallbacks or {}).items()
        }
        self.unavailable = False
        """When true, every call behaves as though settings-api could not be reached."""

        self.rejects: dict[str, tuple[int, str]] = {}
        """Namespaces this fake refuses, by name, as ``(status, detail)``.

        For testing what a service does with a 403 -- a namespace it was not granted --
        without having to configure a whole deployment to produce one.
        """

        self.revision = 0
        self.resolves = 0
        """How many times :meth:`resolve` was called. The assertion in a caching test."""

        self.writes: list[tuple[str, str, Value]] = []

    def seed(self, namespace: str, values: Mapping[str, Value]) -> None:
        """Set this person's values for one namespace."""
        self._values.setdefault(namespace, {}).update(values)

    def seed_fallback(self, namespace: str, key: str, fallback: Fallback) -> None:
        """Declare one key's default and outage rule, as the server would."""
        self._fallbacks.setdefault(namespace, {})[key] = fallback

    async def resolve(self, namespace: str, *, user_token: str) -> ResolvedSettings:
        """This person's settings for one namespace, or the configured failure."""
        self.resolves += 1
        if namespace in self.rejects:
            status, detail = self.rejects[namespace]
            raise SettingsRejected(status, detail)

        fallbacks = self._fallbacks.get(namespace, {})
        if self.unavailable:
            if not fallbacks:
                message = (
                    f"settings-api could not be reached and nothing is known about {namespace}"
                )
                raise SettingsUnavailable(message)
            return ResolvedSettings(
                namespace=namespace,
                values={
                    key: fallback.default
                    for key, fallback in fallbacks.items()
                    if fallback.on_unavailable is OnUnavailable.USE_DEFAULT
                },
                fallbacks=fallbacks,
                revision=None,
                stale=True,
                refused=frozenset(
                    key
                    for key, fallback in fallbacks.items()
                    if fallback.on_unavailable is OnUnavailable.REFUSE
                ),
            )

        return ResolvedSettings(
            namespace=namespace,
            values=dict(self._values.get(namespace, {})),
            fallbacks=fallbacks,
            revision=self.revision,
        )

    async def set(self, namespace: str, key: str, value: Value, *, user_token: str) -> int:
        """Record a write and return the new revision."""
        if self.unavailable:
            message = f"settings-api could not be reached to write {namespace}.{key}"
            raise SettingsUnavailable(message)
        if namespace in self.rejects:
            status, detail = self.rejects[namespace]
            raise SettingsRejected(status, detail)

        self.writes.append((namespace, key, value))
        self._values.setdefault(namespace, {})[key] = value
        self.revision += 1
        return self.revision

    async def aclose(self) -> None:
        """Nothing to release. Present because the Protocol has it."""
        return
