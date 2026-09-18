"""``memory`` -- what an assistant keeps about a person, and for how long.

These are the settings a person would actually want to find. Not "how is memory
implemented" but "what do you keep, how long do you keep it, and how do I get rid of it" --
which is the same set of questions user-api's namespace answers about its own records, and
they are spelled the same way here so that somebody who has answered one does not have to
recognise a differently-shaped version of the other.

Two entries carry weight beyond their size. ``erasure_grace_days`` is the window in which a
forgotten memory can still be brought back; after it, the row is deleted and its journal
pages are overwritten. And ``retrieval_trust_floor`` decides whether anything an assistant
merely *worked out* about somebody may be used without being confirmed -- the difference
between a store of what you said and a store of what was inferred from it.

Most entries are ``PROPOSED``: memory-api reads its own configuration today and has no
per-account settings seam. ``docs/catalogue.md`` repeats that per entry, so nobody ships a
setting believing it already does something.
"""

from __future__ import annotations

from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingType

NAMESPACE = "memory"

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="retrieval_limit",
        value_type=SettingType.INT,
        default=12,
        minimum=0,
        maximum=100,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(12,),
        origin=Origin.PROPOSED,
        origin_note="New here. memory-api takes a limit per request; this is the default it should use.",
        summary="How many memories one retrieval may return.",
        description=(
            "Zero does not make the assistant forget you: it still sees the topic index, "
            "which is the list of subjects it knows something about. It simply brings "
            "nothing in until asked. Higher numbers cost context and can bury a relevant "
            "memory among merely related ones."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="retrieval_trust_floor",
        value_type=SettingType.ENUM,
        default="inferred",
        choices=("stated", "observed", "inferred"),
        on_unavailable=OnUnavailable.REFUSE,
        origin=Origin.PROPOSED,
        origin_note="New here. memory-api already excludes `untrusted` until it is confirmed.",
        summary="How much an assistant may rely on, from what you said to what it worked out.",
        description=(
            "`stated` uses only what you told it. `observed` adds what it saw directly. "
            "`inferred` adds what it worked out, which is the most useful and the most "
            "likely to be wrong about you.\n\n"
            "Nothing distilled from a web page or a document is ever retrieved at any "
            "setting until somebody confirms it, and that is not configurable: a memory "
            "store is a place an attacker would like to write, and permanence is what makes "
            "it worth attacking.\n\n"
            "It refuses rather than falling back, because there is no safe guess: landing "
            "below what you chose loses memories you wanted used, and landing above uses "
            "guesses you did not agree to. Retrieving nothing for one turn is recoverable; "
            "quietly widening what an assistant relies on is not."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="write_importance_floor",
        value_type=SettingType.INT,
        default=3,
        minimum=1,
        maximum=10,
        operator_clampable=True,
        on_unavailable=OnUnavailable.REFUSE,
        origin=Origin.PROPOSED,
        origin_note="New here.",
        summary="How significant something has to be before it is worth remembering.",
        description=(
            "Low keeps almost everything and makes the store noisy. High keeps only what "
            "matters and quietly loses things you would have wanted.\n\n"
            "It refuses rather than falling back: writing nothing during an outage is "
            "recoverable, and writing down more about a person than they agreed to is not."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="recency_half_life_days",
        value_type=SettingType.INT,
        default=30,
        minimum=1,
        maximum=3650,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(30,),
        origin=Origin.EXISTING,
        origin_note="memory-api has a 30-day half-life compiled in (`store/sql.py`).",
        summary="How quickly an unused memory stops being offered first.",
        description=(
            "Ranking, not deletion: nothing is removed by getting old, it simply stops "
            "coming up before newer things. The clock runs from when a memory was last "
            "*used*, not when it was written, so a stable fact you rely on constantly stays "
            "near the top while a one-off from yesterday sinks."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="consolidation",
        value_type=SettingType.ENUM,
        default="on_session_end",
        choices=("never", "on_session_end", "continuous"),
        on_unavailable=OnUnavailable.REFUSE,
        origin=Origin.PROPOSED,
        origin_note="New here. The pass that tidies memory does not exist yet.",
        summary="When an assistant tidies what it has remembered.",
        description=(
            "Consolidation merges near-duplicates, writes better summaries for a subject, "
            "and decides that two things it learned separately are the same thing. "
            "`continuous` keeps the store tidiest and costs the most; `never` leaves it "
            "exactly as written.\n\n"
            "It never runs while you are waiting for an answer -- always in the background, "
            "because a tidy-up on the response path is a slower reply for no benefit you "
            "can see.\n\n"
            "It refuses rather than falling back. Consolidation rewrites memories, and "
            "rewriting somebody's memories without being able to read whether they wanted "
            "that is not a safe guess in either direction."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="erasure_grace_days",
        value_type=SettingType.INT,
        default=30,
        minimum=0,
        maximum=365,
        operator_clampable=True,
        on_unavailable=OnUnavailable.REFUSE,
        origin=Origin.EXISTING,
        origin_note="memory-api has MEMORY_FORGET_GRACE_SECONDS, default 30 days.",
        summary="How long a forgotten memory can still be brought back.",
        description=(
            "Forgetting hides a memory immediately. This is how long you have to change "
            "your mind before it is deleted for good, its search entry removed and the "
            "journal pages that held it overwritten.\n\n"
            "Zero erases at the next sweep. It refuses rather than falling back during an "
            "outage, because both directions are wrong: guessing shorter destroys something "
            "recoverable, and guessing longer keeps something a person asked to be rid of."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="block_char_limit",
        value_type=SettingType.INT,
        default=16_000,
        minimum=256,
        maximum=24_000,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(16_000,),
        origin=Origin.EXISTING,
        origin_note="memory-api's BlockInput defaults char_limit to 16000, ceiling 24000.",
        summary="How large the always-in-context block may be.",
        description=(
            "The block is the handful of sentences an assistant carries in every single "
            "conversation. It is small on purpose: everything in it is paid for on every "
            "turn, and a block that grows without limit is a tax on every reply."
        ),
    ),
)
