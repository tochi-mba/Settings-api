"""What Lucy keeps about the person, and where else something they wrote can end up.

The narrow question first -- whether a fact about somebody is remembered without being
asked, and how much of what is already remembered is brought into one conversation -- and
then the two switches that turn the keeping off wholesale: an incognito session, which
reads no memories and writes none, and the operational log, which is the other place a
sentence somebody typed can come to rest.

memory-api stores what it is given, and its own namespace governs how it stores it. These
decide what it is given, which is why they are Lucy's settings and not memory's. They sit
together because they are the group somebody reads when the question is not "how does the
assistant behave" but "what does it know about me afterwards".
"""

from __future__ import annotations

from settings_api.domain.catalogue.lucy.namespace import NAMESPACE
from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingScope, SettingType

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="memory_write_policy",
        value_type=SettingType.ENUM,
        default="ask_first",
        choices=("never", "ask_first", "automatic"),
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=("never", "ask_first"),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub refuses notes.setFact and notes.remember when this is never, even "
            "with a grant. automatic lets those writes through in ask mode. "
            "Confirm, correct and forget are unchanged."
        ),
        summary="Whether Lucy may remember something about you without being asked.",
        description=(
            "`never` means only an explicit 'remember this' writes anything. `automatic` "
            "lets Lucy keep what it judges worth keeping, and you can read and correct all "
            "of it afterwards. Falling back to `ask_first` during an outage records less "
            "than you chose, never more, which is the direction a fallback should fail in."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="memory_retrieval_limit",
        value_type=SettingType.INT,
        default=12,
        minimum=0,
        maximum=100,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(12,),
        origin=Origin.EXISTING,
        origin_note="The hub's live memory index is capped at this many topics per turn.",
        summary="How many remembered facts Lucy may bring into one conversation.",
        description=(
            "Zero means Lucy still knows the topic index -- what it knows *about* -- but "
            "pulls nothing in until you ask. Higher numbers cost context and can bury the "
            "useful memories among the merely related ones."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="incognito",
        scope=SettingScope.PROFILE,
        value_type=SettingType.BOOL,
        default=False,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(False,),
        origin=Origin.EXISTING,
        origin_note="The hub applies this as the default on a new session when create omits it.",
        summary="Start conversations that neither read your memories nor write any.",
        description=(
            "A conversation still works: Lucy simply does not bring anything it has learned "
            "about you into it, and keeps nothing from it afterwards. The transcript is "
            "still stored, because you asked for a conversation and not for a disappearing "
            "one -- delete the session to be rid of that too."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="log_message_content",
        value_type=SettingType.BOOL,
        default=False,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(False,),
        origin=Origin.EXISTING,
        origin_note=(
            "The hub unredacts message-content fields on log lines for the duration of a "
            "turn when this is on. File contents, memory bodies and tool results stay out."
        ),
        summary="Whether what you say is written to the operational log.",
        description=(
            "Off means the log records counts, shapes and timings and never a sentence you "
            "wrote. On is for debugging your own deployment, and it puts your conversations "
            "wherever those logs go. Falling back to off records less, never more."
        ),
    ),
)
