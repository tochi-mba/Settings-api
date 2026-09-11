"""What the configuration refuses, and where.

:mod:`settings_api.core.config` is the one place a deployment's security-relevant choices
become objects, and most of it is refusals: a namespace name that is not in the catalogue,
an audience prefix that would parse as another service's family, a service token short
enough to guess, two services sharing one, and -- the refusal pydantic-settings would not
make on its own -- a ``SETTINGS_API_*`` variable that matches no setting at all.

Each of those, let through, is a deployment that looks configured and is not: a namespace
list quietly sitting on its default, or a compartment that is not one. Every refusal here
happens at startup, which is the only moment somebody is standing there able to fix it, so
these tests pin *that* as much as they pin the rule itself.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import SettingsError

from settings_api.core.config import (
    ENV_NESTED_DELIMITER,
    ENV_PREFIX,
    MIN_SERVICE_TOKEN_CHARS,
    ConfigurationError,
    LogFormat,
    ServiceConfig,
    Settings,
    UnknownSettingError,
    check_for_unknown_env_vars,
    describe_validation_error,
    known_env_names,
    load_settings,
)
from settings_api.domain.registry import NAMESPACES

TOKEN_A = "spotify-service-token-0123456789abcdef"
TOKEN_B = "media-service-token-0123456789abcdefghij"
"""Two tokens long enough to be accepted, and unlike each other. Their length is asserted
in :func:`test_the_sample_tokens_are_long_enough_to_be_accepted` rather than assumed, so a
change to the minimum shows up as that failure instead of as every service test failing."""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``SETTINGS_API_*`` variable that the test did not set itself.

    Settings read the process environment, so a variable left in the shell that started
    pytest would silently change what these tests are asserting about -- and the tests
    about *unknown* variables would then depend on what the operator happened to export.
    """
    for name in list(os.environ):
        if name.upper().startswith(ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)


def make_settings(**overrides: Any) -> Settings:
    """Settings through the constructor, with the ``.env`` file out of the way.

    ``_env_file=None`` because the model config names ``.env``: without it these tests
    would pass or fail depending on a file in whatever directory pytest was started from.
    """
    values: dict[str, Any] = {"_env_file": None, **overrides}
    return Settings(**values)


def service(
    *,
    token: str = TOKEN_A,
    audience_prefix: str = "spotify",
    namespaces: tuple[str, ...] = ("spotify",),
) -> dict[str, Any]:
    """One entry of the ``services`` document, as JSON would deliver it."""
    return {"token": token, "audience_prefix": audience_prefix, "namespaces": list(namespaces)}


def test_the_sample_tokens_are_long_enough_to_be_accepted() -> None:
    # If the minimum ever rises above these, every services test below would fail with a
    # message about token length rather than about the rule it was written for.
    assert len(TOKEN_A) >= MIN_SERVICE_TOKEN_CHARS
    assert len(TOKEN_B) >= MIN_SERVICE_TOKEN_CHARS
    assert TOKEN_A != TOKEN_B


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

    @pytest.mark.parametrize("prefix", ["settings", "media-tool", "s", "settings_api"])
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
        "namespace", ["spotifyy", "SPOTIFY", "", "common.timezone", "media-tool", "settings"]
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


class TestServiceAudiencePrefix:
    """``audience_prefix`` on one service: the check the internal surface rests on."""

    @pytest.mark.parametrize("prefix", ["spotify", "media-tool", "user", "a"])
    def test_a_plain_prefix_is_accepted(self, prefix: str) -> None:
        assert ServiceConfig(**service(audience_prefix=prefix)).audience_prefix == prefix

    @pytest.mark.parametrize("prefix", ["", "spotify.jobs", ".", "a.b"])
    def test_an_empty_or_dotted_service_prefix_is_refused(self, prefix: str) -> None:
        # `media-tool` must accept `media-tool` and `media-tool.jobs` and refuse
        # `media-toolkit`. A prefix containing a dot makes that parse ambiguous, and the
        # ambiguity is between two services' audience families.
        with pytest.raises(ValidationError) as refusal:
            ServiceConfig(**service(audience_prefix=prefix))

        assert "must be non-empty, trimmed, and contain no dot" in str(refusal.value)

    @pytest.mark.parametrize("prefix", [" spotify", "spotify ", " ", "\tspotify"])
    def test_a_padded_service_prefix_is_refused(self, prefix: str) -> None:
        # Whitespace survives a copy-paste out of a YAML block and matches no audience
        # keyring ever mints, so every user token that service presented would be a 401 --
        # a total outage whose cause is invisible in the configuration that caused it.
        with pytest.raises(ValidationError):
            ServiceConfig(**service(audience_prefix=prefix))

    def test_the_prefix_is_independent_of_the_service_name(self) -> None:
        settings = make_settings(
            services={"spotify-api": service(audience_prefix="spotify", namespaces=("spotify",))}
        )

        # Deliberately two strings. A deployment whose audience family happened to equal
        # its service name would pass a test that conflated them, and break the day one
        # service was renamed.
        assert settings.services["spotify-api"].audience_prefix == "spotify"


