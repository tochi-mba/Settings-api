"""What happens while another service is slow, and when Lucy stops waiting for it.

Three numbers with one shape between them: how long a single call to a sibling may take,
how many times a failed one is tried again, and the clock over the retrying as a whole.
Whichever of the last two runs out first ends it, and the failure is reported rather than
hidden.

They belong together because they trade against one sensation -- how long somebody watches
a spinner before being told the truth. Generous values hide a sibling's bad minute; mean
ones surface every hiccup as a failed turn. Neither is wrong, and that is the argument for
the numbers being a person's rather than a deployment's: somebody on a slow link and
somebody on the same machine as the services want different answers.
"""

from __future__ import annotations

from settings_api.domain.catalogue.lucy.namespace import NAMESPACE
from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingType

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="retry_attempts",
        value_type=SettingType.INT,
        default=2,
        minimum=0,
        maximum=10,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(2,),
        origin=Origin.PROPOSED,
        origin_note="New here. The hub retries a failed sibling call with a fixed policy.",
        summary="How many times a failed call to another service is tried again.",
        description=(
            "Only for failures where trying again is meaningful -- a timeout, a rate limit, "
            "a connection that dropped. A refusal is never retried, because asking a second "
            "time for something you were told you may not have is how a permission prompt "
            "becomes a permission loop.\n\n"
            "Zero surfaces every hiccup to you. Higher hides them at the cost of a turn that "
            "sits there for longer before admitting the service is down. `retry_max_seconds` "
            "is the other half: whichever runs out first ends it."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="retry_max_seconds",
        value_type=SettingType.INT,
        default=30,
        minimum=1,
        maximum=600,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(30,),
        origin=Origin.PROPOSED,
        origin_note="New here. The hub's retry backoff has no per-account ceiling.",
        summary="How long retrying one call may go on before it gives up.",
        description=(
            "The backoff between attempts grows, so a generous `retry_attempts` can add up "
            "to minutes of silence. This is the clock over the count: whichever is reached "
            "first ends the retrying and the failure is reported.\n\n"
            "Worth setting against how long you are prepared to watch a spinner. A service "
            "that has been unreachable for thirty seconds is usually going to be unreachable "
            "for thirty more, and hearing so is more useful than waiting."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="downstream_timeout_seconds",
        value_type=SettingType.INT,
        default=10,
        minimum=1,
        maximum=300,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(10,),
        origin=Origin.EXISTING,
        origin_note="The hub's `http_timeout_seconds`, default 10, applied to every sibling call.",
        summary="How long one call to another service may take before it is abandoned.",
        description=(
            "Every request the hub makes to a sibling -- memory, persona, keyring, the rest "
            "-- is bounded by this. It is deployment-wide today, which is the wrong shape: a "
            "person on a slow link and a person on the same machine as the services want "
            "different numbers.\n\n"
            "Short means a slow service is treated as a down service, which is usually the "
            "right call and occasionally throws away an answer that was coming. Long means a "
            "sibling having a bad day holds up your turn. This is the per-call timeout; "
            "`step_timeout_seconds` bounds a tool step, which may be several calls."
        ),
    ),
)
