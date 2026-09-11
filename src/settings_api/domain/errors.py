"""Domain error vocabulary.

These describe what went wrong in business terms. Translating them into HTTP status codes
is the API layer's job -- nothing here knows what a status code is.

Two rules shape the module, and they pull in opposite directions, so it is worth being
explicit about where the line is.

**Across accounts, nothing is distinguishable.** There is no error here that could tell
one account about another, because there is no request that could ask about another --
the account comes from a verified ``sub`` and no route accepts one. The distinction
simply cannot arise, which is a stronger property than concealing it would be.

**Within one account, a refusal says exactly what it refused.** The caller is the
person's own assistant, or a service holding that person's token. Telling it "that
namespace is not in your grant" is telling it about *itself*, and a caller that cannot
tell "refused" from "absent" retries forever. So :class:`NamespaceNotGrantedError` is
specific, and :class:`SettingPinnedError` is the most specific of the lot -- a person
whose change vanished with a 200 and no explanation is the worst outcome this service
has, which is why a pinned setting is a 409 that says who pinned it and never a silent
no-op.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every error this package raises deliberately."""


class AuthenticationError(DomainError):
    """A token was not accepted.

    Deliberately undifferentiated. Whether the signature is wrong, the audience names
    another service, the issuer is not keyring, the token expired, a required claim is
    missing, the service token matched nothing, or a service presented a user token from
    outside its own audience family -- the caller is told the same thing, because each
    distinction is an oracle that helps somebody forge the next one. The specific reason
    goes to the logs, where only the operator reads it.
    """


class KeyringUnreachableError(DomainError):
    """Keyring's public keys could not be fetched, so no token can be verified.

    Distinct from :class:`AuthenticationError` because it is *our* failure, not the
    caller's: the token may be perfectly good and we cannot tell. It is a 503 with a
    Retry-After, not a 401, so a caller retries rather than throwing its token away and
    sending a person back through a login it did not need.
    """


class UnknownNamespaceError(DomainError):
    """No namespace by that name exists in this build's catalogue.

    Distinct from :class:`NamespaceNotGrantedError` on purpose, and the distinction leaks
    nothing: the catalogue is the same in every deployment of a given build and is
    published by ``describe_settings`` to anybody with a token. "There is no such
    namespace" and "you may not read that namespace" are both facts the caller is
    entitled to, and conflating them would send a caller with a typo round the loop
    forever looking for a permission problem it does not have.
    """


class UnknownSettingError(DomainError):
    """No setting by that name exists in this namespace."""


class UnknownSettingsError(DomainError, ValueError):
    """A document names settings that do not exist.

    Raised by the document-shaped writes -- ``update_settings`` and ``import_settings`` --
    rather than the single-key ones, and a 400 rather than a 404 because the request as a
    whole is malformed rather than addressed at something absent. The message names
    ``describe_settings``, because the caller is usually a model and the fix is to go and
    read what exists. Unknown keys are **never** silently dropped: a caller that believes
    it wrote a setting it did not is worse off than one that was refused.
    """


class NamespaceNotGrantedError(DomainError):
    """The caller asked for a namespace its token does not grant.

    Safe to be specific: this is a fact about the caller's own token, not about what
    exists. media-tool asking for ``user`` is told exactly that, because the fix -- be
    granted it, or stop asking -- is not something it can discover by retrying.
    """


class SettingNotWritableError(DomainError):
    """This setting is the person's own to change, and the caller is not the person.

    Raised for an ``owner_writable_only`` key written through ``/v1/internal``. A service
    holding somebody's token may write within its own namespace, but not these -- see
    ADR-0004 for the argument, and note that ``user.erasure_mode`` is deliberately *not*
    one of them, because marking it so would break user-api's existing route.
    """


class SettingPinnedError(DomainError):
    """Operator policy fixes this setting's value, so it cannot be changed here.

    A 409 with a body that says so, never a silent no-op. The schema response reports
    ``pinned: true`` for exactly these, so a caller can see it coming rather than
    discovering it by being refused.
    """


class InvalidSettingValueError(DomainError, ValueError):
    """A value is the wrong type, outside its bounds, or too large.

    Also a :class:`ValueError` so callers validating input with generic machinery catch it
    without importing this module. The message names the rule that failed and never
    echoes the value.
    """


class CredentialRefusedError(DomainError, ValueError):
    """The value looks like a credential, and this is not where credentials go.

    Names keyring as the right home, because the caller is a model that will otherwise
    try again with the same value somewhere else. There is no override and no setting that
    turns it off -- see :mod:`settings_api.domain.secrets` for why the detector is
    deliberately conservative.
    """


class RevisionMismatchError(DomainError):
    """The caller's ``If-Match`` names a revision this account is no longer at.

    Optimistic concurrency: two assistants editing the same person's settings should not
    silently overwrite each other, and the one that lost is told so rather than told it
    won.
    """


# There is deliberately no `LimitExceededError` here, and its absence is worth a line.
# The only per-account cap in this service is the event log, and that trims rather than
# refusing -- an append that pushed an account over the cap drops its oldest event in the
# same statement. The number of stored settings needs no cap at all: the primary key is
# (account_id, namespace, key) and every key comes from the catalogue, so an account
# cannot hold more rows than the catalogue has entries. That is a structural bound rather
# than a counted one, which is the stronger of the two.
