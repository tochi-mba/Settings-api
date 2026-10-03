"""Reading one person's settings from settings-api, correctly, from any service.

Everything here exists so that the consuming services do not each get the same four
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

## An answer this client cannot use is an outage

A 2xx whose body is not the document settings-api sends -- a proxy's HTML page, an empty
body, JSON without ``settings`` or ``fallbacks``, an ``on_unavailable`` this version does
not know -- is treated exactly as if settings-api could not be reached, and logged without
any value in it. Raising ``KeyError`` or ``ValueError`` instead would reach every consuming
service as a 500, where each of them promises a 503 for an unusable settings-api.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
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

_log = logging.getLogger("settings_client")


class _Unusable(Exception):  # noqa: N818 -- private, and never raised past this module
    """settings-api answered, and the body is not something this client can use.

    ``reason`` names what was wrong and never carries a value out of the body: a reason is
    logged, and a setting's value is the person's.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@runtime_checkable
class SettingsClient(Protocol):
    """What a consuming service depends on.

    A Protocol rather than a base class so a service's tests can substitute
    :class:`settings_client.testing.FakeSettingsClient` without a network, and so the
    dependency in a composition root is the shape rather than the implementation.
    """

    async def resolve(
        self, namespace: str, *, user_token: str, profile: str | None = None
    ) -> ResolvedSettings:
        """This person's effective settings for one namespace, with ``common`` merged in.

        ``profile`` selects profile-scoped values. Account-scoped values are always included.
        """
        ...

    async def set(
        self,
        namespace: str,
        key: str,
        value: Value,
        *,
        user_token: str,
        profile: str | None = None,
    ) -> int:
        """Write one setting on this person's behalf. Returns the new revision.

        ``profile`` is required for profile-scoped keys and ignored for account-scoped ones.
        """
        ...

    def forget(self, user_token: str, namespace: str | None = None) -> None:
        """Stop serving this person's cached settings: one namespace, or every one.

        For a setting changed some other way than :meth:`set` -- through settings-api's own
        routes, by another client -- which this client cannot see happen. The next
        :meth:`resolve` for that token asks settings-api again.
        """
        ...

    async def aclose(self) -> None:
        """Release the connection pool."""
        ...


