"""Which model answers, how hard it thinks, and how the answer sounds.

These are the settings somebody changes when the replies themselves are wrong: too
expensive, too flat, too slow to reason, or in a voice they did not choose. Each one
becomes an argument on the call to the provider, or the decision not to send one, which is
why the hard ceiling on working-out sits here beside the effort level it bounds rather
than with the other limits.

Nothing here is a prompt override. *How* Lucy should write is prose, it has no bounds, and
it belongs in a persona note. This group is the model, the second choice when that one
cannot be reached, the effort, the variance in the wording, and whether a picture may be
sent at all.
"""

from __future__ import annotations

from settings_api.domain.catalogue.lucy.namespace import NAMESPACE
from settings_api.domain.types import (
    AgentAccess,
    OnUnavailable,
    Origin,
    SettingDef,
    SettingScope,
    SettingType,
)

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="model",
        scope=SettingScope.PROFILE,
        value_type=SettingType.STR,
        default="anthropic:claude-opus-5",
        max_chars=128,
        pattern=r"^[a-z0-9][a-z0-9-]*:[A-Za-z0-9][A-Za-z0-9._-]*$",
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("anthropic:claude-opus-5",),
        origin=Origin.EXISTING,
        origin_note="The hub uses this as the session default when a conversation does not name one.",
        summary="Which model answers, written as provider:model.",
        description=(
            "Changing this changes how a conversation reads and what it costs. It is safe "
            "to fall back to the default during an outage: a conversation in a different "
            "model's voice is surprising, not harmful, and the alternative is refusing to "
            "answer at all."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="fallback_model",
        scope=SettingScope.PROFILE,
        value_type=SettingType.STR,
        default=None,
        nullable=True,
        max_chars=128,
        pattern=r"^[a-z0-9][a-z0-9-]*:[A-Za-z0-9][A-Za-z0-9._-]*$",
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(None,),
        origin=Origin.PROPOSED,
        origin_note="New here. The hub fails the turn when the chosen model is unavailable.",
        summary="Which model to try when the chosen one is unavailable, as provider:model.",
        description=(
            "Null means there is no second choice: if `model` cannot be reached the turn "
            "fails and says so. Naming one here means an outage at one provider becomes a "
            "reply in a different voice rather than no reply.\n\n"
            "Worth naming a model from a different provider, since the failure this covers "
            "is usually the provider rather than the model. Lucy says which model answered "
            "whenever it is not the one you chose -- a silent substitution would leave you "
            "judging one model by another's work.\n\n"
            "Null is conservative because it is the choice that does nothing: falling back "
            "to it during an outage cannot send a conversation to a provider you never named."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="thinking",
        scope=SettingScope.PROFILE,
        value_type=SettingType.ENUM,
        default="medium",
        choices=("off", "minimal", "low", "medium", "high"),
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("off", "minimal", "low", "medium"),
        origin=Origin.EXISTING,
        origin_note="The hub sends this as the provider thinking effort on each model request.",
        summary="How much working-out the model is asked to do on an ordinary turn.",
        description=(
            "`off` skips it. `high` spends tokens on hard plans. This is not a prompt "
            "override and does not change who Lucy is; a persona note still wins on voice.\n\n"
            "`high` is left out of the conservative set because it is the expensive end. An "
            "outage that landed on `medium` spends less than a person who chose `high`, never "
            "more, which is the direction a fallback should fail in."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_thinking_tokens",
        value_type=SettingType.INT,
        default=0,
        minimum=0,
        maximum=200_000,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(0,),
        origin=Origin.PROPOSED,
        origin_note="New here. The hub asks for an effort level and sets no token ceiling.",
        summary="A hard ceiling on the working-out for one turn. Zero means the effort level decides.",
        description=(
            "`thinking` says how hard to work in the provider's own vocabulary; this puts a "
            "number on it. Zero leaves the effort level to choose, which is the ordinary "
            "case and the one to leave alone.\n\n"
            "A number is for somebody who has watched `high` spend twenty thousand tokens "
            "deciding something and wants the option kept without the bill. It is a ceiling "
            "and not a target: a turn that needs less still spends less.\n\n"
            "Zero is conservative because it is what happens today. It is also the "
            "*unbounded* value, which is unusual for this catalogue and worth saying plainly: "
            "the safe fallback here costs tokens rather than anything that cannot be undone."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="temperature",
        scope=SettingScope.PROFILE,
        value_type=SettingType.INT,
        default=100,
        minimum=0,
        maximum=200,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(100,),
        agent_writable=AgentAccess.FREELY,
        origin=Origin.EXISTING,
        origin_note="The hub sends temperature as hundredths divided by 100.",
        summary="How varied the model's wording is, in hundredths: 100 means 1.0.",
        description=(
            "Hundredths rather than a decimal, because a setting value here is a whole "
            "number, a flag, a string or a short list and nothing else -- 0 is 0.0 and 200 "
            "is 2.0. The provider's own default is 1.0, which is what 100 means and what "
            "this sends.\n\n"
            "Low makes repeated questions get near-identical answers and makes the writing "
            "flatter. High makes it more varied and more likely to wander. It changes the "
            "wording rather than the reasoning: `thinking` is the knob for how hard the "
            "model works, and this is the knob for how it sounds while doing it."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="response_style",
        scope=SettingScope.PROFILE,
        value_type=SettingType.ENUM,
        default="natural",
        choices=("brief", "natural", "thorough"),
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("natural",),
        origin=Origin.EXISTING,
        origin_note="The hub appends a length instruction to the behaviour section when this is not natural.",
        summary="How much Lucy says when it is not asked to be short.",
        description=(
            "`brief` answers and stops. `thorough` shows its reasoning and its alternatives. "
            "This is a default, not a rule: asking for one or the other in a conversation "
            "still wins."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="vision_enabled",
        scope=SettingScope.PROFILE,
        value_type=SettingType.BOOL,
        default=True,
        on_unavailable=OnUnavailable.REFUSE,
        origin=Origin.PROPOSED,
        origin_note=(
            "The hub reads this into the turn policy. Image turns are not yet refused when "
            "it cannot be confirmed; only disabled_capabilities and approval_policy block "
            "the whole turn."
        ),
        summary="Whether images in a message may be sent to the model at all.",
        description=(
            "On, a screenshot or a photo you attach goes to the provider along with the "
            "text. Off, it is never sent: Lucy is told an image was attached and that it may "
            "not look at it, which is a better failure than quietly answering about text it "
            "could only half understand.\n\n"
            "**This refuses rather than falling back.** On is the default because it is how "
            "the assistant works, so landing on it during an outage would send a picture to "
            "a provider somebody had explicitly decided should not receive their pictures -- "
            "and nobody would find out, because the turn would succeed. A refused turn is "
            "one you can retry; a screenshot of a document already sent is not recoverable. "
            "The refusal only bites on a turn that actually carries an image."
        ),
    ),
)
