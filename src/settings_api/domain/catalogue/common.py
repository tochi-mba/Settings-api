"""``common`` -- the answers every service needs and none of them owns.

Every service may read this namespace, which is the whole reason it exists. "What time
zone are you in" is a question every service would otherwise ask separately, and a
person would answer it once per service and get it wrong in one of them.

``default_profile`` is the one that resolves an existing disagreement rather than
proposing a new convenience, and it is worth being precise about what that disagreement
is. spotify-api and web-search-api each carry their own ``keyring_default_profile``,
defaulting to ``"personal"``; another carries a ``default_profile`` defaulting to
``"default"``. keyring itself has no notion of a default profile at all -- every one of
its routes takes the profile as a required path segment with no fallback -- so the
question "which profile do you mean when I don't say" has, today, three answers and no
owner. This gives it one.
"""

from __future__ import annotations

from settings_api.domain.types import (
    ExtraCheck,
    OnUnavailable,
    Origin,
    SettingDef,
    SettingType,
)

NAMESPACE = "common"

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="timezone",
        value_type=SettingType.STR,
        default="UTC",
        max_chars=64,
        extra_check=ExtraCheck.TIMEZONE,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("UTC",),
        origin=Origin.PROPOSED,
        origin_note="New here. No service in the family holds a per-person time zone today.",
        summary="The time zone this person lives in, as an IANA name such as Europe/Lisbon.",
        description=(
            "Used whenever a service renders a time for this person -- a job that finished "
            "'at 14:20', a session that expires 'tomorrow morning', a reminder. Set it to an "
            "IANA zone name; the value is checked against the tzdata this deployment has, so "
            "a zone that does not exist is refused at the moment it is set rather than "
            "raising deep in a render path months later.\n\n"
            "Falling back to UTC during an outage shows the right instant in the wrong zone, "
            "which is a legible error rather than a wrong one. That is why it falls back "
            "rather than refusing."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="locale",
        value_type=SettingType.STR,
        default="en-GB",
        max_chars=35,
        pattern=r"^[a-z]{2,3}(-[A-Z][a-z]{3})?(-[A-Z]{2}|-[0-9]{3})?$",
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("en-GB",),
        origin=Origin.PROPOSED,
        origin_note="New here.",
        summary="The language and region to write to this person in, as a BCP-47 tag.",
        description=(
            "Language first, then an optional script and region: `en-GB`, `pt-PT`, `zh-Hans-CN`. "
            "Services use it to choose a language and to format dates and numbers.\n\n"
            "The pattern requires the canonical casing -- lowercase language, title-case script, "
            "uppercase region -- because a tag that differs only in case is the same tag, and "
            "two spellings of the same answer is how a lookup table acquires a miss."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="units",
        value_type=SettingType.ENUM,
        default="metric",
        choices=("metric", "imperial"),
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("metric",),
        origin=Origin.PROPOSED,
        origin_note="New here.",
        summary="Whether to state distances, weights and temperatures in metric or imperial.",
        description=(
            "A presentation choice, not a storage one: services record whatever unit the "
            "source gave them and convert on the way out. Nothing is reinterpreted when this "
            "changes, so switching it cannot turn 20 degrees into 20 of something else."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="time_format",
        value_type=SettingType.ENUM,
        default="24h",
        choices=("24h", "12h"),
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("24h",),
        origin=Origin.PROPOSED,
        origin_note="New here.",
        summary="Whether to write clock times as 14:20 or as 2:20 pm.",
        description=(
            "Separate from `locale` on purpose. The two usually agree and a person is "
            "entitled to disagree with their own locale about this one -- plenty of people "
            "read en-GB and want a 12-hour clock, and a service that derived this from the "
            "locale would give them no way to say so."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="default_profile",
        value_type=SettingType.STR,
        default="personal",
        max_chars=64,
        pattern=r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$",
        on_unavailable=OnUnavailable.REFUSE,
        origin=Origin.EXISTING,
        origin_note=(
            "Replaces spotify-api's `keyring_default_profile` and web-search-api's "
            "`WSA_KEYRING_DEFAULT_PROFILE` (both 'personal'), and a third service's "
            "`default_profile` ('default'). keyring itself has no such notion."
        ),
        summary="Which keyring profile a service should use when the request does not name one.",
        description=(
            "A profile is a named set of credentials -- `personal` and `work` can hold "
            "different Spotify accounts -- and this says which one is meant by default. The "
            "pattern is keyring's own profile-name rule, so a value set here is one keyring "
            "will accept.\n\n"
            "**This one refuses rather than falling back**, and it is the clearest case for "
            "doing so in the whole catalogue. Falling back to `personal` for somebody whose "
            "default is `work` would not fail -- it would quietly act on the wrong account, "
            "adding tracks to the wrong playlist or searching with the wrong subscription. A "
            "refused request is a request the person can retry; a request served against the "
            "wrong identity is one nobody notices until later."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="redact_values_in_logs",
        value_type=SettingType.BOOL,
        default=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(True,),
        origin=Origin.PROPOSED,
        origin_note="New here.",
        summary="Whether services must keep this person's values out of their log records.",
        description=(
            "On by default and safe to land on, which is what makes it a fallback rather "
            "than a refusal: an outage cannot turn redaction off.\n\n"
            "Turning it off is a debugging affordance for somebody diagnosing their own "
            "account, not a performance one. Note the asymmetry it does not have: this "
            "service redacts its own logs unconditionally and this setting cannot change "
            "that. There is no setting anywhere that lets a person's own setting values "
            "into settings-api's log records."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="job_retention_hours",
        value_type=SettingType.INT,
        default=1,
        minimum=0,
        maximum=168,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(1,),
        origin=Origin.EXISTING,
        origin_note=(
            "The same knob in three services with three spellings: spotify-api's "
            "`job_ttl_seconds`, web-search-api's `WSA_JOB_RETENTION_SECONDS`, and a "
            "third service's own `job_ttl_seconds`. All three default to one hour."
        ),
        summary="How long the record of a finished background job stays readable.",
        description=(
            "The record, not the result: what was asked for, when, whether it worked and "
            "why not. Three services keep one of these and each had its own deployment-wide "
            "number, so this is the general answer for all of them.\n\n"
            "A namespace may override it -- `spotify.job_retention_hours` does, because "
            "spotify-api's own ceiling is 24 hours rather than a week, and a namespace "
            "whose job leaves a file on a shared disk has its own reason to. Where a "
            "namespace has its own, that one wins; where it does not, this is what the "
            "service reads. That override is the reason `common` is merged *underneath* a "
            "namespace rather than over it.\n\n"
            "One hour is the short end and is therefore the safe fallback: an outage that "
            "landed here forgets somebody's job record sooner than they asked, not later."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="currency",
        value_type=SettingType.STR,
        default=None,
        nullable=True,
        max_chars=3,
        pattern=r"^[A-Z]{3}$",
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(None,),
        origin=Origin.PROPOSED,
        origin_note="New here. Every service that quotes a cost today quotes it in whatever it was billed in.",
        summary="Which currency to state a cost in, as an ISO 4217 code such as GBP or EUR.",
        description=(
            "Anything in the family that puts a number on what something cost -- a token "
            "budget, a spend warning, a subscription -- says it in this. Null means derive "
            "it from `locale`, which is right often enough to be the default and wrong for "
            "everybody who lives in one country and is billed in another.\n\n"
            "Presentation only. Nothing is converted, recharged or recorded differently "
            "because of it, and a service that cannot convert says what it was billed in "
            "rather than guessing a rate. Null is conservative because it changes nothing: "
            "an outage that landed on it shows the same number in the same place as today."
        ),
    ),
)