@dataclass(slots=True)
class _Flight:
    """One token, namespace and profile being fetched: its lock, and who is using it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    callers: int = 0


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
        self._entries: dict[tuple[str, str, str], _Entry] = {}
        self._fallbacks: dict[str, Mapping[str, Fallback]] = {}
        self._flights: dict[tuple[str, str, str], _Flight] = {}
        self._clock = _monotonic

    async def resolve(
        self, namespace: str, *, user_token: str, profile: str | None = None
    ) -> ResolvedSettings:
        """This person's effective settings for one namespace.

        Serves from cache while the entry is younger than ``ttl_seconds``; otherwise
        revalidates with ``If-None-Match``, which is a 304 in the steady state.

        ``profile`` selects profile-scoped values. Account-scoped values always come
        along. The cache is keyed by token, namespace and profile, so two profiles of
        the same person do not share a resolved document.

        When settings-api cannot be reached, answers 5xx, or answers with a body this client
        cannot use:

        * a cached entry for **this token and profile** is served with ``stale=True``;
        * otherwise, if this client has seen the namespace's fallbacks before, a document
          is assembled from them -- ``use_default`` keys get their default, ``refuse``
          keys go into :attr:`~settings_client.models.ResolvedSettings.refused` and raise
          when read;
        * otherwise there is nothing honest to return, and
          :class:`~settings_client.errors.SettingsUnavailable` is raised.

        Raises:
            SettingsUnavailable: settings-api is unreachable, or answered with nothing this
                client can use, and nothing is known.
            SettingsRejected: settings-api answered, and refused -- a namespace this
                service was not granted, or a user token it may not present.
        """
        key = (user_token, namespace, profile or "")
        cached = self._entries.get(key)
        if cached is not None and self._clock() - cached.validated_at < self._ttl:
            self._touch(key)
            return cached.settings

        flight = self._flights.setdefault(key, _Flight())
        flight.callers += 1
        try:
            async with flight.lock:
                # Asked again inside the lock: ten callers arriving together on a cold
                # entry all queue here, and the nine that waited want the answer the first
                # one fetched rather than a fetch of their own.
                cached = self._entries.get(key)
                if cached is not None and self._clock() - cached.validated_at < self._ttl:
                    return cached.settings
                return await self._fetch(
                    namespace, user_token=user_token, profile=profile, cached=cached
                )
        finally:
            # The last caller out takes the lock with it. Kept until an entry was evicted,
            # a lock outlived every resolve that failed -- an outage, a refused grant --
            # because nothing was cached to evict, and with tokens that rotate every few
            # minutes they piled up for as long as the outage lasted.
            flight.callers -= 1
            if not flight.callers:
                del self._flights[key]

    async def set(
        self,
        namespace: str,
        key: str,
        value: Value,
        *,
        user_token: str,
        profile: str | None = None,
    ) -> int:
        """Write one setting on this person's behalf. Returns the new revision.

        Only ever the person's own decision travelling through this service. Cached
        entries for this token and namespace are dropped rather than patched, because
        the response says what the revision became and not what every other setting
        resolved to -- and because a revision bump invalidates every profile's copy.

        Raises:
            SettingsRejected: settings-api refused -- 403 for a namespace this service was
                not granted or a setting only the person may change, 409 for a pinned
                value, 422 for a value the catalogue does not allow or a missing profile.
            SettingsUnavailable: settings-api could not be reached, or answered with a body
                that carries no revision, so whether the write landed is unknown. A write
                is never silently dropped and never queued: the caller is told, because the
                person is standing there having just asked for it.
        """
        url = f"{self._base_url}/v1/internal/settings/{namespace}/{key}"
        params = {"profile": profile} if profile else None
        try:
            response = await self._http.put(
                url,
                json={"value": value},
                headers=self._headers(user_token),
                params=params,
            )
        except httpx.HTTPError as exc:
            message = f"settings-api could not be reached to write {namespace}.{key}"
            raise SettingsUnavailable(message) from exc

        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise SettingsRejected(response.status_code, _detail_of(response))

        # Dropped before the body is read: whatever it says, the write may have landed.
        self._drop_namespace(user_token, namespace)
        try:
            return _revision_of(_json_object(response))
        except _Unusable as unusable:
            _log_unusable(response, f"the write to {namespace}.{key}", unusable)
            message = (
                f"settings-api answered the write to {namespace}.{key} with a body this "
                "client cannot use, so whether it was saved is unknown"
            )
            raise SettingsUnavailable(message) from unusable

    def forget(self, user_token: str, namespace: str | None = None) -> None:
        """Stop serving this person's cached settings: one namespace, or every one.

        :meth:`set` does this for its own writes. A setting changed some other way --
        through settings-api's person-facing routes, by another client -- is invisible here,
        and without this the old value was served for the rest of the cache's life: a
        person who turned something off had it still on for up to a minute.
        """
        if namespace is not None:
            self._drop_namespace(user_token, namespace)
            return
        stale = [key for key in self._entries if key[0] == user_token]
        for key in stale:
            del self._entries[key]

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

    async def _fetch(
        self,
        namespace: str,
        *,
        user_token: str,
        profile: str | None,
        cached: _Entry | None,
    ) -> ResolvedSettings:
        """Fetch or revalidate, and fall back correctly when neither is possible."""
        headers = self._headers(user_token)
        if cached is not None:
            headers["If-None-Match"] = cached.etag
        params = {"profile": profile} if profile else None

        try:
            response = await self._http.get(
                f"{self._base_url}/v1/internal/settings/{namespace}",
                headers=headers,
                params=params,
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

        try:
            settings = _document_of(namespace, response)
        except _Unusable as unusable:
            # Answered, but not with anything usable: a proxy's login page, an empty body,
            # a document from a settings-api newer than this client. To the service whose
            # request this is, that is an outage, and it degrades as one. The cached entry's
            # clock is left alone, so the next call asks again.
            _log_unusable(response, f"the {namespace} namespace", unusable)
            return self._degrade(namespace, cached=cached, why="answered with an unusable body")

        return self._store(namespace, user_token, profile, settings, response)

    def _store(
        self,
        namespace: str,
        user_token: str,
        profile: str | None,
        settings: ResolvedSettings,
        response: httpx.Response,
    ) -> ResolvedSettings:
        """Cache a parsed document, and remember the namespace's fallbacks."""
        # Kept per namespace rather than per token: a default and an outage rule are facts
        # about the deployment, not about a person, so sharing them leaks nothing and is
        # what lets a cold client behave correctly during an outage. Only ever a whole
        # document's: a body that failed to parse halfway taught this client nothing.
        self._fallbacks[namespace] = settings.fallbacks
        self._remember(
            (user_token, namespace, profile or ""),
            _Entry(
                settings=settings,
                etag=response.headers.get("ETag", ""),
                validated_at=self._clock(),
            ),
        )
        return settings

    def _degrade(
        self, namespace: str, *, cached: _Entry | None, why: str = "could not be reached"
    ) -> ResolvedSettings:
        """What to serve when settings-api cannot be reached. See :meth:`resolve`.

        ``why`` completes "settings-api ..." in the error raised when nothing is known.
        """
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
                f"settings-api {why} and nothing is known about "
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

    def _drop_namespace(self, user_token: str, namespace: str) -> None:
        """Drop every cached profile of this namespace. A revision bump invalidates all of them."""
        stale = [key for key in self._entries if key[0] == user_token and key[1] == namespace]
        for key in stale:
            del self._entries[key]

    def _remember(self, key: tuple[str, str, str], entry: _Entry) -> None:
        """Cache an entry, evicting the least recently used if we are at the bound.

        Popped before it is re-inserted, so a refresh of an entry that already existed
        lands at the most-recently-used end rather than keeping its old position.
        """
        self._entries.pop(key, None)
        self._entries[key] = entry
        while len(self._entries) > self._max_entries:
            oldest = next(iter(self._entries))
            del self._entries[oldest]

    def _touch(self, key: tuple[str, str, str]) -> None:
        """Move an existing entry to the most-recently-used end.

        A plain dict, relying on insertion order, rather than ``OrderedDict``: dicts have
        preserved insertion order since 3.7 and this needs exactly one operation
        ``OrderedDict`` would give a name to. Only ever called for a key the caller has
        just found, so there is no "absent" arm to cover.
        """
        self._entries[key] = self._entries.pop(key)


