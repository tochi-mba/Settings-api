"""Routers, in mount order.

**The order matters and is not alphabetical.** Starlette matches routes in the order they
are registered, and this service has literal paths that live underneath a path parameter:
``/v1/settings/schema`` would be matched by ``/v1/settings/{namespace}`` and answered with
"no namespace called schema". Within :mod:`settings_api.api.routers.settings` the literal
paths are therefore declared first, and there is a test that reads each of them rather
than a comment asking the next person to remember.

Between routers the ordering is just as load-bearing: ``internal`` is mounted before
``settings`` so that ``/v1/internal/...`` can never be shadowed, even though today the two
prefixes do not overlap. A future ``/v1/settings`` route with a wildcard would otherwise
silently start answering for the service surface, with the wrong authentication.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from settings_api.api.routers import health, internal, settings

if TYPE_CHECKING:
    from fastapi import APIRouter

ROUTERS: tuple[APIRouter, ...] = (health.router, internal.router, settings.router)
