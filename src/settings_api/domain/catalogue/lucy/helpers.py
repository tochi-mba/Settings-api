"""How many assistants Lucy may start beneath itself, and how much room each one gets.

A helper is a second assistant working on part of the same request, and it is paid for
twice: once for the work, and once for everything it hands back. So this is two decisions
rather than one -- how wide and how deep a fan-out may go, and how much of the parent's
window a helper may spend on its report and on talking to its siblings.

``agent_result_token_cap`` is the load-bearing one. A helper that returns forty thousand
tokens of findings is strictly worse than never having started one, because the parent
pays for all of it and had no say in what was kept.
"""

from __future__ import annotations

from settings_api.domain.catalogue.lucy.namespace import NAMESPACE
from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingType

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="agent_max_depth",
        value_type=SettingType.INT,
        default=3,
        minimum=1,
        maximum=5,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(1, 3),
        origin=Origin.EXISTING,
        origin_note="The hub enforces this as the spawn/reopen nesting cap.",
        summary="How many levels of helper Lucy may start beneath itself.",
        description=(
            "One means Lucy does everything itself. Each level multiplies what a single "
            "request can cost, which is why the ceiling is low. Three is safe to land on "
            "because three is the designed maximum: falling back here cannot widen anything "
            "past what the system allows at its most permissive."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="agent_max_concurrent",
        value_type=SettingType.INT,
        default=5,
        minimum=1,
        maximum=20,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(1, 5),
        origin=Origin.EXISTING,
        origin_note="The hub refuses spawn/reopen once this many helpers are running.",
        summary="How many helpers may work at once.",
        description=(
            "Start low. Three focused helpers usually beat five scattered ones, and every "
            "one of them is spending tokens on your behalf at the same time."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="agent_result_token_cap",
        value_type=SettingType.INT,
        default=2_000,
        minimum=200,
        maximum=20_000,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(2_000,),
        origin=Origin.EXISTING,
        origin_note="The hub caps a helper's returned summary at this many tokens.",
        summary="How much a helper may hand back when it is finished.",
        description=(
            "A helper returns a summary plus references -- workspace paths, result "
            "references, its own id -- never a transcript. This is the size of that summary, "
            "and it is the single most important number in the helper design: a helper that "
            "returns forty thousand tokens of findings is strictly worse than never having "
            "started one, because the parent pays for all of it and got no say in what was "
            "kept.\n\n"
            "Raise it when helpers keep being asked to summarise something genuinely large "
            "and the parent keeps having to ask follow-up questions. The references are "
            "always there, so a parent that wants the detail can go and read it."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="agent_wall_clock_seconds",
        value_type=SettingType.INT,
        default=600,
        minimum=10,
        maximum=7_200,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(600,),
        origin=Origin.EXISTING,
        origin_note="The hub stops a helper that exceeds this wall clock and asks for what it has.",
        summary="How long one helper may run before it is stopped and asked for what it has.",
        description=(
            "A clock per helper rather than for the whole fan-out, because the failure it "
            "catches is one helper stuck on something slow while the others finished ten "
            "minutes ago.\n\n"
            "A stopped helper is asked to hand back what it has rather than being discarded, "
            "so a short setting costs completeness rather than the work. Raise it for "
            "research that genuinely takes a while; lower it when you are waiting and would "
            "rather have a partial answer now."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="agent_message_max_chars",
        value_type=SettingType.INT,
        default=4_000,
        minimum=100,
        maximum=32_000,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(4_000,),
        origin=Origin.EXISTING,
        origin_note="The hub applies this as the per-message cap on helper mail.",
        summary="The largest message one helper may send another.",
        description=(
            "Messages between helpers are for steering -- 'stop, the file moved', 'this is "
            "taking longer than you think' -- and not for moving work around. Anything large "
            "belongs in the workspace, where it can be referenced instead of copied into "
            "everybody's context.\n\n"
            "The cap is refused at the sender, so an oversized message is a failure the "
            "sending helper can see and act on rather than something that vanishes on the "
            "way. Raising it makes it easier to pass content around by message, which is the "
            "habit the cap exists to discourage."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="agent_message_burst",
        value_type=SettingType.INT,
        default=5,
        minimum=1,
        maximum=50,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(5,),
        origin=Origin.EXISTING,
        origin_note="The hub applies this as the per-recipient burst cap on helper mail.",
        summary="How many messages one helper may send another before it has to wait.",
        description=(
            "Two models politely acknowledging each other is the default failure of a "
            "message channel rather than a hypothetical one, and this is what stops it "
            "paying for itself. The limit is per recipient and refused at the sender, so a "
            "helper finds out it is talking too much instead of discovering later that "
            "nobody was listening.\n\n"
            "Raise it for work where helpers genuinely coordinate step by step. Keep it low "
            "if a fan-out has ever turned into a conversation between the helpers about the "
            "conversation between the helpers."
        ),
    ),
)
