"""The composition root: what it wires, what it refuses, and the sweeper's first pass.

The one behaviour here that a reader would not guess is the order of the sweep loop:
sweep, then sleep. The other order leaves rows whose window expired while the service was
stopped sitting there for a further whole interval after it comes back, because the first
thing the loop did was sleep for an hour.
"""

from __future__ import annotations

import asyncio
import gc
import json
import threading
import warnings
from pathlib import Path

import pytest

from settings_api.core.container import Container
from settings_api.domain.policy import NO_POLICY, PolicyError
from settings_api.settings.sweeper import RetiredSweeper
from tests.conftest import build_settings
from tests.fakes.clock import FakeClock


class TestBuild:
    async def test_it_migrates_the_database_and_loads_no_policy_by_default(
        self, tmp_path: Path
    ) -> None:
        container = Container.build(build_settings(tmp_path), clock=FakeClock())
        try:
            rows = await container.database.fetch_all("SELECT version FROM schema_version")
            assert [row["version"] for row in rows] == [1, 2]
            assert container.policy is NO_POLICY
            assert container.services.configured == ("downstream-tool", "spotify-api", "user-api")
        finally:
            await container.aclose()

    async def test_it_loads_a_configured_policy(self, tmp_path: Path) -> None:
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"user": {"log_values": {"pin": False}}}))
        container = Container.build(build_settings(tmp_path, policy_path=path), clock=FakeClock())
        try:
            assert set(container.policy.pins) == {"user.log_values"}
        finally:
            await container.aclose()

    async def test_a_bad_policy_is_a_startup_error(self, tmp_path: Path) -> None:
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"user": {"grace_days": {"maximum": 9999}}}))
        with pytest.raises(PolicyError, match="a policy may only narrow"):
            Container.build(build_settings(tmp_path, policy_path=path), clock=FakeClock())

    async def test_it_uses_the_system_clock_when_none_is_given(self, tmp_path: Path) -> None:
        container = Container.build(build_settings(tmp_path))
        try:
            assert container.clock.now().tzinfo is not None
            assert container.uptime_seconds >= 0
        finally:
            await container.aclose()

    async def test_uptime_is_measured_on_the_injected_clock(self, tmp_path: Path) -> None:
        clock = FakeClock()
        container = Container.build(build_settings(tmp_path), clock=clock)
        try:
            clock.advance(42)
            assert container.uptime_seconds == 42
        finally:
            await container.aclose()

    async def test_building_reaches_nothing_over_the_network(self, tmp_path: Path) -> None:
        # The JWKS client exists and has fetched nothing; the readiness check is what
        # fetches, and only when asked.
        container = Container.build(build_settings(tmp_path), clock=FakeClock())
        try:
            assert container.jwks._keys is None
        finally:
            await container.aclose()


class Exploding:
    """A sweeper that always fails. Hand-written; the Protocol is one method."""

    def __init__(self) -> None:
        self.calls = 0

    async def sweep_once(self) -> int:
        self.calls += 1
        msg = "the disk fell off"
        raise RuntimeError(msg)


class Counting:
    def __init__(self) -> None:
        self.calls = 0

    async def sweep_once(self) -> int:
        self.calls += 1
        return 0


class TestTheSweeper:
    async def test_aclose_is_safe_without_a_running_sweeper(self, tmp_path: Path) -> None:
        container = Container.build(build_settings(tmp_path), clock=FakeClock())
        await container.aclose()

    async def test_the_first_pass_runs_before_the_first_sleep(self, tmp_path: Path) -> None:
        container = Container.build(
            build_settings(tmp_path, sweep_interval_seconds=3600.0), clock=FakeClock()
        )
        counting = Counting()
        container.sweeper = counting  # type: ignore[assignment]
        try:
            container.start_sweeper()
            # Yield once. If the loop slept first, this would still be zero and the rows
            # whose window expired while the service was down would wait another hour.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert counting.calls == 1
        finally:
            await container.aclose()

    async def test_a_failing_sweep_does_not_kill_the_sweeper(self, tmp_path: Path) -> None:
        container = Container.build(
            build_settings(tmp_path, sweep_interval_seconds=0.001), clock=FakeClock()
        )
        exploding = Exploding()
        container.sweeper = exploding  # type: ignore[assignment]
        try:
            container.start_sweeper()
            for _ in range(20):
                await asyncio.sleep(0.001)
            # It kept trying. The first transient error must not silently stop all
            # purging with nothing saying so.
            assert exploding.calls >= 2
            assert container._sweeper_task is not None
            assert not container._sweeper_task.done()
        finally:
            await container.aclose()
        assert container._sweeper_task is None

    async def test_aclose_cancels_a_running_sweeper_cleanly(self, tmp_path: Path) -> None:
        container = Container.build(build_settings(tmp_path), clock=FakeClock())
        container.start_sweeper()
        await asyncio.sleep(0)
        task = container._sweeper_task
        assert task is not None
        await container.aclose()
        assert task.cancelled()
        assert container._sweeper_task is None

    def test_the_real_sweeper_is_wired(self, tmp_path: Path) -> None:
        container = Container.build(build_settings(tmp_path), clock=FakeClock())
        try:
            assert isinstance(container.sweeper, RetiredSweeper)
        finally:
            asyncio.run(container.aclose())


class TestARefusedBuildLeaksNothing:
    """A build that fails after opening the database must close it.

    Migration and policy loading both run after the open, and a policy file that says
    something the catalogue refuses is *meant* to raise. That refusal used to drop the
    open database -- file handle, WAL sidecars and worker thread -- which Python 3.13
    reports as a ResourceWarning at collection and this suite treats as a failure.
    """

    def test_a_migration_that_fails_closes_the_database_it_was_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*_: object, **__: object) -> None:
            msg = "the schema is not what this build expects"
            raise RuntimeError(msg)

        monkeypatch.setattr("settings_api.core.container.migrate", refuse)
        threads_before = threading.active_count()

        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            with pytest.raises(RuntimeError, match="schema"):
                Container.build(build_settings(tmp_path), clock=FakeClock())
            gc.collect()
        assert threading.active_count() == threads_before, "the worker thread was given back"
