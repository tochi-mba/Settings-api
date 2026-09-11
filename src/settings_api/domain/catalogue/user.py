"""``user`` -- what deletion means, and what the change log keeps.

These four already exist in user-api, three of them behind the ``SettingsStore`` port that
was written on day one against the arrival of this service. Wiring them up is a second
adapter and one line in a composition root; nothing above that port changes.

``erasure_mode`` is deliberately **not** ``owner_writable_only``, and the reason belongs
here rather than in a commit message: user-api already exposes ``PUT /v1/user/settings``,
its ``operation_id`` is public API, and MCP clients have tools bound to it. Marking this
owner-writable would break that route the day this service is turned on. The person is
still the one deciding -- the write travels through user-api holding that person's own
token -- and ADR-0004 argues the trade out loud rather than leaving the next reader to
wonder why the most consequential setting here is not the most protected one.
"""

from __future__ import annotations

from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingType

NAMESPACE = "user"

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="erasure_mode",
        value_type=SettingType.ENUM,
        default="grace",
        choices=("grace", "immediate", "tombstone"),
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("grace", "tombstone"),
        origin=Origin.EXISTING,
        origin_note="user-api's `ErasureMode`, behind its `SettingsStore` port since day one.",
        summary="What deleting one entry actually does: schedule it, destroy it, or keep a mark.",
        description=(
            "`grace` hides it immediately and destroys the bytes after `grace_days`. "
            "`immediate` destroys it inside the request, with no recovery. `tombstone` hides "
            "it and never destroys it, for people who would rather keep the record of what "
            "they changed their mind about.\n\n"
            "**Changes are never retroactive.** Switching to `immediate` does not destroy "
            "what is already waiting out a grace period, and switching away from `tombstone` "
            "does not schedule what is already tombstoned. A settings change that silently "
            "destroyed data would be the worst surprise this family could produce.\n\n"
            "Both `grace` and `tombstone` are safe to fall back to, because neither destroys "
            "anything during the outage. `immediate` is not, which is the whole content of "
            "this entry's conservative set: a future change of default must stay inside it."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="grace_days",
        value_type=SettingType.INT,
        default=30,
        minimum=0,
        maximum=365,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(30,),
        origin=Origin.EXISTING,
        origin_note="user-api's `grace_days`, and its deployment-wide `default_grace_days`.",
        summary="How long a deleted entry stays recoverable before its bytes are destroyed.",
        description=(
            "Only meaningful when `erasure_mode` is `grace`. Thirty days is long enough to "
            "notice a mistake and short enough that 'deleted' still means something. Zero "
            "means the next sweep destroys it, which is close to `immediate` without being "
            "it -- the difference is whether the bytes go inside the request.\n\n"
            "This setting is the reason storage here is sparse rather than dense. user-api "
            "shipped a defect where the row-creating write filled in the *domain* default "
            "instead of the *deployment* default, so an account on a seven-day deployment "
            "whose first ever settings change was something else silently acquired a "
            "thirty-day window. Storing only what somebody actually chose makes that "
            "unrepresentable -- see ADR-0005."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="log_values",
        value_type=SettingType.BOOL,
        default=False,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(False,),
        origin=Origin.EXISTING,
        origin_note="user-api's `log_values`, default false.",
        summary="Whether user-api's change log keeps the old value when something changes.",
        description=(
            "Off by default, and this is a privacy decision rather than a storage one. The "
            "event log is a second copy of the personal data: 'changed diagnosis from X to Y' "
            "*is* the sensitive fact, and an event recording it would survive the purge of "
            "the entry it describes unless something went looking.\n\n"
            "Turned on, the old value is kept and is purged along with its entry, so the "
            "promise still holds -- it is just doing more work. Off is the conservative value "
            "and the one an outage lands on: a log that recorded nothing is recoverable by "
            "asking the person, and a log that recorded something it should not have is not."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="default_write_scope",
        value_type=SettingType.STR,
        default=None,
        nullable=True,
        max_chars=32,
        pattern=r"^[a-z][a-z0-9_]*$",
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(None,),
        origin=Origin.PROPOSED,
        origin_note=(
            "New here. user-api derives an entry's scope from the token's audience and "
            "accepts explicit scopes on a write; it has no per-account default."
        ),
        summary="Which compartment a new entry lands in when the writer does not say.",
        description=(
            "Null means unscoped, which is what user-api does today: an entry with no scope "
            "is readable by every valid token. Naming a scope here makes new entries land in "
            "that compartment instead, for somebody who would rather the default were "
            "narrow.\n\n"
            "It can only ever *narrow*: a token still cannot write to a scope it does not "
            "grant, so setting this to a scope an assistant's token lacks makes that "
            "assistant's writes fail rather than making them privileged. Null is the "
            "conservative value because it is what every existing entry already assumes, so "
            "an outage cannot silently change where writes land."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_pinned",
        value_type=SettingType.INT,
        default=40,
        minimum=1,
        maximum=40,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(40,),
        origin=Origin.EXISTING,
        origin_note="user-api's `max_pinned`, default 40.",
        summary="How many entries may be in the block loaded at the start of every conversation.",
        description=(
            "The always-load set is a token budget before it is a preference: every pinned "
            "entry is bytes on every conversation this person has. Lowering it is how "
            "somebody says 'load less about me by default'.\n\n"
            "A person may lower the operator's cap and may never raise it, which is the "
            "general rule for every cap here. A deployment that serves a smaller context "
            "narrows it in policy, and `describe_settings` then reports the narrowed maximum "
            "rather than this one."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="search_default_limit",
        value_type=SettingType.INT,
        default=20,
        minimum=1,
        maximum=100,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(20,),
        origin=Origin.EXISTING,
        origin_note="user-api's `search_default_limit`, bounded above by its `search_max_limit`.",
        summary="How many entries a recall returns when the caller does not ask for a number.",
        description=(
            "More context loaded by default against a larger prompt, or less to read and "
            "more round trips. The maximum here is user-api's own `search_max_limit`, not a "
            "number chosen here.\n\n"
            "The counterpart of `persona.recall_default_limit`, and present for the same "
            "reason: the two services ask a person the same question about two different "
            "records, and leaving one of them deployment-wide while the other is personal "
            "would be an inconsistency nobody could explain."
        ),
    ),
)
