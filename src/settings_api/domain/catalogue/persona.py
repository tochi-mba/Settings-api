"""``persona`` -- the assistant's model of itself, and what happens to it.

persona-api reads every one of these from here, inside its own caps: how many items a
recall returns by default, how many fields and notes are pinned into the prompt, which
persona ``@default`` names, whether its change log keeps old values, and what forgetting
does.

Two of them are worth reading together. persona-api allows twenty personas per account and
had no notion of which one to load when nobody says, which ``default_persona`` answers.
And forgetting was a permanent tombstone with no expiry; ``erasure_mode`` and
``grace_days`` now choose between that, a grace period its sweeper ends, and destruction
inside the request. They are spelled exactly as user-api's so that a person who has
answered the question once does not have to answer a differently-shaped version of it,
but ``erasure_mode`` defaults to ``tombstone`` where user-api's defaults to ``grace``:
persona-api cannot tell a default from a choice, and a ``grace`` default would start
destroying, thirty days out, everything anybody forgot after this was read.
"""

from __future__ import annotations

from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingScope, SettingType

NAMESPACE = "persona"

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="default_persona",
        scope=SettingScope.ACCOUNT,
        value_type=SettingType.STR,
        default=None,
        nullable=True,
        max_chars=64,
        pattern=r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$",
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(None,),
        origin=Origin.EXISTING,
        origin_note=(
            "persona-api reads it, with no profile, whenever a `{profile}` path segment "
            "is `@default`, and resolves that segment to this persona before the route "
            "runs. Null, an outage, or a name it cannot store leaves `@default` refused."
        ),
        summary="Which persona `@default` loads when a conversation does not name one.",
        description=(
            "Null means there is no default and a caller must name one, as persona-api "
            "always required. Naming one here makes `@default` mean that persona on every "
            "persona route.\n\n"
            "One value per account, not per profile: which persona to load is asked "
            "before there is a profile to name, so a value stored against a profile would "
            "never be read.\n\n"
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
            "persona-api's `recall_default_limit`, default 20, which its list, recall, "
            "export and event routes use when a request names no limit."
        ),
        summary="How many items a recall returns when the caller does not ask for a number.",
        description=(
            "Bounded above by persona-api's own `recall_max_limit`, which is 100. Raising "
            "this means more context loaded by default and a larger prompt; lowering it means "
            "less to read and more round trips."
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
        origin_note=(
            "persona-api reads it on set_field and revise_note: on, a change that replaces "
            "a field's value or a note's body keeps what it replaced in that event. "
            "Whatever destroys the row strips the kept value in the same transaction."
        ),
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
        default="tombstone",
        choices=("grace", "immediate", "tombstone"),
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("tombstone", "grace"),
        origin=Origin.EXISTING,
        origin_note=(
            "persona-api reads it on forget_field and forget_note and writes the answer on "
            "the row: `tombstone` keeps it, `grace` schedules its sweeper, `immediate` "
            "destroys the row, its logged values and its search terms before responding."
        ),
        summary="What deleting a persona field or note does: keep it, destroy it later, or now.",
        description=(
            "Identical in meaning and spelling to `user.erasure_mode`. `tombstone` hides the "
            "field or note and keeps it, recoverable; it is what persona-api always did, so "
            "it is the default. `grace` hides it now and destroys it after `grace_days`; "
            "setting a forgotten field again before then revives it. `immediate` destroys "
            "it inside the request, with no recovery.\n\n"
            "The choice is written on each row when it is forgotten and never re-read, so a "
            "change here is never retroactive."
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
        origin_note=(
            "persona-api writes forget time plus this many days on a row forgotten under "
            "`grace`; its sweeper (hourly by default) destroys the row once that has passed."
        ),
        summary="How long a deleted persona field or note stays recoverable before it is destroyed.",
        description=(
            "Only meaningful when `persona.erasure_mode` is `grace`. The period is fixed on "
            "each row when it is forgotten, so changing this never reschedules something "
            "already waiting. Zero means the next sweep, which is still not `immediate`.\n\n"
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
