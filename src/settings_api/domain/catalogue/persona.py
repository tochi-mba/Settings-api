"""``persona`` -- the assistant's model of itself, and what happens to it.

persona-api has **no per-account settings at all** today: its configuration is one
process-wide model built once at startup and frozen into its adapters as plain integers,
and its schema has no settings table and no seam where a per-account override could be
applied. So every entry here is a proposal, and each one names the change persona-api
needs before the value does anything. ``docs/catalogue.md`` repeats that per entry, so
nobody ships a setting that silently does nothing.

Two of them are worth reading together. persona-api allows twenty personas per account and
has no notion of which one to load when nobody says, so ``default_persona`` is a real
question with no current answer. And it has no erasure story whatsoever -- forgetting is a
permanent tombstone with no expiry, its own operations documentation says so, and there is
no sweeper -- so ``erasure_mode`` and ``grace_days`` here are proposing the *mechanism*
rather than configuring one. They are spelled exactly as user-api's so that a person who
has answered the question once does not have to answer a differently-shaped version of it.
"""

from __future__ import annotations

from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingScope, SettingType

NAMESPACE = "persona"

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="default_persona",
        scope=SettingScope.PROFILE,
        value_type=SettingType.STR,
        default=None,
        nullable=True,
        max_chars=64,
        pattern=r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$",
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(None,),
        origin=Origin.PROPOSED,
        origin_note=(
            "New here. persona-api allows `max_personas_per_account` = 20 and has no "
            "default-selection rule; every route takes the persona as a path segment."
        ),
        summary="Which persona to load when a conversation does not name one.",
        description=(
            "Null means there is no default and a caller must name one, which is what "
            "persona-api requires today. Naming one here makes 'no persona given' mean that "
            "persona instead of an error.\n\n"
            "Null is the conservative value, and for an unusual reason: falling back to null "
            "makes the call fail loudly, while falling back to a name would silently load "
            "*a* persona during an outage -- and a persona is the voice the assistant speaks "
            "in, so the wrong one is conspicuous in a way a wrong integer is not."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="recall_default_limit",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=20,
        minimum=1,
        maximum=100,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(20,),
        origin=Origin.EXISTING,
        origin_note=(
            "persona-api has `recall_default_limit` = 20, but it is dead: nothing reads it, "
            "and the live default is the literal `limit: LimitQuery = 20` on six routes."
        ),
        summary="How many items a recall returns when the caller does not ask for a number.",
        description=(
            "Bounded above by persona-api's own `recall_max_limit`, which is 100. Raising "
            "this means more context loaded by default and a larger prompt; lowering it means "
            "less to read and more round trips.\n\n"
            "Marked as existing rather than proposed because the knob is written down in "
            "persona-api's configuration -- but wiring it up means fixing a defect there "
            "first: the setting exists and is never read, so the documented default and the "
            "actual default are two different twenties that could drift apart without anyone "
            "noticing."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="log_values",
        value_type=SettingType.BOOL,
        default=False,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(False,),
        origin=Origin.PROPOSED,
        origin_note="New here. persona-api has an event log; it does not record old values.",
        summary="Whether persona-api's change log keeps the old value when something changes.",
        description=(
            "The same decision as `user.log_values` and spelled the same way, because a "
            "person answering 'does the log keep what I changed' should answer it once per "
            "service at most and never in two different vocabularies.\n\n"
            "Off is conservative: a log that recorded nothing can be reconstructed by asking, "
            "and a log that recorded something it should not have cannot be unrecorded."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="erasure_mode",
        value_type=SettingType.ENUM,
        default="grace",
        choices=("grace", "immediate", "tombstone"),
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("grace", "tombstone"),
        origin=Origin.PROPOSED,
        origin_note=(
            "New here, and it proposes a mechanism rather than configuring one: persona-api "
            "has no grace period, no retention window and no sweeper."
        ),
        summary="What deleting one persona field or note does: schedule it, destroy it, or mark it.",
        description=(
            "Identical in meaning to `user.erasure_mode`, and deliberately identical in "
            "spelling. persona-api today does exactly one of these three -- `tombstone` -- "
            "without calling it anything: forgetting sets a marker that nothing ever purges.\n\n"
            "So adopting this means persona-api gains a sweeper and a grace path. Until it "
            "does, setting this changes nothing, and the generated catalogue documentation "
            "says so.\n\n"
            "As in user-api, a change here must never be retroactive."
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
        origin=Origin.PROPOSED,
        origin_note="New here; depends on the same persona-api change as `persona.erasure_mode`.",
        summary="How long a deleted persona field or note stays recoverable before it is destroyed.",
        description=(
            "Only meaningful once `persona.erasure_mode` is `grace`, and only meaningful at "
            "all once persona-api has a sweeper.\n\n"
            "Thirty days to match user-api, for the same reason the vocabulary matches: two "
            "services that hold a person's data and forget it on different schedules are two "
            "things to remember rather than one."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_pinned_fields",
        value_type=SettingType.INT,
        default=20,
        minimum=1,
        maximum=20,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(20,),
        origin=Origin.EXISTING,
        origin_note="persona-api's `max_pinned_fields`, default 20.",
        summary="How many persona fields go into the prompt on every turn.",
        description=(
            "persona-api's own docstring puts it exactly right: pinned is a token budget, "
            "not a preference. Every pinned field is bytes in the assistant's prompt on "
            "every single turn, so this is really 'how much of the context window may this "
            "persona spend describing itself'.\n\n"
            "The same decision as `user.max_pinned`, and spelled the same way on purpose. A "
            "person who has decided to load less by default should not have to make that "
            "decision twice in two different vocabularies."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_pinned_notes",
        value_type=SettingType.INT,
        default=20,
        minimum=1,
        maximum=20,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(20,),
        origin=Origin.EXISTING,
        origin_note="persona-api's `max_pinned_notes`, default 20.",
        summary="How many persona notes go into the prompt on every turn.",
        description=(
            "The other half of the always-load block. Separate from `max_pinned_fields` "
            "because persona-api counts them separately and because they cost differently: "
            "a field is a short named fact and a note is up to four thousand characters of "
            "prose.\n\n"
            "Somebody who wants a persona that remembers what it is but not every episode "
            "lowers this and leaves the fields alone, which is not expressible with one "
            "combined number."
        ),
    ),
)
