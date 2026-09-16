"""Service tokens, prefixes, namespaces, and the services document.

:mod:`settings_api.core.config` is the one place a deployment's security-relevant choices
become objects, and most of it is refusals: a service token short enough to guess, two
services sharing one. Each of those, let through, is a deployment that looks configured
and is not. Every refusal here happens at startup, which is the only moment somebody is
standing there able to fix it, so these tests pin *that* as much as they pin the rule
itself.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from settings_api.core.config import (
    ENV_PREFIX,
    MIN_SERVICE_TOKEN_CHARS,
    ServiceConfig,
)
from settings_api.domain.registry import NAMESPACES
from tests.unit.core._helpers import TOKEN_A, TOKEN_B, make_settings, service
from tests.unit.core._helpers import clean_env as clean_env  # noqa: PLC0414


def test_the_sample_tokens_are_long_enough_to_be_accepted() -> None:
    # If the minimum ever rises above these, every services test below would fail with a
    # message about token length rather than about the rule it was written for.
    assert len(TOKEN_A) >= MIN_SERVICE_TOKEN_CHARS
    assert len(TOKEN_B) >= MIN_SERVICE_TOKEN_CHARS
    assert TOKEN_A != TOKEN_B


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
