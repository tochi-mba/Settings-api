"""Shared fixtures.

Every test that touches the app builds its own, so nothing leaks between cases.

Three things here are worth knowing before writing a test against them.

**The clock is fake everywhere, including inside the app.** :func:`create_app` builds its
own container, so the only way to inject a clock into a live application is to build the
container and hand it over -- which :func:`app` does. Nothing in this suite sleeps.

**Keyring is a real RSA key and a real JWKS document over a hand-written transport.** See
:mod:`tests.fakes.keyring`. There is no ``unittest.mock`` in this suite: a fake that
satisfies the real shape fails to type-check when the shape changes, and a patched
attribute does not.

**Tokens are minted with a decade-long lifetime.** Several tests here move the clock
months forward to exercise the retired-key retention window, and a fifteen-minute token
would expire underneath them -- producing a 401 that reads as an authorisation bug rather
than as a test that moved time too far. Tests that are *about* expiry pass their own TTL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from settings_api.api.app import create_app
from settings_api.core.config import LogFormat, Settings
from settings_api.core.container import Container
from settings_api.events.sql_log import SqlEventLog
from settings_api.settings.sql_store import SqlSettingsStore
from settings_api.storage.database import Database
from settings_api.storage.migrator import migrate
from tests.fakes.clock import EPOCH, FakeClock
from tests.fakes.keyring import ISSUER, JWKS_URL, FakeKeyring, mint

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import FastAPI

ACCOUNT = "account-a"
OTHER_ACCOUNT = "account-b"

SPOTIFY_TOKEN = "spotify-service-token-0123456789abcdef"
DOWNSTREAM_TOKEN = "downstream-service-token-0123456789abcdefghij"
USER_API_TOKEN = "user-api-service-token-0123456789abcdef"

SERVICES: dict[str, dict[str, Any]] = {
    "spotify-api": {
        "token": SPOTIFY_TOKEN,
        "audience_prefix": "spotify",
        "namespaces": ["spotify"],
    },
    "downstream-tool": {
        "token": DOWNSTREAM_TOKEN,
        "audience_prefix": "downstream-tool",
        "namespaces": ["environments"],
    },
    "user-api": {
        "token": USER_API_TOKEN,
        "audience_prefix": "user",
        "namespaces": ["user"],
    },
}
"""Three services, chosen so the interesting refusals are expressible.

``downstream-tool``'s audience prefix is deliberately not a prefix of another service's name,
and ``spotify-api``'s is deliberately shorter than its own service name -- the two are
independent strings, and a test that used the same value for both would pass whether or
not the code kept them apart.

**Do not copy the ``spotify`` prefix into a deployment.** In code the two strings are
independent; in a deployment, a service that presents the same user token to keyring's
internal surface must use its keyring service name as its prefix, because keyring accepts
that token only when its audience is exactly that name. See ``docs/integration.md``.
"""


def build_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Test settings, built through validation.

    Overrides go through the constructor rather than ``model_copy(update=...)``, which
    skips validators -- so a namespace list the validator would reject would be accepted
    here and fail somewhere far away instead.
    """
    defaults: dict[str, Any] = {
        "_env_file": None,
        "database_path": tmp_path / "settings.db",
        "log_format": LogFormat.CONSOLE,
        "keyring_issuer": ISSUER,
        "keyring_jwks_url": JWKS_URL,
        "services": SERVICES,
        # High enough that ordinary cases never trip a limit by accident. Tests that are
        # *about* a limit build their own settings with a low one.
        "max_events": 500,
    }
    return Settings(**{**defaults, **overrides})


@pytest.fixture
def clock() -> FakeClock:
    """One clock, shared by the app and by the test that moves it."""
    return FakeClock()


@pytest.fixture
def keyring() -> FakeKeyring:
    """A keyring serving one signing key, counting how often it is asked."""
    return FakeKeyring()


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    """A migrated database on a real file.

    A real file rather than ``:memory:`` on purpose. Durability is one of the properties
    this storage exists for, and the erasure tests read the file's **bytes** -- which an
    in-memory database does not have.
    """
    db = Database(tmp_path / "settings.db")
    migrate(db, now=EPOCH)
    try:
        yield db
    finally:
        await db.aclose()


@pytest.fixture
def events(database: Database) -> SqlEventLog:
    return SqlEventLog(database=database)


@pytest.fixture
def store(database: Database, events: SqlEventLog) -> SqlSettingsStore:
    return SqlSettingsStore(database=database, events=events)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at a scratch directory, with limits tuned for tests."""
    return build_settings(tmp_path)


@pytest.fixture
def app(settings: Settings, clock: FakeClock, keyring: FakeKeyring) -> FastAPI:
    """An app wired to the fake clock and the fake keyring.

    The container is built here and parked on the app before the lifespan runs, and
    :func:`settings_api.api.app.start` then finds it already there. That is the one seam
    this suite needs: ``create_app`` deliberately builds its own container, and a test
    that could not substitute the clock could not test a retention window without waiting
    three months.
    """
    return build_app(settings, clock, keyring)


def build_app(settings: Settings, clock: FakeClock, keyring: FakeKeyring) -> FastAPI:
    """Build an app around one set of settings, a clock and a keyring."""
    built = create_app(settings)
    container = Container.build(settings, clock=clock)
    container.jwks._client = AsyncClient(transport=keyring.transport())
    built.state.container = container
    built.state.prebuilt = container
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client wired straight to the ASGI app, with lifespan run for real."""
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://settings.test") as http,
    ):
        yield http


def container_of(app: FastAPI) -> Container:
    """Reach the wired container, for tests that inspect or substitute an adapter."""
    container: Container = app.state.container
    return container


def token(account_id: str = ACCOUNT, *, namespace: str | None = None, **overrides: Any) -> str:
    """A person-facing token, optionally narrowed to one namespace.

    The audience is assembled here rather than passed, because the audience *is* the
    grant: ``settings`` reads everything and ``settings.search`` reads exactly the search
    namespace. Writing it out at every call site would make that relationship easy to get
    wrong in exactly the tests that are about it.
    """
    audience = "settings" if namespace is None else f"settings.{namespace}"
    return mint(account_id=account_id, audience=audience, **overrides)


def auth(value: str) -> dict[str, str]:
    """The Authorization header for a token."""
    return {"Authorization": f"Bearer {value}"}


def service_auth(service_token: str, user_token: str) -> dict[str, str]:
    """Both credentials the internal surface requires.

    Two headers, because they are two different claims: which service is calling, and who
    it is calling for.
    """
    return {
        "Authorization": f"Bearer {service_token}",
        "X-Settings-User-Token": user_token,
    }


async def set_setting(
    client: AsyncClient,
    tok: str,
    namespace: str = "spotify",
    key: str = "default_market",
    value: Any = "GB",
    *,
    profile: str | None = "personal",
) -> dict[str, Any]:
    """Store one setting and return the response body.

    Raises on anything but success, so a test that meant to set up state cannot silently
    continue with none. Defaults to ``profile=personal`` because the helper's default key
    is profile-scoped; pass ``profile=None`` only when testing the 422 for omitting it.
    """
    params = {"profile": profile} if profile is not None else None
    response = await client.put(
        f"/v1/settings/{namespace}/{key}",
        json={"value": value},
        headers=auth(tok),
        params=params,
    )
    assert response.status_code == 200, response.text
    stored: dict[str, Any] = response.json()
    return stored
