"""``environments`` -- how long an idle sandbox lives, and how many one person keeps.

environments-api gives an assistant a Linux shell per account and profile, and reaps what
nobody is using. Its two reaping clocks and its per-profile cap answer a person's question
-- "how long may my half-finished work sit there", "how many environments do I want to
juggle" -- and today each is one number for the whole box.

Everything that bounds what a sandbox *can do* stays with the operator and is not here:
``allow_network``, ``min_sandbox_tier``, every memory, CPU, disk and output quota,
``max_environments_per_account``, ``operator_accounts`` and ``api_keys``. A person choosing
their own network access or sandbox strength would be a person choosing the machine's
exposure, and that is not a preference. A per-person network *default* was considered and
left out: its only safe fallback during an outage would be "no network", which would change
what everybody gets today whenever settings-api was unreachable.

``default_profile`` is not repeated here. ``common.default_profile`` answers it for every
service, environments-api's ``ENVAPI_DEFAULT_PROFILE`` included.
"""

from __future__ import annotations

from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingScope, SettingType

NAMESPACE = "environments"

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="idle_environment_hours",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=24,
        minimum=1,
        maximum=168,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(24,),
        origin=Origin.EXISTING,
        origin_note="environments-api's `environment_idle_ttl_seconds`, default one day.",
        summary="How long an environment nobody is using survives before it is torn down.",
        description=(
            "An environment with no live shell and no activity for this long is reaped, and "
            "its files go with it. Hours rather than seconds, because this is a person's "
            "choice and seconds are a machine's unit; environments-api converts.\n\n"
            "The maximum is a week, for somebody who leaves work half-finished over a "
            "weekend. Lowering it gives the box its disk back sooner.\n\n"
            "A day is the fallback rather than the short end, and the reason is that the two "
            "directions are not symmetrical here. Reaping early during an outage deletes "
            "somebody's unfinished work, which cannot be fetched again; keeping an idle "
            "sandbox a little longer than asked costs disk. A day is also what "
            "environments-api does today, so an outage changes nothing anybody relies on."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="idle_shell_minutes",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=60,
        minimum=1,
        maximum=1440,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(60,),
        origin=Origin.EXISTING,
        origin_note="environments-api's `shell_idle_ttl_seconds`, default one hour.",
        summary="How long a shell nobody is using stays open before it is closed.",
        description=(
            "Closing an idle shell ends the process running in it, not the environment's "
            "files: those follow `idle_environment_hours`. Minutes rather than seconds, for "
            "the same reason as its neighbour.\n\n"
            "Lowering it makes sure a forgotten session does not sit on a shared machine "
            "with credentials injected into its environment. Raising it, up to a day, suits "
            "long commands that print nothing for a while.\n\n"
            "An hour is the fallback because it is today's behaviour, and because a shell "
            "closed early by an outage would be a long build lost, which is the worse of the "
            "two directions."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_environments_per_profile",
        value_type=SettingType.INT,
        default=5,
        minimum=1,
        maximum=5,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(5,),
        origin=Origin.EXISTING,
        origin_note="environments-api's `max_environments_per_profile`.",
        summary="How many environments this person may keep in one profile at once.",
        description=(
            "The maximum is the operator's own per-profile cap rather than a number with "
            "meaning of its own: a person may lower it and never raise it, and the "
            "operator's per-account cap still applies across every profile on top.\n\n"
            "Lowering it is how somebody stops an assistant from creating a fresh "
            "environment for every question instead of reusing the one it has.\n\n"
            "Falling back to the cap during an outage means environments-api behaves "
            "exactly as it does today. Nothing about this setting protects anybody's "
            "privacy, so landing on the cap weakens nothing."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="default_shell",
        scope=SettingScope.PROFILE,
        value_type=SettingType.ENUM,
        default="bash",
        choices=("bash", "sh"),
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("bash",),
        origin=Origin.PROPOSED,
        origin_note=(
            "New here. environments-api has one deployment-wide `shell_binary`, so it needs "
            "to accept a choice between installed shells before this does anything."
        ),
        summary="Which shell a new session starts when the request does not name one.",
        description=(
            "`sh` is for people whose scripts are meant to be portable and who want to find "
            "out when they are not. `bash` is what environments-api starts today.\n\n"
            "The choices are deliberately two shells every sandbox image has. A free-text "
            "path would be a person choosing which binary runs inside the sandbox, which is "
            "the operator's decision, and a shell that is not installed would be a setting "
            "that breaks every session it applies to.\n\n"
            "`bash` is the fallback because it is today's behaviour and the more capable of "
            "the two, so an outage never turns a working script into a failing one."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="persist_history",
        scope=SettingScope.PROFILE,
        value_type=SettingType.BOOL,
        default=False,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(False,),
        origin=Origin.PROPOSED,
        origin_note=(
            "New here. environments-api keeps shell history inside the sandbox for the life "
            "of the shell and does not write it across sessions."
        ),
        summary="Whether a shell's command history survives the shell that wrote it.",
        description=(
            "On, the next session in the same environment can arrow-up through what ran "
            "before. Off, history dies with the process.\n\n"
            "Off is conservative: command history is a second copy of whatever was typed, "
            "including tokens pasted in a hurry, and an outage that started keeping it would "
            "be a record nobody asked for."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="command_timeout_seconds",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=120,
        minimum=5,
        maximum=3600,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(120,),
        origin=Origin.PROPOSED,
        origin_note=(
            "New here. environments-api times out a command with a deployment-wide number; "
            "this is the per-person default the request may still override."
        ),
        summary="How long a command may run before the shell kills it, when nobody says.",
        description=(
            "Five seconds is for people who want a hung install to fail fast. An hour is for "
            "a long build that prints nothing for a while. The request may still name a "
            "shorter or longer limit inside this range.\n\n"
            "Two minutes is today's behaviour in spirit and is therefore the fallback: an "
            "outage that shortened it would kill a build; an outage that lengthened it would "
            "leave a runaway process sitting on the box."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_output_bytes",
        value_type=SettingType.INT,
        default=1_048_576,
        minimum=4_096,
        maximum=16_777_216,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(1_048_576,),
        origin=Origin.PROPOSED,
        origin_note="New here. environments-api caps captured stdout per command.",
        summary="How much of a command's output may be captured and handed back.",
        description=(
            "A ceiling on what Lucy will ever put in a tool result, not on what the process "
            "may print. Bytes beyond this are truncated with a count, never silently dropped.\n\n"
            "One mebibyte is the fallback because it is a typical capture cap and because "
            "raising it during an outage would spend prompt on a log dump nobody asked to "
            "keep. The operator may still clamp this down on a small box."
        ),
    ),
)
