"""Which lines a sibling publishes actually reach the model's prompt.

Three switches over the mechanism as a whole -- whether standing feeds appear at all,
whether an incognito session hides the personal ones, and whether a key Lucy never
declared may arrive -- and then one toggle per capability with one per field beneath it,
so that hiding the workspace's process id does not also cost the working directory.

The per-field toggles are generated rather than typed out. There are twenty-one of them,
they differ only in three strings, and the table they come from has to match the hub's own
row for row: a generated set cannot drift entry by entry, and the table below stays short
enough to read as the table it is.

Lucy owns these rather than the sibling that publishes the lines, because a switch the
sibling owned would come back on the moment the sibling restarted.
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

_SWITCHES: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="prompt_feeds_enabled",
        scope=SettingScope.PROFILE,
        value_type=SettingType.BOOL,
        default=True,
        agent_writable=AgentAccess.WITH_APPROVAL,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(True, False),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub reads this at prepare_turn as the master feed switch. Keep the key "
            "in lockstep with lucy_api.context.policy.MASTER; catalogue modules cannot "
            "import the hub."
        ),
        summary="Whether sibling feeds appear in the prompt at all.",
        description=(
            "Off removes persona notes, now-playing, the workspace shell, everything a "
            "sibling publishes for the model to see. Tool results still arrive; this only "
            "governs the standing and live blocks.\n\n"
            "Both values are conservative because neither is a restriction the person "
            "expressed against the other: falling back to on during an outage shows what "
            "Lucy shows today, and a person who turned this off still has the per-capability "
            "switches."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="prompt_hide_personal_feeds",
        value_type=SettingType.BOOL,
        default=True,
        agent_writable=AgentAccess.WITH_APPROVAL,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(True,),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub reads this at prepare_turn. Keep the key in lockstep with "
            "lucy_api.context.policy.HIDE_PERSONAL."
        ),
        summary="Whether incognito hides feeds marked as personal.",
        description=(
            "On, an incognito session does not show who you are, what is playing, or the "
            "workspace you left open. Off still does not log those lines.\n\n"
            "On is the conservative value and the one an outage lands on: hiding more during "
            "a settings outage is recoverable, and showing a personal line the person turned "
            "off is not."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="prompt_allow_unknown_feed_fields",
        value_type=SettingType.BOOL,
        default=False,
        agent_writable=AgentAccess.NEVER,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(False,),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub reads this at prepare_turn. Keep the key in lockstep with "
            "lucy_api.context.policy.ALLOW_UNKNOWN."
        ),
        summary="Whether a sibling may introduce feed keys Lucy does not already know.",
        description=(
            "Off, a new key is dropped. That is the shape of a prompt injection from a "
            "compromised sibling: a field Lucy never declared, carrying an instruction.\n\n"
            "An assistant may never turn this on. Off is conservative and is what an outage "
            "lands on."
        ),
    ),
)


# Keep the rows below in lockstep with lucy_api.context.fields.FIELDS. Catalogue modules
# may import only settings_api.domain.types, so the table is duplicated on purpose and a
# hub test fails if a field ships without a matching key here.
_FEED_CAPS: tuple[str, ...] = ("account", "persona", "music", "workspace", "research")
_FEED_FIELDS: tuple[tuple[str, str, bool, str], ...] = (
    ("account", "pinned", True, "Pinned fields and notes the person asked to keep in view."),
    ("persona", "identity", True, "Who you are, in this profile."),
    ("persona", "notes", True, "Pinned notes about the person."),
    ("music", "now_playing", True, "What is playing, and how far in."),
    ("music", "device", True, "The speaker or computer audio is coming from."),
    ("music", "shuffled", True, "Whether the queue is shuffled."),
    ("music", "repeat", True, "Whether the queue or track repeats."),
    ("music", "queue_head", False, "What is lined up next."),
    ("workspace", "cwd", True, "The current directory inside the workspace."),
    ("workspace", "shell", True, "Which shell is running."),
    ("workspace", "pid", True, "The running shell's process id."),
    ("workspace", "shells_running", True, "How many shells are open right now."),
    ("workspace", "sandbox", True, "How isolated the workspace is."),
    ("workspace", "git_branch", True, "The current git branch, if there is one."),
    ("workspace", "last_command", False, "The last command that ran, without its output."),
    ("research", "backend", False, "Which search backend is in force."),
)


def _feed_flag(key: str, default: bool, summary: str, description: str) -> SettingDef:
    conservative: tuple[bool, ...] = (False,) if default is False else (True, False)
    return SettingDef(
        namespace=NAMESPACE,
        key=key,
        value_type=SettingType.BOOL,
        default=default,
        agent_writable=AgentAccess.WITH_APPROVAL,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=conservative,
        origin=Origin.EXISTING,
        origin_note="The hub applies this as a prompt-feed toggle at prepare_turn.",
        scope=SettingScope.PROFILE,
        summary=summary,
        description=description,
    )


def _feed_toggles() -> tuple[SettingDef, ...]:
    caps = tuple(
        _feed_flag(
            f"feeds_{capability}",
            True,
            f"Whether the {capability} feed appears in the prompt.",
            (
                f"Off hides every {capability} line, including ones left on individually. "
                "Lucy owns this switch so a sibling outage cannot put the lines back."
            ),
        )
        for capability in _FEED_CAPS
    )
    fields = tuple(
        _feed_flag(
            f"feeds_{capability}_{key}",
            default,
            summary,
            (
                f"One line of the {capability} feed. Off leaves the rest of that feed "
                "in place. Unknown keys from a sibling are dropped unless "
                "`prompt_allow_unknown_feed_fields` is on."
            ),
        )
        for capability, key, default, summary in _FEED_FIELDS
    )
    return caps + fields


SETTINGS: tuple[SettingDef, ...] = (*_SWITCHES, *_feed_toggles())
