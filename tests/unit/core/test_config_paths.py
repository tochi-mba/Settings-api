"""Paths, prefixes, namespaces, and the numeric limits.

:mod:`settings_api.core.config` is the one place a deployment's security-relevant choices
become objects, and most of it is refusals: a namespace name that is not in the catalogue,
an audience prefix that would parse as another service's family. Each of those, let
through, is a deployment that looks configured and is not: a namespace list quietly
sitting on its default, or a compartment that is not one. Every refusal here happens at
startup, which is the only moment somebody is standing there able to fix it, so these
tests pin *that* as much as they pin the rule itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsError

from settings_api.core.config import (
    ENV_PREFIX,
    LogFormat,
)
from settings_api.domain.registry import NAMESPACES
from tests.unit.core._helpers import clean_env as clean_env  # noqa: PLC0414
from tests.unit.core._helpers import make_settings


class TestDatabasePath:
    """The one file everything lives in, pinned to one place on disk."""

    def test_a_relative_database_path_becomes_absolute(self) -> None:
        settings = make_settings(database_path=Path("var/settings.db"))

        assert settings.database_path.is_absolute()

    def test_the_database_path_still_names_the_same_file_after_a_chdir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        settings = make_settings(database_path=Path("var/settings.db"))

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        # Resolution happens at load, so the path cannot come to mean a second place
        # later. Left relative, the sweeper and the erasure byte-scan could end up
        # talking about two different files and both look correct.
        assert settings.database_path == (tmp_path / "var" / "settings.db").resolve()

    def test_a_home_relative_database_path_is_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        # Windows expands `~` from USERPROFILE and ignores HOME; POSIX does the reverse.
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        settings = make_settings(database_path=Path("~/settings.db"))

        # A literal `~` directory is what an un-expanded path would create, next to
        # whatever the process happened to be started in.
        assert settings.database_path == (tmp_path / "settings.db").resolve()

    def test_an_absolute_database_path_is_left_where_it_points(self, tmp_path: Path) -> None:
        settings = make_settings(database_path=tmp_path / "settings.db")

        assert settings.database_path == (tmp_path / "settings.db").resolve()

    def test_dot_segments_are_normalised_away(self, tmp_path: Path) -> None:
        settings = make_settings(database_path=tmp_path / "var" / ".." / "settings.db")

        assert settings.database_path == (tmp_path / "settings.db").resolve()

    def test_a_database_path_from_the_environment_arrives_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(f"{ENV_PREFIX}DATABASE_PATH", "var/settings.db")

        settings = make_settings()

        # The validator runs on the environment value too: a deployment setting a relative
        # path in a unit file gets the same guarantee as one passing it in code.
        assert settings.database_path == (tmp_path / "var" / "settings.db").resolve()

    def test_the_default_is_relative_until_a_settings_object_resolves_it(self) -> None:
        # The field default is `var/settings.db`; what a caller ever sees is the resolved
        # form, because the validator runs on it the moment settings are built.
        settings = make_settings()

        assert settings.database_path == (Path.cwd() / "var" / "settings.db").resolve()


class TestPolicyPath:
    """The operator policy file: absent by default, and resolved when it is not."""

    def test_no_policy_file_means_the_catalogue_stands_as_written(self) -> None:
        settings = make_settings()

        assert settings.policy_path is None

    def test_an_explicitly_absent_policy_path_stays_absent(self) -> None:
        # `None` goes through the same validator as a path; it must come out the other
        # side as `None` rather than as the resolved current directory.
        settings = make_settings(policy_path=None)

        assert settings.policy_path is None

    def test_a_relative_policy_path_becomes_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        settings = make_settings(policy_path=Path("policy.json"))

        assert settings.policy_path == (tmp_path / "policy.json").resolve()

    def test_a_home_relative_policy_path_is_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        # Windows expands `~` from USERPROFILE and ignores HOME; POSIX does the reverse.
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        settings = make_settings(policy_path=Path("~/policy.json"))

        assert settings.policy_path == (tmp_path / "policy.json").resolve()

    def test_a_policy_path_from_the_environment_is_resolved_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(f"{ENV_PREFIX}POLICY_PATH", "policy.json")

        settings = make_settings()

        assert settings.policy_path == (tmp_path / "policy.json").resolve()


class TestAudiencePrefix:
    """The audience family the person-facing surface answers to."""

    def test_the_default_family_is_settings(self) -> None:
        assert make_settings().audience_prefix == "settings"

    @pytest.mark.parametrize("prefix", ["settings", "downstream-tool", "s", "settings_api"])
    def test_a_prefix_with_no_dot_is_accepted(self, prefix: str) -> None:
        assert make_settings(audience_prefix=prefix).audience_prefix == prefix

    @pytest.mark.parametrize("prefix", ["", ".", "settings.search", "a.b.c", "settings."])
    def test_an_empty_or_dotted_prefix_is_refused(self, prefix: str) -> None:
        # An audience is `{prefix}` or `{prefix}.{scope}`. A prefix carrying its own dot
        # would make `settings.search` parse as the whole family rather than as one
        # namespace's grant -- so every narrowed token would read everything.
        with pytest.raises(ValidationError) as refusal:
            make_settings(audience_prefix=prefix)

        assert "must be non-empty, trimmed, and contain no dot" in str(refusal.value)

    def test_the_refusal_quotes_the_prefix_it_refused(self) -> None:
        with pytest.raises(ValidationError) as refusal:
            make_settings(audience_prefix="settings.search")

        # Naming the offending value is what turns a startup crash into a one-line fix.
        assert "'settings.search'" in str(refusal.value)


class TestAllowedNamespaces:
    """Every namespace an audience may name, as a closed set rather than an open one."""

    def test_every_catalogue_namespace_is_allowed_by_default(self) -> None:
        assert make_settings().allowed_namespaces == tuple(sorted(NAMESPACES))

    @pytest.mark.parametrize("namespace", sorted(NAMESPACES))
    def test_each_catalogue_namespace_may_be_allowed_on_its_own(self, namespace: str) -> None:
        settings = make_settings(allowed_namespaces=(namespace,))

        assert settings.allowed_namespaces == (namespace,)

    def test_a_subset_keeps_the_order_it_was_given(self) -> None:
        settings = make_settings(allowed_namespaces=("user", "common"))

        assert settings.allowed_namespaces == ("user", "common")

    @pytest.mark.parametrize(
        "namespace", ["spotifyy", "SPOTIFY", "", "common.timezone", "downstream-tool", "settings"]
    )
    def test_a_namespace_that_is_not_in_the_catalogue_is_refused(self, namespace: str) -> None:
        # Refused at startup, where it is a typo, rather than at request time, where it is
        # an audience that authenticates and then reads nothing.
        with pytest.raises(ValidationError) as refusal:
            make_settings(allowed_namespaces=(namespace,))

        assert "which is not a namespace" in str(refusal.value)

    def test_the_refusal_lists_what_this_build_does_have(self) -> None:
        with pytest.raises(ValidationError) as refusal:
            make_settings(allowed_namespaces=("spotifyy",))

        message = str(refusal.value)
        assert "allowed_namespaces names 'spotifyy'" in message
        # The list of real namespaces is in the message because the reader is somebody
        # halfway through a deployment who has just misspelled one of them.
        for namespace in NAMESPACES:
            assert namespace in message

    def test_a_repeated_namespace_is_refused(self) -> None:
        # A duplicate is never intentional: the set it becomes is identical either way, so
        # the only thing a duplicate can mean is that somebody meant a different name.
        with pytest.raises(ValidationError) as refusal:
            make_settings(allowed_namespaces=("spotify", "common", "spotify"))

        assert "allowed_namespaces contains a duplicate" in str(refusal.value)

    def test_an_unknown_name_is_refused_before_a_duplicate_is_noticed(self) -> None:
        with pytest.raises(ValidationError) as refusal:
            make_settings(allowed_namespaces=("nonsense", "nonsense"))

        # Both faults are present; the unknown name is the one worth naming, because
        # fixing it is what the operator has to do either way.
        assert "which is not a namespace" in str(refusal.value)

    def test_an_empty_allowed_list_is_a_startup_error(self) -> None:
        # With no namespaces allowed, `granted_namespaces` returns an empty set for the
        # bare `settings` audience, and a correctly minted token reads nothing anywhere --
        # the "silently grants nothing" outcome the field's own docstring says it exists to
        # prevent. A service grant of nothing is refused for the same reason.
        with pytest.raises(ValidationError) as refusal:
            make_settings(allowed_namespaces=())

        assert "is empty, so no token could grant anything" in str(refusal.value)

    def test_a_namespace_list_from_the_environment_is_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(f"{ENV_PREFIX}ALLOWED_NAMESPACES", '["spotify", "common"]')

        assert make_settings().allowed_namespaces == ("spotify", "common")

    def test_a_comma_separated_namespace_list_is_a_startup_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(f"{ENV_PREFIX}ALLOWED_NAMESPACES", "spotify,common")

        # Loud rather than clever. Splitting on commas here would mean a value that is
        # *nearly* JSON parsing as one namespace named `["spotify"` and being refused
        # somewhere far less legible than the source it came from.
        with pytest.raises(SettingsError):
            make_settings()

    def test_an_unknown_namespace_from_the_environment_is_refused_as_well(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(f"{ENV_PREFIX}ALLOWED_NAMESPACES", '["spotifyy"]')

        with pytest.raises(ValidationError):
            make_settings()


class TestLimits:
    """The numeric bounds, and which of them may be zero."""

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("port", 0),
            ("port", -1),
            ("max_events", 0),
            ("max_value_bytes", 0),
            ("jwks_cache_seconds", 0.0),
            ("jwks_min_refetch_seconds", -1.0),
            ("keyring_http_timeout_seconds", 0.0),
            ("sweep_interval_seconds", 0.0),
            ("cache_ttl_seconds", -1),
            ("retired_retention_days", -1),
        ],
    )
    def test_a_limit_outside_its_range_is_refused(self, field: str, value: float) -> None:
        with pytest.raises(ValidationError):
            make_settings(**{field: value})

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("port", 1),
            ("max_events", 1),
            ("max_value_bytes", 1),
            ("jwks_cache_seconds", 0.5),
            # Zero is meaningful for both of these and forbidden for the others: "do not
            # ask the caller to cache" and "destroy retired rows at the next sweep".
            ("cache_ttl_seconds", 0),
            ("retired_retention_days", 0),
        ],
    )
    def test_a_limit_inside_its_range_is_kept(self, field: str, value: float) -> None:
        settings = make_settings(**{field: value})

        assert getattr(settings, field) == value

    def test_the_defaults_are_the_documented_ones(self) -> None:
        settings = make_settings()

        assert (settings.port, settings.max_events, settings.max_value_bytes) == (
            8003,
            2_000,
            4_096,
        )
        assert settings.retired_retention_days == 90

    def test_the_service_listens_on_loopback_unless_told_otherwise(self) -> None:
        # This service belongs behind a TLS-terminating proxy; a default of 0.0.0.0 would
        # put an unencrypted settings API on the network of anybody who forgot to set it.
        assert make_settings().host == "127.0.0.1"


class TestLogFormat:
    """How log records are rendered, as a closed set of two."""

    @pytest.mark.parametrize(
        ("value", "expected"), [("json", LogFormat.JSON), ("console", LogFormat.CONSOLE)]
    )
    def test_a_known_format_is_parsed(self, value: str, expected: LogFormat) -> None:
        assert make_settings(log_format=value).log_format is expected

    def test_an_unknown_format_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(log_format="xml")

    def test_the_default_is_machine_readable(self) -> None:
        assert make_settings().log_format is LogFormat.JSON

    def test_a_log_format_is_its_own_string(self) -> None:
        # A `StrEnum`, so the value can be handed to a renderer without unwrapping and a
        # configuration dump reads as `"json"` rather than as `"LogFormat.JSON"`.
        assert LogFormat.JSON.value == "json"
        assert f"{LogFormat.JSON}" == "json"
