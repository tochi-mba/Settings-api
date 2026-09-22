"""The conversation as a thing in its own right: how it reads, and when it goes quiet.

A session has a name, a lifetime and some files, and these are the choices about it that
are not about the model's answers at all -- whether it is titled from what it turned out
to be about, how long it stays in the list before being archived, how long its workspace
survives after the last message, whether the working-out is shown while it happens, and
whether a slow turn says so instead of looking like a hang.

``input_policy`` is here rather than with the turn limits because it is a fact about the
conversation and not about a turn: a web page, a command line and another agent can all be
in one session at once, and left to emerge the result is an interleaved transcript nobody
can read.
"""

from __future__ import annotations

from settings_api.domain.catalogue.lucy.namespace import NAMESPACE
from settings_api.domain.types import (
    AgentAccess,
    OnUnavailable,
    Origin,
    SettingDef,
    SettingScope,
    SettingType,
)

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="input_policy",
        scope=SettingScope.PROFILE,
        value_type=SettingType.ENUM,
        default="enqueue",
        choices=("reject", "enqueue", "interrupt", "rollback"),
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("enqueue",),
        origin=Origin.EXISTING,
        origin_note=("The hub applies this as the default on a new session when create omits it."),
        summary="What happens when you say something while Lucy is still working.",
        description=(
            "`enqueue` finishes the current turn and then reads what you said. `interrupt` "
            "stops, keeps the progress, and takes the new message. `rollback` discards the "
            "turn. `reject` refuses the second message.\n\n"
            "This needs an answer because a web page, a command line and another agent can "
            "all be in one conversation at once; left to emerge, the result is an "
            "interleaved transcript nobody can read."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="auto_title",
        scope=SettingScope.PROFILE,
        value_type=SettingType.BOOL,
        default=True,
        agent_writable=AgentAccess.WITH_APPROVAL,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(True, False),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub titles an untitled parent session from the first user message once "
            "the turn completes."
        ),
        summary="Whether a conversation gets a title written from what it is about.",
        description=(
            "On, the first exchange is turned into a short title so a list of sessions reads "
            "as a list of subjects rather than of timestamps. Off, a session keeps the time "
            "it started until you name it yourself.\n\n"
            "The title is derived from the conversation, so it is one more short piece of "
            "text about you, stored where the session is stored and destroyed with it. "
            "Neither value is a restriction against the other, which is why both are safe to "
            "land on: a session that acquired a title during an outage can be renamed, and "
            "one that did not can be titled later."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="session_idle_archive_days",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=30,
        minimum=0,
        maximum=3_650,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(0, 30),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub archives quiet conversations as they are listed, using updated_at. "
            "A live or parked turn is never treated as idle. Zero days means never."
        ),
        summary="How long a conversation sits idle before it is archived. Zero means never.",
        description=(
            "Archiving is about the list, not the data: an archived session is out of the "
            "way, still searchable and still openable. Nothing is deleted by this setting "
            "and nothing can be -- deleting a conversation is something you do deliberately, "
            "and a number in a settings page is not that.\n\n"
            "Zero keeps everything in front of you, which is the honest default for somebody "
            "who has not thought about it. Both zero and the default are safe to fall back "
            "to for the same reason: neither destroys anything, so the worst an outage can "
            "do here is leave a list untidy."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="workspace_retention_hours",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=24,
        minimum=1,
        maximum=720,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(24,),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub puts remaining seconds on the workspace live block from last "
            "activity plus this retention, so a long conversation can see the sandbox "
            "expiry coming."
        ),
        summary="How long a conversation's files survive after it goes quiet.",
        description=(
            "Reading a file does not count as activity in the service that holds it, so a "
            "long conversation can have its files reaped underneath it. Lucy keeps the "
            "sandbox alive while a session is open and warns you before this runs out."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="stream_thinking",
        scope=SettingScope.PROFILE,
        value_type=SettingType.BOOL,
        default=False,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(False,),
        origin=Origin.EXISTING,
        origin_note="The hub streams reasoning events only when this is on.",
        summary="Whether you see Lucy's reasoning as it happens.",
        description=(
            "Off by default because reasoning is working-out rather than an answer, and "
            "reading it as though it were an answer is misleading. Turning it on does not "
            "change what Lucy does, only what you watch."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="notify_on_long_turn",
        scope=SettingScope.PROFILE,
        value_type=SettingType.BOOL,
        default=True,
        agent_writable=AgentAccess.FREELY,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(True,),
        origin=Origin.EXISTING,
        origin_note="The hub emits lucy.turn.slow after this many seconds of a running turn.",
        summary="Whether to say so when a turn is taking a long time.",
        description=(
            "On, a turn that passes `long_turn_seconds` says what it is doing and roughly "
            "how far along it is, so a long wait is a long wait rather than a silence you "
            "have to decide about. Off, it simply arrives when it arrives.\n\n"
            "On is conservative: a notice somebody did not want is an annoyance, and "
            "abandoning a turn that was nearly finished because nothing said so is the "
            "failure this exists to prevent."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="long_turn_seconds",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=60,
        minimum=5,
        maximum=3_600,
        agent_writable=AgentAccess.FREELY,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(60,),
        origin=Origin.EXISTING,
        origin_note="The hub waits this many seconds before emitting lucy.turn.slow.",
        summary="How long a turn runs before it counts as a long one.",
        description=(
            "Only meaningful with `notify_on_long_turn` on. Sixty seconds is roughly where "
            "a person stops assuming the answer is nearly there and starts wondering whether "
            "anything is happening.\n\n"
            "Lower it if you would rather hear early and often; raise it if the work you do "
            "is routinely slow and the notices have become noise. It says nothing about when "
            "a turn is stopped -- that is `max_turn_seconds`, and this one only talks."
        ),
    ),
)
