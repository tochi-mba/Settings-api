"""Destroying a person's settings, properly.

Two steps, and the second is the one that is easy to leave out.

**The ``DELETE`` removes the rows from the b-tree.** It does not remove the bytes. In WAL
mode the pages a transaction touched live in the ``-wal`` file next to the database until
a checkpoint moves them, so a service that deleted and stopped would leave the forgotten
values sitting in a sidecar, findable with ``grep``, for as long as the log went
unchecked.

**``PRAGMA wal_checkpoint(TRUNCATE)`` is what actually erases.** ``FULL`` is not enough:
it flushes the log into the database and leaves the log's own pages where they are.
``TRUNCATE`` empties the file. A checkpoint cannot run inside a transaction, which is why
this is a step after rather than a line inside.

``PRAGMA secure_delete`` is on as well, and it is worth being clear that it is not what
makes this work: it overwrites pages as they are freed, which covers the freelist case,
and the checkpoint is what covers the case that actually arises here.

The test for all of this scans the **bytes** of the database and its ``-wal`` for a
sentinel. It is the one test in this repository that a unit test cannot replace, because
it is about the file rather than about the code -- and it is written that way on purpose,
so that it would survive a change of mechanism to ``VACUUM`` without being rewritten.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from settings_api.core.logging import get_logger

if TYPE_CHECKING:
    from settings_api.auth.tokens import Identity
    from settings_api.settings.service import SettingsService
    from settings_api.storage.database import Database

logger = get_logger(__name__)


class Erasure:
    """ "Forget everything you know about me", done in a way that survives ``grep``."""

    def __init__(self, *, database: Database, service: SettingsService) -> None:
        self._db = database
        self._service = service

    async def forget(self, identity: Identity) -> int:
        """Destroy every setting, the account row and the whole event log.

        Returns how many settings rows went, which is what the response reports. The
        event log goes too and is not counted separately: "delete everything you know
        about me" has one honest meaning, and a surviving log saying what used to be set
        is not it.
        """
        removed = await self._service.forget(identity)
        # Outside the transaction, because a checkpoint cannot run inside one. Every write
        # this account ever made is in the log until this runs.
        await self._db.checkpoint_truncate()
        logger.info("erasure_completed", removed=removed)
        return removed
