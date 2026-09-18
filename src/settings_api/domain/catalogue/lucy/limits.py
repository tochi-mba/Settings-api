"""What one turn or one plan may do before it has to stop and say where it got to.

A ceiling on the length of a reply, on the tool calls a turn may make, on the steps one
plan may hold and how many of them run at once, on how long a step and a whole plan may
take, and on what a conversation may spend altogether. Somebody setting these is deciding
how much Lucy may do unattended, and they all guard against the same failure: a model that
has decided the answer is four hundred more calls.

Reaching one of them is not an error. The turn ends, or the plan is refused with its own
count, and Lucy says what it did and what it did not get to -- which is what makes them
safe to set low, and why the place to look after lowering one is the transcript rather
than an error log.
"""

from __future__ import annotations

from settings_api.domain.catalogue.lucy.namespace import NAMESPACE
from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingScope, SettingType

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="max_output_tokens_per_turn",
        value_type=SettingType.INT,
        default=8_000,
        minimum=256,
        maximum=128_000,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(8_000,),
        origin=Origin.EXISTING,
        origin_note="The hub sends this as max_output_tokens on each model request.",
        summary="How much Lucy may generate in a single reply.",
        description=(
            "The ceiling on one answer, counted in tokens the model writes. Raising it lets "
            "a long piece of work come back whole instead of in pieces; lowering it is how "
            "somebody says 'answer me, do not write me an essay', and it is the cheapest "
            "single lever on what a conversation costs.\n\n"
            "A reply that reaches the ceiling stops mid-sentence and says so, so the failure "
            "is visible and the next turn can continue. Falling back to the default during "
            "an outage spends less than anybody who raised it and truncates a reply for "
            "anybody who did -- both are recoverable by asking again, which is why this "
            "falls back rather than refusing."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_tool_calls_per_turn",
        value_type=SettingType.INT,
        default=60,
        minimum=1,
        maximum=500,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(60,),
        origin=Origin.EXISTING,
        origin_note="The hub's turn budget reads this as max_tool_calls.",
        summary="How many tool calls one turn may make before it has to stop and report.",
        description=(
            "Counted across the whole turn rather than per plan, so a turn cannot get past "
            "it by submitting six plans of ten. When it is reached the turn ends and says "
            "what it did and what it did not get to, rather than failing.\n\n"
            "Lower is a shorter leash: good for a conversation, frustrating for a piece of "
            "work that genuinely needs fifty reads. Raising it is how an unattended task "
            "gets to finish, and it is also how an unattended task gets expensive. Falling "
            "back to the designed value spends no more than the system spends today."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_turn_seconds",
        value_type=SettingType.INT,
        default=0,
        minimum=0,
        maximum=86_400,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(0,),
        origin=Origin.EXISTING,
        origin_note="The hub's turn budget reads this as max_seconds. Zero remains unlimited.",
        summary="A wall-clock ceiling on one whole turn. Zero means no ceiling.",
        description=(
            "Zero is today's behaviour: a turn runs until it is finished, out of tool calls, "
            "or out of budget. Setting a number is how somebody says 'if you have not "
            "answered me in five minutes, stop and tell me where you got to'.\n\n"
            "It is a clock, not a budget, so it catches the failure the token counters miss: "
            "a turn waiting on something slow spends almost nothing and can wait forever. "
            "Zero is conservative because it is what happens today -- an outage that landed "
            "here cannot cut a turn short that somebody was waiting on."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_llm_turns",
        value_type=SettingType.INT,
        default=12,
        minimum=1,
        maximum=100,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(12,),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub already stops its main loop at `Budget.max_iterations`; this setting "
            "makes that existing fixed limit personal."
        ),
        summary="How many model rounds one main turn may take before it stops.",
        description=(
            "One round is one model response, usually followed by a plan of tool calls. "
            "Twelve allows a substantial task while still stopping a model that keeps "
            "asking for another plan without converging. Reaching it leaves the turn "
            "resumable and records the exact count.\n\n"
            "Lower it for short conversational work. Raise it for long autonomous work, "
            "knowing that this permits more model calls as well as more time. During an "
            "outage the fixed limit the hub used before this setting remains in force."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_subagent_turns",
        value_type=SettingType.INT,
        default=8,
        minimum=1,
        maximum=50,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(8,),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub stops each child loop at this count. Depth and concurrency are separate."
        ),
        summary="How many model rounds one child agent may take before it stops.",
        description=(
            "Applied to each child independently, not shared across the fan-out. A child "
            "that reaches it returns the useful work and references it has so far rather "
            "than disappearing; the lead can then finish or deliberately continue.\n\n"
            "The default is shorter than the lead's because several children may spend it "
            "at once. Depth and concurrency are separate limits: raising this lets each "
            "existing child run longer but does not allow more children to start."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_steps_per_plan",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=20,
        minimum=1,
        maximum=100,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(20,),
        origin=Origin.EXISTING,
        origin_note="The hub's plan schema and weftai maxSteps both read this.",
        summary="How many steps one tool plan may contain before it is refused.",
        description=(
            "A plan is the batch of calls Lucy submits in one go. This is the guard against "
            "a model that has decided the answer is four hundred more calls: the plan is "
            "refused with the count, and Lucy has to narrow it and try again.\n\n"
            "Lowering it forces smaller, more considered batches and costs round trips. "
            "Raising it lets one plan do more unattended, which is the thing that goes wrong "
            "expensively. Falling back to the designed value cannot widen anything past what "
            "the system allows at its most permissive."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_parallel_steps",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=4,
        minimum=1,
        maximum=16,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(1, 4),
        origin=Origin.EXISTING,
        origin_note="The hub's weftai runtime reads this as maxParallel.",
        summary="How many steps of one plan may run at the same time.",
        description=(
            "One means every step waits for the one before it, which is slower and far "
            "easier to follow when something goes wrong. Higher finishes a wide plan sooner "
            "and interleaves the output of several tools in the transcript.\n\n"
            "This is concurrency inside a single plan, not helpers: `agent_max_concurrent` "
            "is the other one. Both values in the conservative set are safe -- one is the "
            "most cautious setting there is, and four is the designed default -- and an "
            "outage landing on either cannot run more at once than the system already does."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="step_timeout_seconds",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=10,
        minimum=1,
        maximum=600,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(10,),
        origin=Origin.EXISTING,
        origin_note="The hub's weftai runtime reads this as stepTimeoutMs.",
        summary="How long one step of a plan may take before it is abandoned.",
        description=(
            "Ten seconds is tuned for tools that answer promptly, and it is too short for a "
            "build, a large download or a search against a slow provider. Raising it is what "
            "somebody does when a real piece of work keeps being cut off; the cost is that a "
            "hung tool now holds the turn for that long before anybody finds out.\n\n"
            "A timed-out step is reported to the model as a result rather than raised as an "
            "error, so Lucy can try something else instead of the turn collapsing. Falling "
            "back during an outage gives the timeout the tools were written against."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="plan_timeout_seconds",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=60,
        minimum=1,
        maximum=3_600,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(60,),
        origin=Origin.EXISTING,
        origin_note="The hub's weftai runtime reads this as planTimeoutMs.",
        summary="How long a whole plan of steps may take before it is abandoned.",
        description=(
            "The ceiling over `step_timeout_seconds`: twenty steps that each finish just "
            "inside their own timeout still have to finish inside this one. Without it a "
            "plan of slow-but-not-hung steps runs for as long as it likes.\n\n"
            "Set it comfortably above the step timeout. Setting it below means the plan is "
            "abandoned before its first step can even time out, which is legal, confusing, "
            "and not something this service can cross-check -- a catalogue entry is checked "
            "on its own and the other value may not be set at all."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="session_token_budget",
        value_type=SettingType.INT,
        default=0,
        minimum=0,
        maximum=100_000_000,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(0,),
        origin=Origin.EXISTING,
        origin_note="The hub's turn budget uses this as max_tokens; zero means no session cap.",
        summary="A ceiling on what one conversation may spend. Zero means no ceiling.",
        description=(
            "Counted across Lucy and every helper it starts, so a fan-out cannot spend past "
            "it by splitting the work up. Lucy warns as it approaches and stops at it."
        ),
    ),
)
