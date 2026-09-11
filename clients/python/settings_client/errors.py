"""What a consuming service has to handle.

Two exceptions, and the difference between them is the whole of the ``on_unavailable``
contract:

:class:`SettingsUnavailable` means *we do not know*. settings-api could not be reached and
this client has never successfully read this namespace, so it cannot even tell you what
the defaults are. A service seeing this has a genuine decision to make and no information
to make it with.

:class:`SettingsRefused` means *we know we must not guess*. settings-api is unreachable
and the setting you asked for is one whose default is permissive -- ``disabled_providers``
being empty means "every provider is allowed" -- so falling back to it would override a
restriction the person explicitly asked for. The operation fails instead. That is the
lesser failure and the only one that is not a broken promise.
"""

from __future__ import annotations


class SettingsClientError(Exception):
    """Base class for everything this client raises deliberately."""


class SettingsUnavailable(SettingsClientError):
    """settings-api could not be reached and nothing is cached for this namespace.

    Distinct from :class:`SettingsRefused` because there is nothing to fall back *to*: a
    client that has never had a successful response does not know the deployment's
    defaults. The fix is operational rather than a decision at the call site, which is why
    it is worth telling the two apart.
    """


class SettingsRefused(SettingsClientError):
    """This setting must not be guessed at, and settings-api is unreachable.

    Raised on **access to the key**, not when the namespace is resolved, so an operation
    that never touches the setting is not failed for it. Searching without needing
    ``disabled_providers`` should still work; searching *with* it should not proceed on a
    guess.
    """

    def __init__(self, namespace: str, key: str) -> None:
        self.namespace = namespace
        self.key = key
        super().__init__(
            f"{namespace}.{key} cannot be resolved and must not be guessed at: its default "
            "is permissive, so falling back would override a restriction this person set. "
            "Fail the operation instead."
        )


class SettingsRejected(SettingsClientError):
    """settings-api refused a write, and said why.

    Carries the status and the ``detail`` out of the problem+json body, because the
    interesting cases are all ones a caller can act on: 403 for a namespace this service
    was not granted or a setting only the person may change, 409 for a value the operator
    pinned, 422 for a value the catalogue refuses.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"settings-api refused the write ({status_code}): {detail}")
