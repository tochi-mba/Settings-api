"""The ports and their adapters agree, and the value objects are frozen.

Signatures are compared parameter by parameter: an adapter that drifted from its port
would still pass ``isinstance`` (a runtime-checkable Protocol checks names only) and would
fail at the first call nobody wrote a test for.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from settings_api.core.clock import Clock, SystemClock
from settings_api.events.log import EventLog
from settings_api.events.sql_log import SqlEventLog
from settings_api.settings.sql_store import SqlSettingsStore
from settings_api.settings.store import AccountState, Applied, Change, SettingsStore, StoredSetting
from settings_api.storage.database import Database
from tests.fakes.clock import EPOCH, FakeClock


def public_methods(kind: type) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in inspect.getmembers(kind, callable)
        if not name.startswith("_")
    }


@pytest.mark.parametrize(
    ("port", "adapter"),
    [
        (EventLog, SqlEventLog),
        (SettingsStore, SqlSettingsStore),
        (Clock, SystemClock),
        (Clock, FakeClock),
    ],
)
def test_every_port_method_exists_on_the_adapter_with_the_same_parameters(
    port: type, adapter: type
) -> None:
    for name, expected in public_methods(port).items():
        actual = inspect.signature(getattr(adapter, name))
        assert [p.name for p in actual.parameters.values()] == [
            p.name for p in expected.parameters.values()
        ], name
        assert [p.kind for p in actual.parameters.values()] == [
            p.kind for p in expected.parameters.values()
        ], name


def test_the_adapters_satisfy_their_ports(database: Database) -> None:
    events = SqlEventLog(database=database)
    assert isinstance(events, EventLog)
    assert isinstance(SqlSettingsStore(database=database, events=events), SettingsStore)
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FakeClock(), Clock)


@pytest.mark.parametrize(
    "value",
    [
        StoredSetting(namespace="a", key="b", value=1, set_at=EPOCH, set_by="x"),
        AccountState(exists=False, revision=0, rows={}),
        Change(namespace="a", key="b", value=1),
        Applied(revision=1, changed=()),
    ],
    ids=type,
)
def test_the_value_objects_are_frozen(value: object) -> None:
    # An existing field, on purpose: assigning a NON-field on a frozen slots dataclass
    # trips a CPython 3.11 quirk (the generated __setattr__ calls super() with the
    # pre-slots class) and raises TypeError rather than the FrozenInstanceError under test.
    first = dataclasses.fields(value)[0].name  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(value, first, "changed")