def _monotonic() -> float:
    """Elapsed time for cache ages.

    A monotonic reading rather than a wall clock, because a cache TTL must survive the
    machine's clock being adjusted. This client is a library used by services that inject
    their own clocks; it takes the reading itself because a settings client that required
    a clock to be constructed would be one nobody wires up in four lines.
    """
    return time.monotonic()


def _document_of(namespace: str, response: httpx.Response) -> ResolvedSettings:
    """A resolved namespace out of a successful response, checked as far as it is read.

    Every field this client reads is checked before anything is kept, so a body that fails
    halfway leaves the cache and the learned fallbacks as they were. The values themselves
    are passed through unchecked: they are the person's, and their types are the catalogue's
    business rather than this client's.

    Raises:
        _Unusable: the body is not the document settings-api sends.
    """
    body = _json_object(response)
    values = body.get("settings")
    if not isinstance(values, dict):
        message = "'settings' is missing or not an object"
        raise _Unusable(message)
    declared = body.get("fallbacks")
    if not isinstance(declared, dict):
        message = "'fallbacks' is missing or not an object"
        raise _Unusable(message)
    revision = _revision_of(body)
    return ResolvedSettings(
        namespace=namespace,
        values=values,
        fallbacks={key: _fallback_of(key, item) for key, item in declared.items()},
        revision=revision,
    )


def _fallback_of(key: str, item: object) -> Fallback:
    """One key's declared outage rule, or :class:`_Unusable` naming the key and not the value."""
    if not isinstance(item, dict) or "default" not in item:
        message = f"the fallback for {key!r} is not an object with a default"
        raise _Unusable(message)
    rule = item.get("on_unavailable")
    if not isinstance(rule, str) or rule not in OnUnavailable:
        # A rule this version does not know -- a newer settings-api, most likely. Guessing
        # which of the two it resembles could serve a default the person refused.
        message = f"the fallback for {key!r} has no on_unavailable this client knows"
        raise _Unusable(message)
    return Fallback(default=item["default"], on_unavailable=OnUnavailable(rule))


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """The body as a JSON object, or :class:`_Unusable` saying it is not one."""
    try:
        body = response.json()
    except ValueError:
        # Covers a decoding failure too: UnicodeDecodeError is a ValueError.
        message = "the body is not JSON"
        raise _Unusable(message) from None
    if not isinstance(body, dict):
        message = "the body is not a JSON object"
        raise _Unusable(message)
    return body


def _revision_of(body: dict[str, Any]) -> int:
    """The ``revision``, which settings-api always sends as an integer."""
    revision = body.get("revision")
    # bool is an int to isinstance, and `true` is not a revision.
    if isinstance(revision, bool) or not isinstance(revision, int):
        message = "'revision' is missing or not an integer"
        raise _Unusable(message)
    return revision


def _log_unusable(response: httpx.Response, what: str, unusable: _Unusable) -> None:
    """Say what came back, and never what was in it.

    The status, the content type, the length and what was wrong: enough to tell a proxy's
    login page from a settings-api newer than this client, and nothing a person chose.
    """
    _log.warning(
        "settings-api answered %s for %s with a body this client cannot use "
        "(%s; content-type %s, %d bytes); treating it as unreachable",
        response.status_code,
        what,
        unusable.reason,
        response.headers.get("Content-Type", "none"),
        len(response.content),
    )


def _detail_of(response: httpx.Response) -> str:
    """The ``detail`` out of a problem+json body, or something honest if there is none."""
    try:
        body: dict[str, Any] = response.json()
    except ValueError:
        return response.text[:200]
    detail = body.get("detail")
    return detail if isinstance(detail, str) else response.text[:200]
