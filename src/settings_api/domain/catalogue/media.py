"""``media`` -- how long downloaded files sit on a shared box, and at what quality.

media-tool runs headless-Chromium downloads per account and keeps the artifacts on disk
until a TTL expires. "How long do my files sit on that machine" is about as clearly the
person's question as anything in this catalogue, and today it is one number for everybody.

``preferred_quality`` is the entry that needs the most work in the owning service before
it does anything: media-tool's ``MediaQuality`` has no quality concept at all today, so
adopting it means its query type gains a field and its downloader learns to pass it on.
The generated catalogue documentation says so under that entry, because a setting that
looks like it works and does not is worse than an absent one.
"""

from __future__ import annotations

from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingType

NAMESPACE = "media"

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="artifact_retention_hours",
        value_type=SettingType.INT,
        default=1,
        minimum=0,
        maximum=720,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(1,),
        origin=Origin.EXISTING,
        origin_note="media-tool's `artifact_ttl_seconds`, default one hour.",
        summary="How long a downloaded file stays on the server before it is deleted.",
        description=(
            "Zero means delete it as soon as the job that produced it is finished with it. "
            "The maximum is thirty days, and a deployment with a small disk narrows that in "
            "policy rather than by asking people to be considerate.\n\n"
            "One hour is the conservative value here precisely because it is the *shortest* "
            "of the sensible ones: an outage that fell back to it deletes somebody's file "
            "sooner than they asked, which is recoverable by downloading again, rather than "
            "later than they asked, which is a file sitting on a shared machine for a month "
            "because a service was down."
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
        origin_note="media-tool's `job_ttl_seconds`, default one hour.",
        summary="How long the record of a finished job stays readable before it is reaped.",
        description=(
            "The job record, not the file: what was asked for, when, whether it worked and "
            "why not. Separate from `artifact_retention_hours` because the two answer "
            "different questions -- a person may want the file gone within the hour and the "
            "record of having asked for it kept for a week.\n\n"
            "The record names a URL somebody chose to download, so it is not neutral "
            "metadata; one hour is the short end, and it is the end an outage lands on."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="concurrent_jobs",
        value_type=SettingType.INT,
        default=5,
        minimum=1,
        maximum=5,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(5,),
        origin=Origin.EXISTING,
        origin_note="media-tool's `max_active_jobs_per_account`.",
        summary="How many downloads this person may have running at once.",
        description=(
            "The maximum is the operator's own per-account cap rather than a number with "
            "meaning of its own; a person may lower it and never raise it. Lowering it is "
            "how somebody keeps their own work from queueing behind itself on a busy box.\n\n"
            "Falling back to the cap during an outage means media-tool behaves exactly as it "
            "does today, which is why this is safe to land on: nothing about it is a "
            "restriction somebody expressed."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_file_gb",
        value_type=SettingType.INT,
        default=2,
        minimum=1,
        maximum=20,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(2,),
        origin=Origin.EXISTING,
        origin_note="media-tool's `max_file_bytes`, expressed in whole gigabytes.",
        summary="The largest single download this person wants attempted.",
        description=(
            "Gigabytes rather than bytes, because the byte count is a machine's unit and this "
            "is a person's choice. media-tool converts.\n\n"
            "Lowering it is a guard against an assistant misreading a link and spending an "
            "hour pulling something nobody wanted. The operator's own ceiling still applies "
            "on top, so this narrows and never widens."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="delete_artifact_after_download",
        value_type=SettingType.BOOL,
        default=False,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(False,),
        origin=Origin.PROPOSED,
        origin_note="New here. media-tool deletes on TTL only.",
        summary="Whether to delete a file the moment it has been fetched once.",
        description=(
            "For people who treat the server as a pipe rather than as storage: the artifact "
            "exists for exactly as long as it takes to collect it.\n\n"
            "Off is the conservative value even though on is the one that deletes sooner, and "
            "the reason is that this setting cannot extend anything. With it off, "
            "`artifact_retention_hours` still applies and the file still goes; with it on and "
            "a flaky connection, a half-finished download has nothing left to retry against. "
            "An outage falling back to off delays a deletion that is going to happen anyway."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="preferred_quality",
        value_type=SettingType.ENUM,
        default="best",
        choices=("best", "1080p", "720p", "480p", "audio_only"),
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("best",),
        origin=Origin.PROPOSED,
        origin_note=(
            "New here, and the furthest from working: media-tool's `MediaQuery` has no "
            "quality concept at all, so this needs a field on that type and a downloader "
            "that passes it on before it does anything."
        ),
        summary="What quality to fetch when the request does not ask for one.",
        description=(
            "`audio_only` is the interesting one: for somebody who mostly wants talks and "
            "podcasts, it is the difference between a gigabyte and thirty megabytes, and "
            "between a minute and five.\n\n"
            "`best` is conservative because it is what media-tool does today, so falling back "
            "to it during an outage changes nothing about anybody's downloads. It is also the "
            "*expensive* choice, which is worth saying plainly: the safe fallback here costs "
            "bandwidth rather than privacy, and that is the right way round."
        ),
    ),
)
