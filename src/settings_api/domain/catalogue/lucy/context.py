"""How much window a turn gets, and what is given up first when it runs short.

Every number here is a share of ``max_context_tokens`` or a guard over it: the reserve
kept empty for the reply, the point at which a conversation is summarised, the tails of
history and of tool results that summarising may not touch, and the three budgets the
result formatter works to. They are one group because they are one piece of arithmetic --
raising any of them takes the room from the others, and reading one without the rest says
nothing about what a turn will actually hold.

The warning threshold is here too, although it reclaims nothing. It decides whether a
person hears about the squeeze while finishing the thought is still their own choice
rather than something already done for them.
"""

from __future__ import annotations

from settings_api.domain.catalogue.lucy.namespace import NAMESPACE
from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingScope, SettingType

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="max_context_tokens",
        value_type=SettingType.INT,
        default=200_000,
        minimum=8_000,
        maximum=1_000_000,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(200_000,),
        origin=Origin.EXISTING,
        origin_note="The hub's band allocator takes this as the window; shares are fractions of it.",
        summary="How much context one turn may use before Lucy starts reclaiming room.",
        description=(
            "The bands are shares of this number, so lowering it makes every band smaller "
            "together rather than starving one of them. Set below what the model actually "
            "supports to spend less; setting it above only wastes the reserve."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="reserve_percent",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=13,
        minimum=5,
        maximum=40,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(13,),
        origin=Origin.EXISTING,
        origin_note="The hub rescales written bands from this reserve through shares_for().",
        summary="What share of the window is kept empty for the reply and one more result.",
        description=(
            "The reserve is never written to. It is the room this turn's answer needs, plus "
            "enough for one large tool result to arrive without evicting the memory that "
            "says who you are.\n\n"
            "Lowering it hands the room to history and tool results, and pays for it the "
            "first time a long answer meets a big result at the end of a full conversation. "
            "Raising it is what somebody does when replies keep being cut short. Every other "
            "band is a share of what is left, so this number moves all of them together."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="warn_at_percent",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=60,
        minimum=10,
        maximum=95,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(60,),
        origin=Origin.EXISTING,
        origin_note="The hub emits a context notice at this fullness; dropping starts at the compact trigger.",
        summary="How full the window gets before Lucy says so.",
        description=(
            "A notice rather than an action: nothing is reclaimed at this point, you are "
            "simply told the room is going. It is worth having separately from "
            "`compaction_trigger_percent` because the useful moment to hear about it is "
            "before the summarising happens, while finishing the thought or starting a fresh "
            "session is still your choice rather than something done for you.\n\n"
            "Set it above the compaction trigger and you will be warned about a compaction "
            "that has already happened. Set it very low and every long conversation nags."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="compaction_trigger_percent",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=72,
        minimum=50,
        maximum=90,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(72,),
        origin=Origin.EXISTING,
        origin_note="The hub auto-compacts a live turn once it crosses this share of the window.",
        summary="How full the window gets before a conversation is summarised.",
        description=(
            "The ceiling is 90 on purpose, and the default sits well below it. Quality is "
            "already degrading by ninety percent full, and at ninety-five there is no room "
            "left for the summarising call itself -- so waiting longer does not save a "
            "summary, it loses one."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="history_turns_kept",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=4,
        minimum=0,
        maximum=100,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(4,),
        origin=Origin.EXISTING,
        origin_note="The hub's auto-compact keeps this many newest turns verbatim.",
        summary="How many recent exchanges are never summarised away.",
        description=(
            "Compaction replaces old exchanges with a summary of them. This is the tail it "
            "may not touch: the last few turns stay verbatim, because a summary of what you "
            "said thirty seconds ago is how an assistant loses the thread of the thing it is "
            "currently doing.\n\n"
            "Raising it keeps more of the recent conversation exact and leaves less room for "
            "everything else. Zero lets compaction summarise right up to the current turn, "
            "which is the cheapest and the most likely to produce a reply that has subtly "
            "misremembered the last thing you asked for."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="tool_results_kept",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=3,
        minimum=0,
        maximum=50,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(3,),
        origin=Origin.EXISTING,
        origin_note="The hub's reclamation ladder keeps this many newest unprotected tool results.",
        summary="How many recent tool results survive reclamation in full.",
        description=(
            "Tool results are the largest band and the first one reclaimed, and the one "
            "Lucy is most likely to need again is the one that just arrived. This is the "
            "floor under that: however tight the window gets, this many of the newest "
            "results are kept whole rather than shortened to a reference.\n\n"
            "Zero means everything competes on cost alone, which reclaims the most room and "
            "occasionally throws away the file Lucy was halfway through reading. Higher "
            "protects more and leaves less for the conversation itself."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_tool_result_tokens",
        value_type=SettingType.INT,
        default=25_000,
        minimum=1_000,
        maximum=100_000,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(25_000,),
        origin=Origin.EXISTING,
        origin_note="The hub spills a tool result past this many tokens and keeps the rest by reference.",
        summary="How large one tool result may be before it is spilled instead of inlined.",
        description=(
            "A result over this goes to the workspace and the model gets a reference and a "
            "head instead. The number is a judgement about where reading the whole thing "
            "stops being worth what it costs: one forty-thousand-token result evicts the "
            "pinned memory that tells Lucy who you are.\n\n"
            "Raising it keeps more in front of the model and reclaims the room from "
            "somewhere else. Lowering it spills sooner, which costs a round trip when the "
            "answer really was in the middle of the file. Nothing is lost either way -- a "
            "spilled result is still on disk and still fetchable -- so falling back to the "
            "designed value during an outage costs a little context and no information."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="render_read_tokens",
        value_type=SettingType.INT,
        default=2_000,
        minimum=100,
        maximum=20_000,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(2_000,),
        origin=Origin.EXISTING,
        origin_note="The hub's result formatter reads this as the full-read budget.",
        summary="How much of a single read result the model is shown.",
        description=(
            "One of three budgets the result formatter works to. This is the full read: when "
            "Lucy asks for a file or a page, this is how much of it comes back before the "
            "rest becomes a reference.\n\n"
            "Raising it means fewer follow-up reads and a bigger prompt on every turn that "
            "did one. Lowering it is the right move for somebody working with large files "
            "who would rather Lucy searched than skimmed. Nothing is lost at any value -- the "
            "remainder is still fetchable -- which is why landing on the designed value "
            "during an outage costs a round trip and no information."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="render_preview_tokens",
        value_type=SettingType.INT,
        default=400,
        minimum=50,
        maximum=5_000,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(400,),
        origin=Origin.EXISTING,
        origin_note="The hub's result formatter reads this as the preview budget.",
        summary="How much of a previewed result the model is shown.",
        description=(
            "The small budget, and small on purpose. A preview is what comes back when Lucy "
            "listed forty things and needs to know *which* of them to look at properly; "
            "spending a read's worth of tokens on each of forty previews is how a listing "
            "fills the window.\n\n"
            "Raise it when Lucy keeps picking the wrong item out of a list and having to go "
            "back. Keep it low when the lists are long and the answer is usually obvious "
            "from a title."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="render_total_tokens",
        value_type=SettingType.INT,
        default=8_000,
        minimum=500,
        maximum=100_000,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(8_000,),
        origin=Origin.EXISTING,
        origin_note="The hub's result formatter reads this as the total budget for one plan.",
        summary="How much of the window one plan's results may take in total.",
        description=(
            "The ceiling over the other two: twenty steps that each render inside the read "
            "budget still have to share this. Once it is spent the remaining results are "
            "rendered as references only, and Lucy is told that is what happened.\n\n"
            "This is the number that decides whether a wide plan crowds out the conversation "
            "it was meant to serve. Raising it buys detail at the cost of history; lowering "
            "it keeps more of what you said and makes Lucy fetch more deliberately."
        ),
    ),
)