class TestServiceNamespaces:
    """What one service may see: the blast radius of one compromised service token."""

    def test_a_grant_becomes_a_tuple_whatever_json_delivered(self) -> None:
        config = ServiceConfig(**service(namespaces=("spotify", "media")))

        assert config.namespaces == ("spotify", "media")

    def test_a_service_granted_nothing_is_refused_rather_than_left_reading_common(self) -> None:
        # `common` is added to every grant at request time, so an empty list is not
        # "no access" -- it is a service that reads common and nothing else, which is
        # never what somebody meant to write.
        with pytest.raises(ValidationError) as refusal:
            ServiceConfig(**service(namespaces=()))

        assert "omit it instead" in str(refusal.value)

    @pytest.mark.parametrize("namespace", ["spotifyy", "SPOTIFY", "", "settings"])
    def test_an_unknown_namespace_in_a_grant_is_refused_at_startup(self, namespace: str) -> None:
        # The worse half of the same typo: the service would authenticate, ask for the
        # namespace it believes it was granted, and be told 403 by a deployment that
        # thought it had granted it.
        with pytest.raises(ValidationError) as refusal:
            ServiceConfig(**service(namespaces=(namespace,)))

        assert "namespaces names" in str(refusal.value)

    def test_a_repeated_namespace_in_a_grant_is_refused(self) -> None:
        with pytest.raises(ValidationError) as refusal:
            ServiceConfig(**service(namespaces=("spotify", "spotify")))

        assert "namespaces contains a duplicate" in str(refusal.value)

    @pytest.mark.parametrize("namespace", sorted(NAMESPACES))
    def test_every_catalogue_namespace_may_be_granted(self, namespace: str) -> None:
        config = ServiceConfig(**service(namespaces=(namespace,)))

        assert config.namespaces == (namespace,)


class TestServiceToken:
    """The whole proof that a caller on ``/v1/internal`` is a service at all."""

    @pytest.mark.parametrize("length", [0, 1, MIN_SERVICE_TOKEN_CHARS - 1])
    def test_a_token_shorter_than_the_minimum_is_refused(self, length: int) -> None:
        # Checked rather than trusted: a deployment that pasted a placeholder would look
        # exactly like a working one until somebody guessed the placeholder.
        with pytest.raises(ValidationError) as refusal:
            ServiceConfig(**service(token="x" * length))

        assert f"at least {MIN_SERVICE_TOKEN_CHARS} characters" in str(refusal.value)

    @pytest.mark.parametrize("length", [MIN_SERVICE_TOKEN_CHARS, MIN_SERVICE_TOKEN_CHARS + 1, 128])
    def test_a_token_of_at_least_the_minimum_is_accepted(self, length: int) -> None:
        config = ServiceConfig(**service(token="x" * length))

        assert config.token.get_secret_value() == "x" * length

    def test_the_token_does_not_appear_when_the_config_is_rendered(self) -> None:
        config = ServiceConfig(**service())

        # A `SecretStr` so a configuration dump, a traceback frame or a log line carrying
        # the model renders stars. The value itself is one explicit call away.
        assert TOKEN_A not in repr(config)
        assert TOKEN_A not in str(config)
        assert config.token.get_secret_value() == TOKEN_A


class TestServiceConfigShape:
    """A service entry is exactly three fields, and it does not change afterwards."""

    def test_an_unrecognised_field_is_refused(self) -> None:
        # `extra="forbid"`, so a misspelled `namespace` is a startup error rather than a
        # service silently configured with no namespaces at all.
        with pytest.raises(ValidationError) as refusal:
            ServiceConfig(**{**service(), "namespace": "spotify"})

        assert "extra_forbidden" in str(refusal.value)

    def test_a_profile_is_not_a_field_a_service_entry_has(self) -> None:
        # There is no profile anywhere in this service; `extra="forbid"` is what makes
        # that true of the configuration as well as of the request bodies.
        with pytest.raises(ValidationError):
            ServiceConfig(**{**service(), "profile": "work"})

    def test_a_service_entry_cannot_be_edited_after_it_is_built(self) -> None:
        config = ServiceConfig(**service())

        with pytest.raises(ValidationError):
            config.audience_prefix = "media-tool"


