"""How Lucy works with the person: when to ask, when to offer a view, what to mention.

Matters of temperament rather than safety. Some people hate a round trip and others hate a
wrong guess; some want a sparring partner and others want the thing done without
commentary. Each default is what the hub's authored prompt already says, and a default adds
no text, so a person who never opens settings is sent the prompt they always were.

None of these is a floor. Whether something destructive or outward asks first is
``approval_policy`` and the permission gate, whatever these say; the hub's prompt says so in
the same sentence that states the choice.
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
        key="ambiguity",
        scope=SettingScope.PROFILE,
        value_type=SettingType.ENUM,
        default="assume_and_say",
        choices=("assume_and_say", "ask_first"),
        agent_writable=AgentAccess.WITH_APPROVAL,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("assume_and_say", "ask_first"),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub reads it into TurnPolicy.manner. With `ask_first`, the prompt's "
            "`preferences` section tells the model to ask which reading was meant, in one "
            "question, before acting; the default is the authored rule and adds no text."
        ),
        summary="Whether Lucy guesses or asks when a request could mean two things.",
        description=(
            "`assume_and_say` takes the careful reading and says which in one line. "
            "`ask_first` costs a round trip and never acts on a wrong guess.\n\n"
            "Anything destructive or outward still asks either way, so both values are safe "
            "to land on during an outage."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="opinions",
        scope=SettingScope.PROFILE,
        value_type=SettingType.ENUM,
        default="when_they_matter",
        choices=("when_they_matter", "only_when_asked"),
        agent_writable=AgentAccess.WITH_APPROVAL,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("when_they_matter", "only_when_asked"),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub reads it into TurnPolicy.manner. With `only_when_asked`, the "
            "`preferences` section says to give an opinion only when asked; the default is "
            "the authored rule and adds no text."
        ),
        summary="Whether Lucy offers its own view or keeps it until asked.",
        description=(
            "`when_they_matter` gives a view once, where it would change the decision. "
            "`only_when_asked` does the thing without commentary."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="announce_memory_writes",
        scope=SettingScope.PROFILE,
        value_type=SettingType.BOOL,
        default=True,
        agent_writable=AgentAccess.WITH_APPROVAL,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(True,),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub reads it into TurnPolicy.manner; only a literal false turns it off. Off, "
            "the `preferences` section says not to mention a kept note unless asked."
        ),
        summary="Whether Lucy mentions it when it keeps something about you.",
        description=(
            "Off keeps quietly. What is kept is still yours to read, correct and delete, and "
            "`memory_write_policy` still decides whether anything may be kept at all.\n\n"
            "On is the transparent side and is what an outage lands on: being told about a "
            "note you did not need to hear about costs a clause, and not being told about one "
            "you would have objected to is the failure."
        ),
    ),
)
