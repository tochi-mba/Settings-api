"""Reading one person's settings from settings-api, correctly, from six services.

Everything here exists so that six consuming services do not each get the same four
things slightly wrong: caching, revalidation, single-flight, and what to do during an
outage.

## How the cache is keyed, and why not the obvious way

The obvious design keys the cache by **account id**, read out of the user token's ``sub``
claim without verifying it. It is a vulnerability. This client does not verify tokens --
that is settings-api's job, and putting a JWKS fetch and a signature check into every
consuming service is precisely what this family avoids -- so an unverified ``sub`` is a
string the caller supplied. Anything that could hand a consuming service a forged token
claiming ``sub: victim`` would be served the victim's cached settings, *without a request
to settings-api ever being made*. The server's authorisation is bypassed by the cache in
front of it.

So the cache is keyed by **the token itself**. Nothing is ever served for a token
settings-api has not authorised at least once. Tokens are short-lived, which sounds like
it would defeat the cache and does not: within one token's life the client serves from
memory and revalidates with ``If-None-Match``, and a new token for the same person costs
one request rather than one per call. The cost is a full response rather than a 304 on the
first call with a new token, and that is the price of not having the hole above.

Two things *are* shared across tokens, because neither is person-specific: the
**fallbacks** for a namespace (the deployment's defaults and each key's outage rule) and
nothing else. That sharing is what lets a client behave correctly during an outage for a
person it has never seen -- see :meth:`SettingsClient.resolve`.

## Single-flight

Ten concurrent requests for one person's namespace produce one outbound call. Without it,
a cold cache under load is a thundering herd pointed at a service that is, by
construction, on the critical path of every other service in the family.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

from settings_client.errors import SettingsRejected, SettingsUnavailable
from settings_client.models import Fallback, OnUnavailable, ResolvedSettings, Value

if TYPE_CHECKING:
    from collections.abc import Mapping

USER_TOKEN_HEADER = "X-Settings-User-Token"  # noqa: S105 -- a header name
DEFAULT_TTL_SECONDS = 60.0
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_CACHED_TOKENS = 512
"""How many distinct tokens are cached before the least recently used is dropped.

Bounded because a long-running service sees a new token every fifteen minutes per person,
and an unbounded dictionary keyed on them is a slow leak that only shows up in production.
"""


@runtime_checkable
class SettingsClient(Protocol):
    """What a consuming service depends on.

    A Protocol rather than a base class so a service's tests can substitute
    :class:`settings_client.testing.FakeSettingsClient` without a network, and so the
    dependency in a composition root is the shape rather than the implementation.
    """

    async def resolve(self, namespace: str, *, user_token: str) -> ResolvedSettings:
        """This person's effective settings for one namespace, with ``common`` merged in."""
        ...

    async def set(self, namespace: str, key: str, value: Value, *, user_token: str) -> int:
        """Write one setting on this person's behalf. Returns the new revision."""
        ...

    async def aclose(self) -> None:
        """Release the connection pool."""
        ...


@dataclass(slots=True)
class _Entry:
    """One cached namespace for one token."""

    settings: ResolvedSettings
    etag: str
    validated_at: float


