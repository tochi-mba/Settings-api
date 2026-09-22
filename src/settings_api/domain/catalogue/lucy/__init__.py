"""``lucy`` -- how the assistant behaves for one person.

Everything here is a knob with a small, checkable value: a number, a flag, an enum, a short
list. That constraint is the reason this namespace can exist at all. A setting is at most
4096 bytes and has to be validated against bounds, so *how Lucy should write* cannot live
here -- a prompt override is prose, it has no bounds, and a bad one is not a value out of
range but a voice that sounds wrong. Behaviour like that belongs in a persona note, which
is already the family's home for "lessons about how to behave in this profile". The
division is not arbitrary: knobs here, character there.

Almost every entry used to be ``PROPOSED``. The hub now reads the turn limits, the
model knobs, helper depth and concurrency, memory-write policy, prompt-feed toggles,
new-session defaults (including incognito), whether reasoning is streamed, whether
message bodies may appear in the process log, the context window and reclamation knobs,
idle-session archival, workspace retention on the live block, and the refuse keys on
every turn. Entries that still say ``PROPOSED`` are ones the hub stores in policy or
catalogue but has not yet made the live behaviour of a conversation.
``docs/catalogue.md`` repeats the origin per entry so that nobody ships a setting
believing it does something it does not.

Two entries deserve reading together. ``permission_mode`` decides whether Lucy asks before
acting, and ``approval_policy`` decides what it may never stop asking about. They are
separate because the first is a preference and the second is a floor: a person who turns
everything to ``auto`` is still protected from an irreversible action they never saw,
because the floor is not theirs to lower.

## Why this namespace is a package

Every other namespace here is one module, because a namespace is a table and a table reads
best in one file. This one has enough entries with a paragraph each that that is no longer
true, and it is past the family's limit of a thousand lines in a file.

The split is by what a person is deciding rather than by length, so that the group a
reader wants is the group they open:

- :mod:`~settings_api.domain.catalogue.lucy.model` -- which model answers and how it sounds
- :mod:`~settings_api.domain.catalogue.lucy.context` -- the window, and what is reclaimed first
- :mod:`~settings_api.domain.catalogue.lucy.limits` -- what one turn or plan may do
- :mod:`~settings_api.domain.catalogue.lucy.helpers` -- the assistants Lucy starts beneath itself
- :mod:`~settings_api.domain.catalogue.lucy.permissions` -- what it may do without asking
- :mod:`~settings_api.domain.catalogue.lucy.recall` -- what it keeps about you afterwards
- :mod:`~settings_api.domain.catalogue.lucy.sessions` -- the conversation as a thing of its own
- :mod:`~settings_api.domain.catalogue.lucy.reliability` -- waiting on a sibling that is slow
- :mod:`~settings_api.domain.catalogue.lucy.feeds` -- what reaches the prompt at all

Each of those says in its own docstring why its group is a group, which is the reasoning
that had nowhere to live while this was one file. ``SETTINGS`` below joins them in that
order, and the order is documentation rather than behaviour: it is what somebody reads
down in ``docs/catalogue.md``, starting with the model because that is what they came for
and ending with the prompt-feed toggles over one mechanism because nobody comes for those.

A package satisfies the assembler exactly as a module does -- it offers ``NAMESPACE`` and
``SETTINGS``, which is the whole of the ``NamespaceModule`` protocol -- so nothing in
``_MODULES`` next door knows this happened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from settings_api.domain.catalogue.lucy import (
    context,
    feeds,
    helpers,
    limits,
    model,
    permissions,
    recall,
    reliability,
    sessions,
)
from settings_api.domain.catalogue.lucy.namespace import NAMESPACE as NAMESPACE

if TYPE_CHECKING:
    from settings_api.domain.types import SettingDef

SETTINGS: tuple[SettingDef, ...] = (
    *model.SETTINGS,
    *context.SETTINGS,
    *limits.SETTINGS,
    *helpers.SETTINGS,
    *permissions.SETTINGS,
    *recall.SETTINGS,
    *sessions.SETTINGS,
    *reliability.SETTINGS,
    *feeds.SETTINGS,
)
