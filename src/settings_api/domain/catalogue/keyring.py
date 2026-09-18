"""``keyring`` -- how long a session lasts, how many there may be, and what gets said.

keyring is the auth root for the whole family and holds the accounts, the profiles and the
credentials. Three of its deployment-wide knobs are really the person's: how long a
session survives being idle, how many sessions they may hold at once, and whether they
hear about it when a new one appears.

The fourth entry here is new and is the most consequential in the namespace.
``require_reauth_for_credential_changes`` is the only setting in this catalogue that a
service must not be able to turn off through the ordinary write path, because a service
that could turn it off would be a service that could disarm the check standing between it
and the credentials it is about to ask for. It is ``owner_writable_only`` for exactly that
reason -- see ADR-0004.
"""

from __future__ import annotations

from settings_api.domain.types import OnUnavailable, Origin, SettingDef, SettingType

NAMESPACE = "keyring"

SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        namespace=NAMESPACE,
        key="session_ttl_days",
        value_type=SettingType.INT,
        default=14,
        minimum=1,
        maximum=90,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(14,),
        origin=Origin.EXISTING,
        origin_note="keyring's `session_ttl_seconds`, default 14 * 24 * 3600.",
        summary="How long a session survives without being used before it stops working.",
        description=(
            "The idle timeout. Using a session refreshes it; leaving it alone for this long "
            "ends it. keyring's separate absolute ceiling still applies, so this cannot keep "
            "a session alive indefinitely.\n\n"
            "The fallback is worth being honest about, because 14 is not the safest value -- "
            "1 is. A person who chose a one-day timeout and hits an outage gets fourteen "
            "days until this service is back. The alternative is refusing, and refusing here "
            "means nobody in the family can authenticate while settings-api is down: it "
            "would make this service a hard dependency of every login, which is a far larger "
            "failure than a bounded, temporary lengthening of one timeout."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="max_sessions",
        value_type=SettingType.INT,
        default=20,
        minimum=1,
        maximum=20,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(20,),
        origin=Origin.EXISTING,
        origin_note="keyring's `max_sessions_per_account`, default 20.",
        summary="How many sessions this person may hold at once before the oldest is dropped.",
        description=(
            "keyring trims rather than refuses: logging in on a twenty-first device ends the "
            "least recently used session instead of failing the login. Lowering this is how "
            "somebody says 'I use two devices, and a third session means something is wrong'.\n\n"
            "The maximum is the operator's cap rather than a number with meaning of its own. "
            "A person may lower it and may not raise it, which is the general rule for every "
            "cap in this catalogue."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="email_notifications",
        value_type=SettingType.BOOL,
        default=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(True,),
        origin=Origin.PROPOSED,
        origin_note=(
            "keyring has an email backend and an outbox; it has no per-account switch for "
            "whether to use them."
        ),
        summary="Whether keyring may email this person at all.",
        description=(
            "The master switch. With it off, keyring sends nothing -- no new-session notices, "
            "no security notices -- and this person finds out about their account only by "
            "looking.\n\n"
            "On is the conservative value and is therefore the one an outage lands on: a "
            "notification somebody did not want is an annoyance, and a missed notice that "
            "somebody else signed in is the failure this exists to prevent. Password resets "
            "are not covered by this and cannot be: an email a person explicitly asked for by "
            "clicking 'reset' is not a notification."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="notify_on_new_session",
        value_type=SettingType.BOOL,
        default=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(True,),
        origin=Origin.PROPOSED,
        origin_note="New here. keyring records sessions; it does not announce them.",
        summary="Whether to say so when a new session is created on this account.",
        description=(
            "A sign-in from a device this account has not used before is the earliest signal "
            "a person gets that somebody else has their password. Turning this off is "
            "reasonable for somebody who signs in from a new container every day and "
            "unreasonable for almost everybody else.\n\n"
            "Subordinate to `email_notifications`: with that off, this changes nothing."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="require_reauth_for_credential_changes",
        value_type=SettingType.BOOL,
        default=True,
        owner_writable_only=True,
        on_unavailable=OnUnavailable.REFUSE,
        origin=Origin.PROPOSED,
        origin_note="New here. keyring does not currently re-challenge for credential writes.",
        summary="Whether changing a stored credential requires proving the password again.",
        description=(
            "On, adding or replacing a credential means entering the account password again, "
            "even inside a live session. It is the control that stops a stolen session token "
            "from becoming a stolen Spotify account, a stolen mailbox and a stolen everything "
            "else in one pass.\n\n"
            "Two things make this the most protected entry in the catalogue. It is "
            "**owner-writable only**, so it can be changed only with a token the person "
            "minted for settings itself -- a service holding this person's token cannot turn "
            "it off, which matters because the service most likely to want it off is the one "
            "about to write a credential. And it **refuses rather than falling back**: if "
            "this service is unreachable, keyring must not assume the answer is the permissive "
            "one. Refusing a credential change during an outage is an inconvenience; assuming "
            "no re-authentication is needed is a door left open."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="session_absolute_ttl_days",
        value_type=SettingType.INT,
        default=90,
        minimum=1,
        maximum=365,
        operator_clampable=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(90,),
        origin=Origin.EXISTING,
        origin_note="keyring's `session_absolute_ttl_seconds`, default 90 * 24 * 3600.",
        summary="The longest a session may live, however often it is used.",
        description=(
            "`session_ttl_days` ends a session that goes unused; this ends one that never "
            "does. Without it a session used daily would live for ever, and "
            "'re-authenticate occasionally' would be a thing the system never actually "
            "asked for.\n\n"
            "keyring refuses an absolute ceiling below its idle timeout, so a person "
            "setting this below `session_ttl_days` will have that write refused by keyring "
            "rather than by this service -- the two cannot be cross-validated here, because "
            "a catalogue entry is checked on its own and the other value may not even be "
            "set. It is a real edge and the honest place to catch it is the service that "
            "owns the rule."
        ),
    ),
    SettingDef(
        namespace=NAMESPACE,
        key="notify_on_credential_change",
        value_type=SettingType.BOOL,
        default=True,
        on_unavailable=OnUnavailable.USE_DEFAULT,
        conservative_values=(True,),
        origin=Origin.PROPOSED,
        origin_note="New here. keyring records a credential write in its audit log and sends nothing.",
        summary="Whether to say so when a stored credential is added, replaced or removed.",
        description=(
            "The counterpart to `notify_on_new_session`, for the event a step further in: "
            "somebody already inside the account taking or replacing the keys to everything "
            "else. A credential write is rare and deliberate, so a notice about one is "
            "almost never noise, and the one time it is unexpected it is the only warning "
            "there will be.\n\n"
            "Subordinate to `email_notifications`: with that off, this changes nothing. On "
            "is conservative and is what an outage lands on -- an unwanted email is an "
            "annoyance, and an unnoticed credential replacement is the failure it exists "
            "to catch."
        ),
    ),
)