class TestConfiguredServices:
    """The cross-field rules: one token each, and nothing granted that is not allowed."""

    def test_a_deployment_with_no_services_configured_is_valid(self) -> None:
        # The right state for a service nobody has been told to trust yet: a working
        # person-facing surface, and a `/v1/internal` that refuses everything.
        assert make_settings().services == {}

    def test_a_service_is_parsed_into_a_service_config(self) -> None:
        settings = make_settings(services={"spotify-api": service()})

        assert settings.services["spotify-api"].namespaces == ("spotify",)

    def test_two_services_sharing_a_token_are_refused(self) -> None:
        # Whichever name matched first would decide which *audience family* is acceptable
        # for the user token, so the weaker of the two grants would be reachable with the
        # other's token: the confused deputy, reopened from the inside.
        with pytest.raises(ValidationError) as refusal:
            make_settings(
                services={
                    "spotify-api": service(token=TOKEN_A, audience_prefix="spotify"),
                    "media-tool": service(
                        token=TOKEN_A, audience_prefix="media-tool", namespaces=("media",)
                    ),
                }
            )

        assert "two services share a service token" in str(refusal.value)

    def test_two_services_with_their_own_tokens_are_accepted(self) -> None:
        settings = make_settings(
            services={
                "spotify-api": service(token=TOKEN_A, audience_prefix="spotify"),
                "media-tool": service(
                    token=TOKEN_B, audience_prefix="media-tool", namespaces=("media",)
                ),
            }
        )

        assert sorted(settings.services) == ["media-tool", "spotify-api"]

    def test_a_service_granted_a_namespace_the_deployment_does_not_allow_is_refused(self) -> None:
        with pytest.raises(ValidationError) as refusal:
            make_settings(
                services={"spotify-api": service(namespaces=("spotify",))},
                allowed_namespaces=("common", "user"),
            )

        # Named so the operator knows which of the two lists to change, rather than being
        # told only that they disagree.
        assert "service 'spotify-api' is granted spotify" in str(refusal.value)

    def test_the_refusal_names_every_namespace_the_service_cannot_reach(self) -> None:
        with pytest.raises(ValidationError) as refusal:
            make_settings(
                services={"tool": service(namespaces=("spotify", "media", "common"))},
                allowed_namespaces=("common",),
            )

        # Sorted and complete: one restart per missing namespace is how a five-minute fix
        # becomes an afternoon.
        assert "is granted media, spotify" in str(refusal.value)

    def test_a_service_granted_exactly_what_is_allowed_is_accepted(self) -> None:
        settings = make_settings(
            services={"spotify-api": service(namespaces=("spotify",))},
            allowed_namespaces=("common", "spotify"),
        )

        assert settings.services["spotify-api"].namespaces == ("spotify",)

    def test_a_later_service_is_checked_as_well_as_the_first(self) -> None:
        # The loop does not stop at the first service it likes; a second one added at the
        # bottom of a JSON document is the likeliest place for this mistake to appear.
        with pytest.raises(ValidationError) as refusal:
            make_settings(
                services={
                    "spotify-api": service(token=TOKEN_A, namespaces=("spotify",)),
                    "media-tool": service(
                        token=TOKEN_B, audience_prefix="media-tool", namespaces=("media",)
                    ),
                },
                allowed_namespaces=("common", "spotify"),
            )

        assert "service 'media-tool' is granted media" in str(refusal.value)

    def test_the_services_document_arrives_from_one_environment_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            f"{ENV_PREFIX}SERVICES",
            json.dumps(
                {
                    "spotify-api": service(),
                    "media-tool": service(
                        token=TOKEN_B, audience_prefix="media-tool", namespaces=("media",)
                    ),
                }
            ),
        )

        settings = make_settings()

        # One JSON document rather than a nested tree, because a configuration this
        # service cannot enumerate is one the typo check cannot check.
        assert settings.services["media-tool"].audience_prefix == "media-tool"

    def test_a_shared_token_is_refused_even_when_it_arrives_as_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            f"{ENV_PREFIX}SERVICES",
            json.dumps(
                {
                    "spotify-api": service(token=TOKEN_A),
                    "media-tool": service(
                        token=TOKEN_A, audience_prefix="media-tool", namespaces=("media",)
                    ),
                }
            ),
        )

        with pytest.raises(ValidationError) as refusal:
            make_settings()

        assert "two services share a service token" in str(refusal.value)


class TestUnknownFields:
    """``extra="forbid"``: a name nothing reads is a startup error, not a shrug."""

    def test_an_unrecognised_constructor_field_is_refused(self) -> None:
        with pytest.raises(ValidationError) as refusal:
            make_settings(databse_path="var/typo.db")

        assert "extra_forbidden" in str(refusal.value)

    def test_a_profile_is_not_a_setting(self) -> None:
        # One settings set per account. There is no profile anywhere, and the
        # configuration is one of the four places that has to keep being true.
        with pytest.raises(ValidationError):
            make_settings(profile="work")

    @pytest.mark.parametrize("field", ["allow_private_networks", "require_authentication"])
    def test_a_setting_this_family_refused_to_add_is_not_quietly_accepted(self, field: str) -> None:
        # The module docstring lists what is deliberately absent. Absence is only worth
        # anything if setting it anyway is refused rather than ignored.
        with pytest.raises(ValidationError):
            make_settings(**{field: True})


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

        with pytest.raises(UnknownSettingError, match="alowed"):
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
