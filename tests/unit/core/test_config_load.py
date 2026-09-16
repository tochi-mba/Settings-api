"""Unknown environment variables, loading, and how a refusal is described.

:mod:`settings_api.core.config` is the one place a deployment's security-relevant choices
become objects, and most of it is refusals -- including the refusal pydantic-settings
would not make on its own: a ``SETTINGS_API_*`` variable that matches no setting at all.
Each of those, let through, is a deployment that looks configured and is not. Every
refusal here happens at startup, which is the only moment somebody is standing there able
to fix it, so these tests pin *that* as much as they pin the rule itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator

from settings_api.core.config import (
    ENV_NESTED_DELIMITER,
    ENV_PREFIX,
    ConfigurationError,
    ServiceConfig,
    Settings,
    UnknownSettingError,
    check_for_unknown_env_vars,
    describe_validation_error,
    known_env_names,
    load_settings,
)
from settings_api.domain.registry import NAMESPACES
from tests.unit.core._helpers import clean_env as clean_env  # noqa: PLC0414
from tests.unit.core._helpers import make_settings, service


class _Inner(BaseModel):
    depth: int = 1


class _Outer(BaseModel):
    inner: _Inner = Field(default_factory=_Inner)
    flat: str = "x"


class _Outermost(BaseModel):
    outer: _Outer = Field(default_factory=_Outer)


class TestKnownEnvNames:
    """The set the typo check is measured against."""

    def test_every_name_is_prefixed_and_upper_case(self) -> None:
        names = known_env_names()

        assert names
        assert all(name == name.upper() and name.startswith(ENV_PREFIX) for name in names)

    @pytest.mark.parametrize(
        "name",
        [
            "SETTINGS_API_DATABASE_PATH",
            "SETTINGS_API_ALLOWED_NAMESPACES",
            "SETTINGS_API_AUDIENCE_PREFIX",
            "SETTINGS_API_SERVICES",
            "SETTINGS_API_POLICY_PATH",
            "SETTINGS_API_KEYRING_JWKS_URL",
            "SETTINGS_API_MAX_VALUE_BYTES",
            "SETTINGS_API_RETIRED_RETENTION_DAYS",
        ],
    )
    def test_a_real_setting_has_its_name(self, name: str) -> None:
        assert name in known_env_names()

    def test_there_is_exactly_one_name_per_field(self) -> None:
        # The check is only as good as this set: a field that contributed no name would be
        # a setting nobody could set without tripping the unknown-variable refusal.
        assert len(known_env_names()) == len(Settings.model_fields)

    def test_a_misspelling_of_a_real_name_is_not_in_the_set(self) -> None:
        assert f"{ENV_PREFIX}ALOWED_NAMESPACES" not in known_env_names()

    def test_the_services_document_is_one_name_rather_than_a_tree(self) -> None:
        names = known_env_names()

        # `services` is a dict of models, not a nested model, so it contributes one flat
        # name. That is the whole reason it is configured as a JSON document: a nested
        # spelling could not be enumerated, and what cannot be enumerated cannot be
        # checked for typos.
        assert f"{ENV_PREFIX}SERVICES" in names
        assert not any(ENV_NESTED_DELIMITER in name for name in names)

    def test_a_nested_model_contributes_its_own_names(self) -> None:
        names = known_env_names(_Outer, "X_")

        assert names == {f"X_INNER{ENV_NESTED_DELIMITER}DEPTH", "X_FLAT"}

    def test_a_nested_model_does_not_also_claim_the_flat_name(self) -> None:
        # `X_INNER` is not a name anything reads: the delimiter spelling is how
        # pydantic-settings addresses a nested field, so offering the flat one would let a
        # variable that does nothing pass the check.
        assert "X_INNER" not in known_env_names(_Outer, "X_")

    def test_the_delimiter_repeats_all_the_way_down(self) -> None:
        names = known_env_names(_Outermost, "X_")

        expected = f"X_OUTER{ENV_NESTED_DELIMITER}INNER{ENV_NESTED_DELIMITER}DEPTH"
        assert names == {expected, f"X_OUTER{ENV_NESTED_DELIMITER}FLAT"}

    def test_another_model_may_be_walked_with_its_own_prefix(self) -> None:
        assert known_env_names(ServiceConfig, "SVC_") == {
            "SVC_TOKEN",
            "SVC_AUDIENCE_PREFIX",
            "SVC_NAMESPACES",
        }


class TestUnknownEnvVars:
    """The refusal pydantic-settings will not make for us."""

    def test_pydantic_would_ignore_a_misspelled_variable_which_is_why_this_check_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(f"{ENV_PREFIX}ALOWED_NAMESPACES", '["spotify"]')

        # The deployment believes it has compartmentalised its services. It has not: the
        # namespace list is still the default, and nothing anywhere says so. This is the
        # exact failure the check turns into a startup error.
        assert make_settings().allowed_namespaces == tuple(sorted(NAMESPACES))
        with pytest.raises(UnknownSettingError):
            check_for_unknown_env_vars()

    def test_a_misspelled_variable_is_named_in_the_refusal(self) -> None:
        with pytest.raises(UnknownSettingError) as refusal:
            check_for_unknown_env_vars({f"{ENV_PREFIX}ALOWED_NAMESPACES": "x"})

        assert f"{ENV_PREFIX}ALOWED_NAMESPACES" in str(refusal.value)

    def test_every_offender_is_named_so_one_pass_fixes_the_deployment(self) -> None:
        with pytest.raises(UnknownSettingError) as refusal:
            check_for_unknown_env_vars(
                {
                    f"{ENV_PREFIX}ZEBRA": "1",
                    f"{ENV_PREFIX}ALOWED_NAMESPACES": "2",
                    f"{ENV_PREFIX}PORT": "8003",
                }
            )

        # Sorted and complete: naming one at a time is one restart per typo, and the
        # second restart is where people stop reading the error.
        message = str(refusal.value)
        assert f"{ENV_PREFIX}ALOWED_NAMESPACES, {ENV_PREFIX}ZEBRA" in message
        assert f"{ENV_PREFIX}PORT" not in message

    @pytest.mark.parametrize(
        "name",
        [
            "PATH",
            "HOME",
            "SETTINGS_APIX",
            "OTHER_API_PORT",
            "SETTINGS_API",
            "settings_api_port",
        ],
    )
    def test_a_variable_outside_the_prefix_is_none_of_our_business(self, name: str) -> None:
        # The check owns exactly one namespace of the environment. Refusing anything else
        # would make this service the arbiter of every other program's configuration.
        check_for_unknown_env_vars({name: "value"})

    def test_an_environment_with_nothing_of_ours_in_it_is_fine(self) -> None:
        check_for_unknown_env_vars({})

    @pytest.mark.parametrize("name", sorted(known_env_names()))
    def test_every_real_setting_may_be_set(self, name: str) -> None:
        # Parametrised over the real set, so a field added without being walked by
        # `known_env_names` fails here rather than in somebody's deployment.
        check_for_unknown_env_vars({name: "value"})

    def test_the_process_environment_is_read_when_none_is_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(f"{ENV_PREFIX}NOT_A_SETTING", "1")

        with pytest.raises(UnknownSettingError) as refusal:
            check_for_unknown_env_vars()

        assert f"{ENV_PREFIX}NOT_A_SETTING" in str(refusal.value)

    def test_a_clean_process_environment_raises_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(f"{ENV_PREFIX}PORT", "8003")

        check_for_unknown_env_vars()

    def test_a_nested_spelling_of_the_services_document_is_refused(self) -> None:
        nested = f"{ENV_PREFIX}SERVICES{ENV_NESTED_DELIMITER}SPOTIFY"

        # pydantic-settings would half-understand this one: the nested delimiter is
        # configured, so `SERVICES__SPOTIFY__TOKEN` builds a partial service entry and
        # then fails somewhere less legible. One JSON document is the supported spelling.
        with pytest.raises(UnknownSettingError):
            check_for_unknown_env_vars({f"{nested}{ENV_NESTED_DELIMITER}TOKEN": "x"})

    def test_the_refusal_is_a_value_error(self) -> None:
        # Subclassing `ValueError` rather than `Exception`: a caller catching the broad
        # configuration-is-wrong case catches this too.
        assert issubclass(UnknownSettingError, ValueError)


class TestLoadSettings:
    """The startup path: check the environment, then build."""

    def test_settings_are_built_from_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(f"{ENV_PREFIX}APP_NAME", "settings-test")
        monkeypatch.setenv(f"{ENV_PREFIX}PORT", "9003")

        settings = load_settings()

        assert (settings.app_name, settings.port) == ("settings-test", 9003)

    def test_a_loaded_database_path_is_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(f"{ENV_PREFIX}DATABASE_PATH", "var/settings.db")

        assert load_settings().database_path == (tmp_path / "var" / "settings.db").resolve()

    def test_an_empty_environment_loads_the_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        settings = load_settings()

        assert settings.services == {}
        assert settings.allowed_namespaces == tuple(sorted(NAMESPACES))

    def test_a_misspelled_variable_stops_startup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(f"{ENV_PREFIX}ALOWED_NAMESPACES", '["spotify"]')

        with pytest.raises(UnknownSettingError):
            load_settings()

    def test_the_typo_check_runs_before_anything_is_built(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(f"{ENV_PREFIX}NOT_A_SETTING", "1")
        monkeypatch.setenv(f"{ENV_PREFIX}PORT", "0")

        # Both are wrong. The unknown variable is what comes back, which is how we know
        # the check happens first -- a deployment with a typo and a bad value should be
        # told about the typo rather than about a field it can see is set correctly.
        with pytest.raises(UnknownSettingError):
            load_settings()

    def test_a_bad_value_is_still_refused_once_the_names_are_known(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(f"{ENV_PREFIX}PORT", "0")

        with pytest.raises(ConfigurationError, match="port"):
            load_settings()

    def test_a_refused_service_token_is_never_echoed_by_load_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # pydantic renders the raw INPUT beside every failure, and a startup crash is
        # logged verbatim -- so without this reshaping, a too-short token would be pasted
        # into the log, the one place the SecretStr wrapper exists to keep it out of.
        monkeypatch.chdir(tmp_path)
        leaked = "short-but-real-token"
        monkeypatch.setenv(
            f"{ENV_PREFIX}SERVICES",
            json.dumps({"x": {"token": leaked, "audience_prefix": "x", "namespaces": ["spotify"]}}),
        )

        with pytest.raises(ConfigurationError) as refusal:
            load_settings()

        rendered = str(refusal.value)
        assert leaked not in rendered
        assert "services.x.token" in rendered
        assert "at least 32 characters" in rendered
        # `from None`: the original ValidationError, values included, is not chained on.
        assert refusal.value.__cause__ is None
        assert refusal.value.__suppress_context__ is True

    def test_a_lower_case_typo_is_still_a_startup_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # pydantic-settings matches names case-insensitively, so `settings_api_port=9999`
        # really sets the port -- and a typo check that only looked at upper-case names
        # would let `settings_api_alowed_namespaces` straight past, which is the exact
        # failure the check exists to prevent.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("settings_api_alowed_namespaces", '["spotify"]')

        # The message names the variable as the environment holds it: as typed on POSIX,
        # upper-cased on Windows, whose `os.environ` folds every name. Either is the typo.
        with pytest.raises(UnknownSettingError, match=r"(?i)alowed"):
            load_settings()

    def test_a_lower_case_known_name_is_not_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("settings_api_port", "9999")

        settings = load_settings()

        # Both halves agree about what a name is: the loader honoured it, so the check
        # must recognise it.
        assert settings.port == 9999


class TestDescribeValidationError:
    def test_it_renders_locations_and_messages_and_nothing_else(self) -> None:
        try:
            ServiceConfig(token=SecretStr("tiny"), audience_prefix="a.b", namespaces=("spotify",))
        except ValidationError as exc:
            rendered = describe_validation_error(exc)
        else:
            pytest.fail("expected a ValidationError")

        assert rendered.startswith("invalid configuration: ")
        assert "token: " in rendered
        assert "audience_prefix: " in rendered
        assert "tiny" not in rendered
        assert "input_value" not in rendered

    def test_a_root_level_failure_is_named_root(self) -> None:
        class Root(BaseModel):
            a: int = 1

            @model_validator(mode="after")
            def _refuse(self) -> Root:
                msg = "no"
                raise ValueError(msg)

        try:
            Root()
        except ValidationError as exc:
            rendered = describe_validation_error(exc)
        else:
            pytest.fail("expected a ValidationError")

        assert "<root>: Value error, no" in rendered


class TestBuiltThroughTheConstructor:
    """Why the suite never builds settings with ``model_copy``."""

    def test_model_copy_skips_the_validators_the_constructor_runs(self) -> None:
        settings = make_settings()

        smuggled = settings.model_copy(update={"allowed_namespaces": ("nonsense",)})

        # `model_copy(update=...)` writes the attribute without validating it, so a
        # namespace list the constructor refuses lands intact and fails far away -- as a
        # 401 on a request, rather than as a message about the configuration that caused
        # it. This is why `tests.conftest.build_settings` takes the long way round.
        assert smuggled.allowed_namespaces == ("nonsense",)
        with pytest.raises(ValidationError):
            make_settings(allowed_namespaces=("nonsense",))

    def test_model_copy_leaves_a_relative_database_path_relative(self) -> None:
        settings = make_settings()

        copied = settings.model_copy(update={"database_path": Path("var/settings.db")})

        # The resolution that makes "one file" true never runs. Two components handed
        # this object could then disagree about which file the database is.
        assert not copied.database_path.is_absolute()
        assert make_settings(database_path=Path("var/settings.db")).database_path.is_absolute()

    def test_model_copy_smuggles_a_token_past_the_length_check(self) -> None:
        config = ServiceConfig(**service())

        smuggled = config.model_copy(update={"token": SecretStr("tiny")})

        # Frozen is not validated: `model_copy` writes straight into the new object, so a
        # placeholder token that the constructor refuses outright survives here. A test
        # that built its services this way would prove nothing about a real deployment.
        assert smuggled.token.get_secret_value() == "tiny"
        with pytest.raises(ValidationError):
            ServiceConfig(**service(token="tiny"))
