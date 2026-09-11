"""Destroying rows for keys that have left the catalogue.

A catalogue entry may carry ``retired_at``. From that moment its rows are ignored on every
read -- so the setting is gone from the service's behaviour immediately -- but the rows
themselves survive for ``retired_retention_days``.

**That delay is the whole design, and it is not caution for its own sake.** Deleting on
sight would mean that merging a catalogue typo, or retiring a key and then changing your
mind, destroys every affected person's expressed choice in the moment the deploy lands,
with nothing to restore from. Because reads merely ignore the rows, reverting the
catalogue within the window restores every one of those choices intact. Ninety days is
long enough to notice.

The sweep is over every account at once, because a retirement is a fact about the
catalogue rather than about anybody's settings. Each affected account gets one event
saying its stored choice was destroyed, recorded at that account's current revision
without bumping it: the rows had been ignored on every read since the key was retired, so
no resolved value changed, and invalidating every cached copy of an unchanged document
would be a lie told to six services at once.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from settings_api.core.logging import get_logger
from settings_api.domain.registry import every_entry

if TYPE_CHECKING:
    from settings_api.core.clock import Clock
    from settings_api.domain.types import SettingDef
    from settings_api.settings.store import SettingsStore
    from settings_api.storage.database import Database

logger = get_logger(__name__)


class RetiredSweeper:
    """Removes the rows belonging to retired keys, once their window has passed."""

    def __init__(
        self,
        *,
        store: SettingsStore,
        database: Database,
        clock: Clock,
        retention_days: int,
        event_cap: int,
    ) -> None:
        self._store = store
        self._db = database
        self._clock = clock
        self._retention_days = retention_days
        self._event_cap = event_cap

    def due(self) -> list[SettingDef]:
        """Every retired entry whose retention window has passed.

        Compared as dates rather than as instants: ``retired_at`` is the day a key left
        the catalogue and carries no time of day, so treating it as midnight and
        subtracting would make the window end at a different moment depending on when the
        process happens to run.
        """
        cutoff = self._clock.now().date() - timedelta(days=self._retention_days)
        return [
            entry
            for entry in every_entry()
            if entry.retired_at is not None and date.fromisoformat(entry.retired_at) < cutoff
        ]

    async def sweep_once(self) -> int:
        """Destroy the rows for every key whose window has passed. Returns how many went.

        The truncating checkpoint runs once per sweep rather than once per key, and only
        when something was actually removed -- a checkpoint on every tick of an idle
        sweeper is a write amplifier on a service that otherwise does nothing all day.
        """
        removed = 0
        for entry in self.due():
            removed += await self._store.purge_setting(
                entry.namespace,
                entry.key,
                now=self._clock.now(),
                event_cap=self._event_cap,
            )

        if removed:
            # The deletes above left the old values in the -wal file. See
            # settings_api.settings.erasure for why this step is the one that erases.
            await self._db.checkpoint_truncate()
            logger.info("retired_rows_purged", removed=removed)
        return removed
