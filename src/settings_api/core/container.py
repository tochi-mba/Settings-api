"""The composition root.

Every adapter is chosen and wired here, once, and handed to the app. Nothing else
constructs its own dependencies -- which is what makes the whole service testable by
substitution, and what keeps "which store" a configuration decision rather than a code
one.

Two things happen here that are worth knowing before editing it.

**The policy file is read at startup and never again.** A policy that could change under a
running process would mean a pin that appears between a read and a write, and a caller
told 409 for a setting that was writable a moment ago with nothing in the response to
explain it. Changing policy is a restart, which is what a deployment does to change any of
its other decisions.

**Nothing reaches keyring.** The JWKS client is constructed and does not fetch. A service
that refused to start unless keyring were reachable would turn one outage into two, at the
moment these services are being restarted together.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from settings_api.auth.jwks import JwksClient
from settings_api.auth.service_tokens import ServiceAuthenticator
from settings_api.auth.tokens import TokenVerifier
from settings_api.core.clock import SystemClock
from settings_api.core.logging import get_logger
from settings_api.domain import policy as policy_rules
from settings_api.events.sql_log import SqlEventLog
from settings_api.settings.erasure import Erasure
from settings_api.settings.service import SettingsService
from settings_api.settings.sql_store import SqlSettingsStore
from settings_api.settings.sweeper import RetiredSweeper
from settings_api.storage.database import Database
from settings_api.storage.migrator import migrate

if TYPE_CHECKING:
    from settings_api.core.clock import Clock
    from settings_api.core.config import Settings
    from settings_api.domain.policy import Policy
    from settings_api.events.log import EventLog
    from settings_api.settings.store import SettingsStore

logger = get_logger(__name__)


@dataclass(slots=True)
class Container:
    """Everything the API needs, already wired together."""

    settings: Settings
    clock: Clock
    database: Database
    policy: Policy
    # The ports, not the adapters. Annotating these with the concrete classes would make
    # every consumer depend on which adapter was wired, which is the one thing a
    # composition root exists to prevent.
    events: EventLog
    store: SettingsStore
    service: SettingsService
    erasure: Erasure
    sweeper: RetiredSweeper
    jwks: JwksClient
    verifier: TokenVerifier
    services: ServiceAuthenticator
    started_monotonic: float
    _sweeper_task: asyncio.Task[None] | None = None

    @classmethod
    def build(cls, settings: Settings, *, clock: Clock | None = None) -> Container:
        """Construct every adapter named by ``settings``.

        Raises:
            PolicyError: the configured policy file is missing, malformed, or says
                something the catalogue refuses. A startup error on purpose: this is the
                only moment at which somebody is in a position to fix it.
        """
        clock = clock or SystemClock()
        database = Database(settings.database_path)
        migrate(database, now=clock.now())
        policy = policy_rules.load(settings.policy_path)

        events = SqlEventLog(database=database)
        store = SqlSettingsStore(database=database, events=events)
        service = SettingsService(
            store=store, events=events, policy=policy, clock=clock, config=settings
        )
        jwks = JwksClient(
            url=settings.keyring_jwks_url,
            clock=clock,
            cache_seconds=settings.jwks_cache_seconds,
            min_refetch_seconds=settings.jwks_min_refetch_seconds,
            timeout_seconds=settings.keyring_http_timeout_seconds,
            # The shared client's diagnostics -- a refused key id, a fetch that failed -- land
            # in this service's structured, redacted log rather than the standard library's.
            logger=get_logger("settings_api.auth.jwks"),
        )

        return cls(
            settings=settings,
            clock=clock,
            database=database,
            policy=policy,
            events=events,
            store=store,
            service=service,
            erasure=Erasure(database=database, service=service),
            sweeper=RetiredSweeper(
                store=store,
                database=database,
                clock=clock,
                retention_days=settings.retired_retention_days,
                event_cap=settings.max_events,
            ),
            jwks=jwks,
            verifier=TokenVerifier(
                jwks=jwks,
                issuer=settings.keyring_issuer,
                audience_prefix=settings.audience_prefix,
                allowed_namespaces=settings.allowed_namespaces,
                clock=clock,
            ),
            services=ServiceAuthenticator(services=settings.services),
            started_monotonic=clock.monotonic(),
        )

    @property
    def uptime_seconds(self) -> float:
        return self.clock.monotonic() - self.started_monotonic

    def start_sweeper(self) -> None:
        """Begin destroying rows belonging to keys that have left the catalogue."""
        self._sweeper_task = asyncio.create_task(self._sweep_forever(), name="retired-sweeper")

    async def aclose(self) -> None:
        """Shut everything down in dependency order."""
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper_task
            self._sweeper_task = None

        await self.jwks.aclose()
        # Last: everything above may still want to write on its way out.
        await self.database.aclose()

    async def _sweep_forever(self) -> None:
        """Sweep, then wait, rather than wait, then sweep.

        The other order has a gap nobody would guess at from the outside: rows whose
        retention window expired while the service was stopped would sit there for a
        further whole interval after it came back, because the first thing the loop did
        was sleep for an hour. user-api shipped the other order and had to change it.
        """
        while True:
            await self._sweep_guarded()
            await asyncio.sleep(self.settings.sweep_interval_seconds)

    async def _sweep_guarded(self) -> None:
        """Run one sweep, surviving any failure.

        A sweep failure must not kill the sweeper: the next tick tries again. Without this
        the first transient error would silently stop all purging, and rows somebody's
        catalogue change was meant to destroy would sit there indefinitely with nothing
        saying so -- a broken promise that looks exactly like a working service.
        """
        try:
            await self.sweeper.sweep_once()
        except Exception:
            logger.exception("sweep_failed")
