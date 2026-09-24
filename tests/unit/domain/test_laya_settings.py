"""The decision controls are real account settings with conservative outage defaults."""

from settings_api.domain.catalogue.lucy import SETTINGS
from settings_api.domain.types import OnUnavailable


def test_decision_controls_are_registered_and_bounded() -> None:
    entries = {entry.key: entry for entry in SETTINGS}
    expected = {
        "decisions": False,
        "decision_shadow_mode": True,
        "decision_capabilities": True,
        "decision_memory": True,
        "decision_recovery": False,
        "decision_claims": True,
        "decision_timeout_ms": 1000,
        "decision_max_per_turn": 8,
    }
    for key, default in expected.items():
        entry = entries[key]
        assert entry.default == default
        assert entry.on_unavailable is OnUnavailable.USE_DEFAULT
        entry.check()
    assert entries["decision_timeout_ms"].maximum == 5000
    assert entries["decision_max_per_turn"].maximum == 32