class HttpSettingsClient:
    """The real client: HTTP, cached, revalidated, single-flighted.

    Args:
        base_url: where settings-api lives.
        service_token: this service's own token, proving which service is calling.
        ttl_seconds: how long a cached namespace is served without revalidating.
        timeout_seconds: per-request timeout. Short on purpose -- this call is on the
            critical path of somebody else's request, and a slow settings-api must degrade
            to a stale read rather than to a slow one.
        max_cached_tokens: the LRU bound. See :data:`DEFAULT_MAX_CACHED_TOKENS`.
        transport: an httpx transport, for tests that drive the real app in-process.
    """

    # Six keyword-only arguments, every one an independent decision a deployment makes.
    # An options object would be constructed on one line and unpacked on the next, and
    # would turn a four-line integration into a six-line one.
    def __init__(  # noqa: PLR0913
        self,
        *,
        base_url: str,
        service_token: str,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_cached_tokens: int = DEFAULT_MAX_CACHED_TOKENS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._ttl = ttl_seconds
        self._max_entries = max_cached_tokens
        self._http = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._fallbacks: dict[str, Mapping[str, Fallback]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._clock = _monotonic

    async def resolve(self, namespace: str, *, user_token: str) -> ResolvedSettings:
        """This person's effective settings for one namespace.

        Serves from cache while the entry is younger than ``ttl_seconds``; otherwise
        revalidates with ``If-None-Match``, which is a 304 in the steady state.

        When settings-api cannot be reached:

        * a cached entry for **this token** is served with ``stale=True``;
        * otherwise, if this client has seen the namespace's fallbacks before, a document
          is assembled from them -- ``use_default`` keys get their default, ``refuse``
          keys go into :attr:`~settings_client.models.ResolvedSettings.refused` and raise
          when read;
        * otherwise there is nothing honest to return, and
          :class:`~settings_client.errors.SettingsUnavailable` is raised.

        Raises:
            SettingsUnavailable: settings-api is unreachable and nothing is known.
            SettingsRejected: settings-api answered, and refused -- a namespace this
                service was not granted, or a user token it may not present.
        """
        key = (user_token, namespace)
        cached = self._entries.get(key)
        if cached is not None and self._clock() - cached.validated_at < self._ttl:
            self._touch(key)
            return cached.settings

        async with self._lock_for(key):
            # Asked again inside the lock: ten callers arriving together on a cold entry
            # all queue here, and the nine that waited want the answer the first one
            # fetched rather than a fetch of their own.
            cached = self._entries.get(key)
            if cached is not None and self._clock() - cached.validated_at < self._ttl:
                return cached.settings
            return await self._fetch(namespace, user_token=user_token, cached=cached)

    async def set(self, namespace: str, key: str, value: Value, *, user_token: str) -> int:
        """Write one setting on this person's behalf. Returns the new revision.

        Only ever the person's own decision travelling through this service. The cached
        entry for this token is dropped rather than patched, because the response says
        what the revision became and not what every other setting resolved to.

        Raises:
            SettingsRejected: settings-api refused -- 403 for a namespace this service was
                not granted or a setting only the person may change, 409 for a pinned
                value, 422 for a value the catalogue does not allow.
            SettingsUnavailable: settings-api could not be reached. A write is never
                silently dropped and never queued: the caller is told, because the person
                is standing there having just asked for it.
        """
        url = f"{self._base_url}/v1/internal/settings/{namespace}/{key}"
        try:
            response = await self._http.put(
                url, json={"value": value}, headers=self._headers(user_token)
            )
        except httpx.HTTPError as exc:
            message = f"settings-api could not be reached to write {namespace}.{key}"
            raise SettingsUnavailable(message) from exc

        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise SettingsRejected(response.status_code, _detail_of(response))

        self._entries.pop((user_token, namespace), None)
        body: dict[str, Any] = response.json()
        return int(body["revision"])

    async def aclose(self) -> None:
        """Release the connection pool."""
        await self._http.aclose()

    # -- Internals ---------------------------------------------------------------------

    def _headers(self, user_token: str) -> dict[str, str]:
        """Both credentials: which service is calling, and who it is calling for."""
        return {
            "Authorization": f"Bearer {self._service_token}",
            USER_TOKEN_HEADER: user_token,
        }

    def _lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        """The single-flight lock for one token and namespace."""
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def _fetch(
        self, namespace: str, *, user_token: str, cached: _Entry | None
    ) -> ResolvedSettings:
        """Fetch or revalidate, and fall back correctly when neither is possible."""
        headers = self._headers(user_token)
        if cached is not None:
            headers["If-None-Match"] = cached.etag

        try:
            response = await self._http.get(
                f"{self._base_url}/v1/internal/settings/{namespace}", headers=headers
            )
        except httpx.HTTPError:
            return self._degrade(namespace, cached=cached)

        if response.status_code == httpx.codes.NOT_MODIFIED and cached is not None:
            # Nothing changed. Refresh the clock on what we hold rather than re-parsing a
            # body we did not receive.
            cached.validated_at = self._clock()
            return cached.settings

        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            # settings-api is up but unwell -- a 503 while it cannot reach keyring, say.
            # That is an outage from this caller's point of view, so it degrades rather
            # than raising something a service would have to special-case.
            return self._degrade(namespace, cached=cached)

        if response.status_code >= httpx.codes.BAD_REQUEST:
            # A 401, 403 or 404 is a fact about this caller, not an outage. Degrading here
            # would hide a misconfigured grant behind defaults that happened to work.
            raise SettingsRejected(response.status_code, _detail_of(response))

        return self._store(namespace, user_token, response)

    def _store(self, namespace: str, user_token: str, response: httpx.Response) -> ResolvedSettings:
        """Parse a successful response, cache it, and remember the fallbacks."""
        body: dict[str, Any] = response.json()
        fallbacks = {
            key: Fallback(
                default=item["default"],
                on_unavailable=OnUnavailable(item["on_unavailable"]),
            )
            for key, item in body["fallbacks"].items()
        }
        # Kept per namespace rather than per token: a default and an outage rule are facts
        # about the deployment, not about a person, so sharing them leaks nothing and is
        # what lets a cold client behave correctly during an outage.
        self._fallbacks[namespace] = fallbacks

        settings = ResolvedSettings(
            namespace=namespace,
            values=body["settings"],
            fallbacks=fallbacks,
            revision=body["revision"],
        )
        self._remember(
            (user_token, namespace),
            _Entry(
                settings=settings,
                etag=response.headers.get("ETag", ""),
                validated_at=self._clock(),
            ),
        )
        return settings

    def _degrade(self, namespace: str, *, cached: _Entry | None) -> ResolvedSettings:
        """What to serve when settings-api cannot be reached. See :meth:`resolve`."""
        if cached is not None:
            # The person's own most recent values, which are strictly better than any
            # default. Not revalidated, so the clock is left where it is: the next call
            # tries again rather than settling into a stale read for a whole TTL.
            return ResolvedSettings(
                namespace=cached.settings.namespace,
                values=cached.settings.values,
                fallbacks=cached.settings.fallbacks,
                revision=cached.settings.revision,
                stale=True,
            )

        fallbacks = self._fallbacks.get(namespace)
        if fallbacks is None:
            message = (
                f"settings-api could not be reached and nothing is known about "
                f"{namespace}; this client has never had a successful response for it"
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

    def _remember(self, key: tuple[str, str], entry: _Entry) -> None:
        """Cache an entry, evicting the least recently used if we are at the bound."""
        self._entries[key] = entry
        self._touch(key)
        while len(self._entries) > self._max_entries:
            oldest = next(iter(self._entries))
            del self._entries[oldest]
            self._locks.pop(oldest, None)

    def _touch(self, key: tuple[str, str]) -> None:
        """Move an entry to the most-recently-used end.

        A plain dict, relying on insertion order, rather than ``OrderedDict``: dicts have
        preserved insertion order since 3.7 and this needs exactly one operation
        ``OrderedDict`` would give a name to.
        """
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._entries[key] = entry


def _monotonic() -> float:
    """Elapsed time for cache ages.

    A monotonic reading rather than a wall clock, because a cache TTL must survive the
    machine's clock being adjusted. This client is a library used by services that inject
    their own clocks; it takes the reading itself because a settings client that required
    a clock to be constructed would be one nobody wires up in four lines.
    """
    return asyncio.get_event_loop().time()


def _detail_of(response: httpx.Response) -> str:
    """The ``detail`` out of a problem+json body, or something honest if there is none."""
    try:
        body: dict[str, Any] = response.json()
    except ValueError:
        return response.text[:200]
    detail = body.get("detail")
    return detail if isinstance(detail, str) else response.text[:200]
