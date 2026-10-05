"""When Lucy acts on her own: what a watch or check-in may do when it ends with nobody there.

Work that outlives the turn that started it -- "tell me when CI is green", "check back in an
hour" -- ends while nobody is talking. These decide what happens then: whether the turn it
opens may act for the person or only report, whether it may open at three in the morning,
whether a watch wakes the conversation at all when the model did not say, and how long a
watch keeps looking.

Nobody is present when the work ends to read settings, so the hub reads these when the work
is started and writes the answer on it. A change here is therefore not retroactive: a watch
already running keeps the rules it was started under.

``act_unattended`` is the one with a permissive default, because acting on a standing
instruction is what Lucy always did. It refuses rather than falls back, and no assistant may
change it: a model that could turn it on could give itself standing permission.
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
        key="act_unattended",
        scope=SettingScope.PROFILE,
        value_type=SettingType.BOOL,
        default=True,
        on_unavailable=OnUnavailable.REFUSE,
        origin=Origin.EXISTING,
        origin_note=(
            "The hub reads it into TurnPolicy.act_unattended. Off, a watch or check-in records "
            "no standing consent, consent recorded before it was turned off is not used, and "
            "the tool result tells the model the woken turn may only report. Refused, it reads "
            "as off."
        ),
        summary="Whether a turn Lucy opens on her own may act for you, or only report.",
        description=(
            "On, a watch or check-in that wakes the conversation records standing consent you "
            "can see and revoke, so the turn it opens can finish what you asked -- merge the "
            "pull request once CI is green. Off, that turn says what happened and asks before "
            "doing anything.\n\n"
            "On is what Lucy always did, and it is the permissive value, so an outage refuses "
            "rather than guessing: a turn that cannot read this does not act. No assistant may "
            "change it, with approval or without."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="quiet_hours",
        scope=SettingScope.PROFILE,
        value_type=SettingType.STR,
        default="",
        max_chars=11,
        pattern=r"^$|^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$",
        agent_writable=AgentAccess.WITH_APPROVAL,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("",),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub reads it into TurnPolicy.quiet_hours, on the person's "
            "`common.timezone`. A wake inside the window is held back as a durable check-in "
            "due when it closes; the ending's event and live-block line still go out at once."
        ),
        summary="Hours, on your clock, when Lucy opens no turn on her own.",
        description=(
            "`HH:MM-HH:MM` in your time zone, and it may wrap midnight: `23:00-07:00`. Empty "
            "means none. An ending inside the window is still announced where you can see it; "
            "only the turn that tells you waits until the window closes, and it survives a "
            "restart.\n\n"
            "A window whose start and end are the same minute is no window -- 'quiet all day' "
            "is `wake_by_default` turned off. An outage falls back to none: a message that "
            "arrives at night is an annoyance, and one that never arrives is the failure."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="wake_by_default",
        scope=SettingScope.PROFILE,
        value_type=SettingType.BOOL,
        default=True,
        agent_writable=AgentAccess.WITH_APPROVAL,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(True, False),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub reads it into TurnPolicy.wake_by_default, as what `watch.start` and "
            "`repos.watch` do when the model passes no `wake`; the schema says which default "
            "applies."
        ),
        summary="Whether a watch wakes the conversation when it fires, unless told otherwise.",
        description=(
            "Off, a fired watch waits for the next time you talk, and its result is in the "
            "live block then. A watch the model starts with `wake` on still wakes, because "
            "you asked for that one.\n\n"
            "Both values are safe to land on: neither grants anything, and a woken turn is "
            "still bound by `act_unattended` and `quiet_hours`."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="watch_default_minutes",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=5,
        minimum=1,
        maximum=60,
        operator_clampable=True,
        agent_writable=AgentAccess.WITH_APPROVAL,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(5,),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub reads it into TurnPolicy.watch_default_minutes, as the lifetime of a "
            "`watch.start` that names no `for_seconds`. Repository watches have their own "
            "default, `github.watch_default_hours`."
        ),
        summary="How long a watch keeps looking when nobody said how long.",
        description=(
            "A watch never lives past an hour. Expiry is a notice, not a failure: the watch "
            "says it stopped looking and what it last saw.\n\n"
            "Five minutes is what Lucy always did, and the shorter side is the safe one: a "
            "watch that expires early reports and does nothing extra."
        ),
    ),
)
