"""What Lucy may do without asking, and what it must ask about whatever else was allowed.

``permission_mode`` is the preference and ``approval_policy`` is the floor under it. The
first is what somebody chose; the second is what ``auto`` cannot switch off, which is why
a person who turned everything to ``auto`` is still protected from an irreversible action
they never saw. The floor is not theirs to lower, and that is the design rather than an
oversight.

``confirm_outward_actions`` is a floor of the same kind drawn in a different place: around
where an effect lands rather than around which tool produced it. An unwanted file is
deleted and forgotten; an unwanted message has been read by the time anybody notices.

The two capability lists are here because they answer the same question one step earlier.
Not "may Lucy do this now", but "is this among the things Lucy does at all".
"""

from __future__ import annotations

from settings_api.domain.catalogue.lucy.namespace import NAMESPACE
from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingScope, SettingType

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="permission_mode",
        scope=SettingScope.PROFILE,
        value_type=SettingType.ENUM,
        default="ask",
        choices=("ask", "accept_edits", "plan", "auto"),
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("ask", "plan"),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub applies this as the default on a new session when create omits it. "
            "The session row is the live value after that."
        ),
        summary="Whether Lucy asks before doing something that changes the world.",
        description=(
            "`ask` checks anything not already granted. `accept_edits` grants workspace "
            "writes and asks about everything else. `plan` is read-only and refuses every "
            "write with a sentence saying so. `auto` allows everything and asks nothing -- "
            "explicit opt-in, and every event records that the mode was `auto`, because a "
            "decision nobody was asked about should at least be one somebody can find.\n\n"
            "Falling back to `ask` during an outage means more prompts, never fewer."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="approval_policy",
        value_type=SettingType.ENUM,
        default="destructive_always_asks",
        choices=("destructive_always_asks", "spend_and_destructive_ask"),
        on_unavailable=OnUnavailable.REFUSE,
        origin=Origin.EXISTING,
        origin_note="The hub refuses the turn when this cannot be confirmed, rather than guessing the floor.",
        summary="The things Lucy must ask about no matter what else you have allowed.",
        description=(
            "This is a floor, not a preference: it is what `auto` cannot switch off. "
            "Something irreversible always gets a question.\n\n"
            "It refuses rather than falling back, and that is deliberate. Every other "
            "setting here has a safe default to land on during an outage; this one's job is "
            "to be the last thing standing, so a settings outage must not be a way to find "
            "out what happens when it is absent."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="confirm_outward_actions",
        scope=SettingScope.PROFILE,
        value_type=SettingType.BOOL,
        default=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(True,),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub asks before an outward write even in auto, unless a grant already "
            "allows it. Plan mode still refuses writes."
        ),
        summary="Whether anything other people will see is confirmed before it happens.",
        description=(
            "Sending a message, posting something, adding to a shared playlist, writing to a "
            "repository somebody else reads. These are the actions whose cost is not "
            "technical: an unwanted file is deleted and forgotten, and an unwanted message "
            "has been read by the time you notice.\n\n"
            "On asks first, whatever `permission_mode` says -- it is a question about where "
            "the effect lands rather than about which tool produced it, so `auto` does not "
            "switch it off. Off is for somebody running unattended work who has accepted "
            "that. On is conservative and is what an outage lands on: more questions, never "
            "fewer."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="enabled_capabilities",
        value_type=SettingType.STR_LIST,
        default=[],
        max_items=32,
        max_item_chars=48,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=([],),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub names these in the model prompt when they are disconnected; the HTTP "
            "catalogue still lists every pack."
        ),
        summary="Capabilities to offer even when they are not connected yet.",
        description=(
            "Empty means Lucy offers whatever is connected and stays quiet about the rest. "
            "Naming one here makes Lucy mention it and offer to set it up."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="disabled_capabilities",
        value_type=SettingType.STR_LIST,
        default=[],
        max_items=32,
        max_item_chars=48,
        on_unavailable=OnUnavailable.REFUSE,
        origin=Origin.EXISTING,
        origin_note="The hub hides a listed capability from tools. An outage refuses the turn.",
        summary="Capabilities to hide completely, connected or not.",
        description=(
            "A disabled capability is not offered and not mentioned. Lucy is told it is "
            "switched off rather than simply not seeing it, so that it stops suggesting the "
            "thing instead of forgetting the thing exists.\n\n"
            "**This refuses rather than falling back.** An empty list is the default because "
            "that is how Lucy works at all, so landing on it during an outage would re-enable "
            "something the person turned off -- and nobody would find out, because the turn "
            "would succeed."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="claude_code_delegation",
        scope=SettingScope.PROFILE,
        value_type=SettingType.BOOL,
        default=False,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(False,),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub's coder capability reads it. Off, the capability reports disabled and "
            "tells Lucy it is the person's switch. An outage lands on off, never more. No "
            "assistant may change it, with approval or without: a model that could turn it "
            "on could hand itself the person's computer."
        ),
        summary="Whether Lucy may hand a whole task to Claude Code on your machine.",
        description=(
            "On, Lucy can delegate a task you asked for to a real Claude Code session on "
            "this machine -- in the folders you list, at the run level you choose, and only "
            "ever after you approve each task on its own card. Off, the capability is "
            "absent, and Lucy can only tell you where to turn it on.\n\n"
            "Each delegated session runs with your own Claude Code login and spends your "
            "own Claude budget, which is why every switch here is yours alone."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="claude_code_directories",
        scope=SettingScope.PROFILE,
        value_type=SettingType.STR_LIST,
        default=[],
        max_items=16,
        max_item_chars=256,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=([],),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub refuses a delegation whose directory is not listed here, before "
            "anything reaches the bridge. Empty means delegation is off even when the "
            "switch above is on. An outage lands on empty."
        ),
        summary="The folders on your machine a delegated Claude Code task may work in.",
        description=(
            "Absolute paths, one per entry. A task runs in exactly one of them -- Lucy asks "
            "when a request does not say which -- and a folder not on this list is refused "
            "outright, however the task was worded. The list is yours alone to edit; Lucy "
            "can read it to say where she may work, and nothing more."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="claude_code_run_level",
        scope=SettingScope.PROFILE,
        value_type=SettingType.ENUM,
        default="edits",
        choices=("plan", "edits", "full"),
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("plan", "edits"),
        origin=Origin.EXISTING,
        origin_note=(
            "The bridge maps it to the claude CLI's permission mode: plan, acceptEdits, "
            "bypassPermissions. The approval card names the level each task will run at. "
            "An outage falls no looser than edits."
        ),
        summary="How much a delegated Claude Code task may do without asking anyone.",
        description=(
            "`plan` is read-only: the task explores and reports, and changes nothing. "
            "`edits` lets it edit files in its folder and asks about everything beyond "
            "that -- the default, and the sensible place to start. `full` lets it run "
            "commands and act without further prompts, which is for folders you would "
            "trust a contractor alone in.\n\n"
            "Lucy may never change this, with approval or without; the card that asks for "
            "each task says the level it will run at."
        ),
    ),
)
