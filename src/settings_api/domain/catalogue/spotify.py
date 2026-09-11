"""``spotify`` -- the motivating example.

``default_market`` is the setting that made the case for this whole service. It sits in
``spotify-api/src/spotify_api/config.py`` as a deployment-wide environment variable::

    default_market: str | None = Field(
        default=None,
        description="ISO 3166-1 alpha-2 market applied when a request omits one.",
    )

Which country's catalogue a person's track searches resolve against is a fact about the
person, deployed as if it were a fact about a machine. On a box serving one person that is
invisible; on a box serving two people in different countries it is wrong for one of them,
and the only fix available today is a second deployment.

The bounds below are the owning service's own, read out of its config rather than chosen
here. That matters for ``confirm_timeout_seconds`` in particular: spotify-api caps it at
300, so a catalogue that allowed 600 would let a person set a value the service they were
configuring would refuse.
"""

from __future__ import annotations

from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingType

NAMESPACE = "spotify"

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="default_market",
        value_type=SettingType.STR,
        default=None,
        nullable=True,
        max_chars=2,
        pattern=r"^[A-Z]{2}$",
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(None,),
        origin=Origin.EXISTING,
        origin_note="spotify-api's `default_market`, an ISO 3166-1 alpha-2 code or None.",
        summary="Which country's catalogue track searches resolve against by default.",
        description=(
            "An ISO 3166-1 alpha-2 code such as `GB` or `PT`. It decides which recordings "
            "exist, which are playable, and which of several regional releases a search "
            "resolves to -- so the same query genuinely returns different tracks in different "
            "markets.\n\n"
            "Null means 'let Spotify decide from the token', which is what spotify-api does "
            "today and is therefore the conservative fallback: an outage changes nothing "
            "rather than quietly resolving somebody's playlist against the wrong country.\n\n"
            "Upper case only. spotify-api normalises what it is given, and a catalogue that "
            "accepted `gb` would store one spelling and hand out another."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_batch_size",
        value_type=SettingType.INT,
        default=50,
        minimum=1,
        maximum=200,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(50,),
        origin=Origin.EXISTING,
        origin_note="spotify-api's `max_batch_size`, `Field(ge=1, le=200)`, default 50.",
        summary="How many tracks one resolve call may carry.",
        description=(
            "Larger batches are fewer round trips and a longer wait for the first answer; "
            "smaller ones are the reverse. Two hundred is the owning service's own ceiling, "
            "not a number chosen here.\n\n"
            "Falling back to fifty is safe because batch size is a throughput choice and not "
            "a restriction: nobody expresses a privacy preference by resolving fewer tracks "
            "at a time."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="confirm_timeout_seconds",
        value_type=SettingType.INT,
        default=15,
        minimum=5,
        maximum=300,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(15,),
        origin=Origin.EXISTING,
        origin_note=(
            "spotify-api's `confirm_timeout_seconds`, `Field(gt=0, le=300)`, default 15.0. "
            "The ceiling is the owning service's; a catalogue allowing more would let a "
            "person set a value spotify-api refuses."
        ),
        summary="How long a pending action waits for a human to confirm before giving up.",
        description=(
            "The window between 'shall I add these forty tracks' and the answer. Somebody "
            "driving an assistant by voice while doing something else wants longer than "
            "somebody sitting at a keyboard.\n\n"
            "A timeout that expires abandons the action, so falling back to fifteen seconds "
            "during an outage risks abandoning something a person would have confirmed at "
            "sixty. That fails safe: the action does not happen, and nothing was written to "
            "anybody's library that they did not agree to."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="job_retention_hours",
        value_type=SettingType.INT,
        default=1,
        minimum=0,
        maximum=24,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(1,),
        origin=Origin.EXISTING,
        origin_note="spotify-api's `job_ttl_seconds`, `Field(gt=0, le=86400)`, default 3600.",
        summary="How long a finished playlist job stays readable before it is reaped.",
        description=(
            "Overrides `common.job_retention_hours` for this namespace, and exists as its "
            "own entry for one reason: spotify-api caps its own TTL at 24 hours, and the "
            "common setting allows a week. A catalogue that offered seven days here would "
            "let a person set a value the service they were configuring refuses.\n\n"
            "Worth knowing before relying on it: spotify-api's job store is in memory, so "
            "these records are lost on restart and are not shared between replicas. This "
            "sets a ceiling on how long they live, not a floor."
        ),
    ),
)
