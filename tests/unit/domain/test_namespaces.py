"""Namespaces, and who may read them.

Two rules carry the whole service-facing security model, and both are one function here:
``ServiceGrant.accepts_audience`` is the confused-deputy defence, and ``grants`` is the
blast radius of a leaked service token. The person-facing side is ``granted_namespaces``,
which is user-api's scopes-from-the-audience shape applied to a different question.
"""

from __future__ import annotations

import pytest

from settings_api.domain.errors import NamespaceNotGrantedError, UnknownNamespaceError
from settings_api.domain.namespaces import (
    COMMON,
    ServiceGrant,
    granted_namespaces,
    require_granted,
    require_known,
)

DOWNSTREAM = ServiceGrant(
    service="downstream-tool",
    audience_prefix="downstream-tool",
    namespaces=frozenset({"environments"}),
)
ALLOWED = ("common", "environments", "search", "spotify", "user")


class TestWhatAServiceMayRead:
    def test_its_own_namespace(self) -> None:
        assert DOWNSTREAM.grants("environments") is True

    def test_common_without_being_listed(self) -> None:
        # The block every service needs and none owns. A deployment that had to remember
        # to add it to six grants would eventually forget it in one.
        assert DOWNSTREAM.grants(COMMON) is True

    @pytest.mark.parametrize("namespace", ["user", "search", "spotify", "keyring", "nope"])
    def test_nothing_else(self, namespace: str) -> None:
        # The blast radius of one compromised service token is exactly its namespaces.
        assert DOWNSTREAM.grants(namespace) is False

    def test_require_names_the_service_and_the_namespace(self) -> None:
        with pytest.raises(
            NamespaceNotGrantedError, match="downstream-tool is not granted the user namespace"
        ):
            DOWNSTREAM.require("user")

    def test_require_is_silent_for_a_granted_namespace(self) -> None:
        DOWNSTREAM.require("environments")
        DOWNSTREAM.require(COMMON)


class TestWhoseTokensAServiceMayPresent:
    """THE check. Without it a static service token plus any user token reads any account."""

    @pytest.mark.parametrize(
        "audience", ["downstream-tool", "downstream-tool.jobs", "downstream-tool.a.b"]
    )
    def test_its_own_family(self, audience: str) -> None:
        assert DOWNSTREAM.accepts_audience(audience) is True

    @pytest.mark.parametrize(
        "audience",
        [
            "settings",
            "spotify",
            "user",
            "environments",
            "downstream-toolkit",
            "xdownstream-tool",
            "downstream-tool-2",
            "",
            ".",
        ],
    )
    def test_nothing_else(self, audience: str) -> None:
        assert DOWNSTREAM.accepts_audience(audience) is False

    def test_the_separator_is_what_stops_one_prefix_being_a_prefix_of_another(self) -> None:
        # "downstream-toolkit" starts with "downstream-tool". Plain startswith would accept
        # it, and a service called downstream-toolkit would then be able to present
        # downstream-tool's tokens.
        assert DOWNSTREAM.accepts_audience("downstream-toolkit") is False
        assert DOWNSTREAM.accepts_audience("downstream-tool.kit") is True


class TestWhatAPersonsTokenGrants:
    def test_the_bare_prefix_grants_everything_the_deployment_allows(self) -> None:
        assert granted_namespaces("settings", prefix="settings", allowed=ALLOWED) == frozenset(
            ALLOWED
        )

    def test_a_dotted_audience_grants_that_namespace_and_common(self) -> None:
        # The useful compartment: an assistant that may set search preferences and may not
        # touch erasure policy. common comes along because every read merges it.
        assert granted_namespaces(
            "settings.search", prefix="settings", allowed=ALLOWED
        ) == frozenset({"search", "common"})

    def test_a_token_for_another_service_is_refused(self) -> None:
        # The case that matters most: another service's token presented on the
        # person-facing surface. Verifying it against our audience would fail anyway; this
        # is where it is named.
        with pytest.raises(NamespaceNotGrantedError, match="is not in the 'settings' family"):
            granted_namespaces("downstream-tool", prefix="settings", allowed=ALLOWED)

    def test_a_dotted_audience_from_another_family_is_refused(self) -> None:
        with pytest.raises(NamespaceNotGrantedError, match="not in the 'settings' family"):
            granted_namespaces("user.health", prefix="settings", allowed=ALLOWED)

    def test_an_unknown_namespace_is_refused_rather_than_granting_nothing(self) -> None:
        # A typo in a mint request would otherwise produce a token that works, reads
        # nothing, and looks like a correctly configured assistant that has simply not
        # been told anything.
        with pytest.raises(NamespaceNotGrantedError, match="does not recognise"):
            granted_namespaces("settings.serach", prefix="settings", allowed=ALLOWED)

    def test_an_empty_scope_after_the_dot_is_refused(self) -> None:
        with pytest.raises(NamespaceNotGrantedError, match="does not recognise"):
            granted_namespaces("settings.", prefix="settings", allowed=ALLOWED)

    def test_allowed_may_be_any_iterable(self) -> None:
        assert granted_namespaces(
            "settings.environments", prefix="settings", allowed=iter(["environments"])
        ) == frozenset({"environments", "common"})


class TestRequireKnown:
    def test_a_known_namespace_is_silent(self) -> None:
        require_known("environments", known=ALLOWED)

    def test_an_unknown_one_names_what_exists(self) -> None:
        # Checked before the grant, so a caller that misspells a namespace it DOES hold is
        # told it misspelled it, rather than sent looking for a permissions problem.
        with pytest.raises(
            UnknownNamespaceError,
            match="no namespace 'enviroments'; this build has common, environments",
        ):
            require_known("enviroments", known=ALLOWED)


class TestRequireGranted:
    def test_a_granted_namespace_is_silent(self) -> None:
        require_granted("environments", granted={"environments", "common"})

    def test_an_ungranted_one_is_a_fact_about_the_token(self) -> None:
        with pytest.raises(
            NamespaceNotGrantedError, match="this token does not grant the user namespace"
        ):
            require_granted("user", granted={"environments", "common"})
