"""Turning a value somebody sent into a value this service will store.

Three things happen here and the order is the interesting part, because it decides what a
caller is told when more than one rule would fire:

1. **Shape**, against the catalogue entry's own declaration. Type first, then bounds.
2. **Credentials**, over every string and every string inside a list.
3. **Size**, over the serialized form, as a backstop for a type whose bounds somebody
   forgot to write.

Shape before credentials, so a caller that sent a number where a string belongs is told
that rather than told its number does not look like a secret. Credentials before size, so
a caller pasting a long API key is told what is actually wrong with it rather than told it
is too long -- which would send them back with a shorter API key.

## The JSON rules are not incidental

``json.dumps(..., allow_nan=False)`` everywhere. By default Python renders ``NaN`` and
``Infinity`` as the bare tokens ``NaN`` and ``Infinity``, which are not JSON: Python reads
them back and every other reader in the world does not. Without this a stored value would
come back fine through this service and break the moment a consuming service parsed the
response. Turning it on also makes a previously-unreachable ``except`` arm reachable,
which the coverage gate will tell you about.

Values are stored as JSON text even when they are booleans, which is what lets one column
serve every type and makes adding a sixth type something other than a migration.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from settings_api.domain.errors import CredentialRefusedError, InvalidSettingValueError
from settings_api.domain.secrets import KEYRING_ADVICE, looks_like_a_credential

if TYPE_CHECKING:
    from settings_api.domain.types import SettingDef, Value


def validate(definition: SettingDef, value: Value, *, max_bytes: int) -> Value:
    """Check one value against its catalogue entry and return it.

    Returns the value rather than ``None`` so a caller cannot validate and then store a
    separately-derived copy, which is two paths that can disagree after somebody edits
    one.

    Raises:
        InvalidSettingValueError: wrong type, outside its bounds, or too large. The
            message names the rule that failed and never echoes the value, because it
            ends up in a 422 body and in a log line.
        CredentialRefusedError: it looks like a credential. Names keyring and never
            echoes the matched text.
    """
    checked = _check_shape(definition, value)
    refuse_credentials(definition, checked)
    _check_size(definition, checked, max_bytes=max_bytes)
    return checked


def _check_shape(definition: SettingDef, value: Value) -> Value:
    """Run the catalogue entry's own rules, in this package's error vocabulary.

    :meth:`~settings_api.domain.types.SettingDef.validate` raises a plain ``ValueError``,
    because an import-linter contract keeps the catalogue one hop from this module's
    errors. This is the one place that translation happens.
    """
    try:
        return definition.validate(value)
    except ValueError as exc:
        raise InvalidSettingValueError(str(exc)) from exc


def refuse_credentials(definition: SettingDef, value: Value) -> None:
    """Refuse a value that looks like a credential, at every string it contains.

    Lists are walked rather than rendered and scanned as one string, so an item that is a
    credential is caught whether it is the first or the sixtieth -- and so the reason
    names the setting rather than the position, which would be a way of asking the service
    to confirm where in the list the secret is.

    Raises:
        CredentialRefusedError: naming the kind of credential and keyring, never the text.
    """
    for text in _strings_in(value):
        reason = looks_like_a_credential(text)
        if reason is not None:
            msg = f"{definition.qualified}: {reason}; {KEYRING_ADVICE}"
            raise CredentialRefusedError(msg)


def _strings_in(value: Value) -> list[str]:
    """Every string a value contains. One element for a string, all items for a list."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    return []


def _check_size(definition: SettingDef, value: Value, *, max_bytes: int) -> None:
    """Refuse a value whose serialized form is larger than the deployment allows."""
    size = len(encode(definition, value).encode())
    if size > max_bytes:
        msg = (
            f"{definition.qualified} may be at most {max_bytes} bytes serialized; "
            f"this one is {size}"
        )
        raise InvalidSettingValueError(msg)


def encode(definition: SettingDef, value: Value) -> str:
    """Render a value as the JSON text a row holds.

    Raises:
        InvalidSettingValueError: the value is not JSON-serializable, or is a float that
            JSON has no spelling for. Neither can arise from a value that passed
            :func:`_check_shape` today -- the five types are all serializable and none of
            them is a float -- but the arm is here rather than absent because the whole
            point of ``allow_nan=False`` is that Python's default is *wrong*, and a sixth
            type added later would find this guard already in place.
    """
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        msg = f"{definition.qualified} must be JSON-serializable"
        raise InvalidSettingValueError(msg) from exc


def decode(raw: str) -> Value:
    """Parse the JSON text a row holds back into a value.

    Annotated rather than returned straight out of :func:`json.loads`: what comes back is
    ``Any``, and the annotation is where the shape the column was written with is
    asserted. A row this service wrote always round-trips; a row edited by hand in
    ``sqlite3`` might not, and that is a broken database rather than a bad request.
    """
    parsed: Value = json.loads(raw)
    return parsed
