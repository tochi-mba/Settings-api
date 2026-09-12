"""Which service is calling, decided in constant time with no early return.

A comparison that returned as soon as it found a match would leak, in its timing, roughly
where in the list the caller sits. The structural test reads the source and asserts there
is no ``break``; the behavioural ones assert the right grant comes back wherever the
matching service sits in the mapping.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import SecretStr

from settings_api.auth.service_tokens import BAD_SERVICE, ServiceAuthenticator
from settings_api.core.config import ServiceConfig
from settings_api.domain.errors import AuthenticationError
from settings_api.domain.namespaces import ServiceGrant

FIRST = "first-service-token-0123456789abcdef"
LAST = "last-service-token-0123456789abcdefgh"


def config(token: str, prefix: str, *namespaces: str) -> ServiceConfig:
    return ServiceConfig(token=SecretStr(token), audience_prefix=prefix, namespaces=namespaces)


SERVICES = {
    "alpha": config(FIRST, "alpha", "spotify"),
    "beta": config("beta-service-token-0123456789abcdefg", "beta", "media", "search"),
    "omega": config(LAST, "omega", "user"),
}


class TestIdentifying:
    def test_each_token_yields_its_own_grant(self) -> None:
        authenticator = ServiceAuthenticator(services=SERVICES)
        assert authenticator.identify(FIRST) == ServiceGrant(
            service="alpha", audience_prefix="alpha", namespaces=frozenset({"spotify"})
        )
        assert authenticator.identify(LAST).service == "omega"

    def test_the_grant_carries_the_configured_namespaces_as_a_frozenset(self) -> None:
        grant = ServiceAuthenticator(services=SERVICES).identify(
            "beta-service-token-0123456789abcdefg"
        )
        assert grant.namespaces == frozenset({"media", "search"})
        assert grant.audience_prefix == "beta"

    def test_an_unknown_token_is_refused_with_one_message(self) -> None:
        with pytest.raises(AuthenticationError, match=BAD_SERVICE):
            ServiceAuthenticator(services=SERVICES).identify("nobody-knows-this-token-0123456789")

    def test_a_prefix_of_a_real_token_is_refused(self) -> None:
        with pytest.raises(AuthenticationError, match=BAD_SERVICE):
            ServiceAuthenticator(services=SERVICES).identify(FIRST[:-1])

    def test_a_token_differing_in_its_last_character_is_refused(self) -> None:
        with pytest.raises(AuthenticationError, match=BAD_SERVICE):
            ServiceAuthenticator(services=SERVICES).identify(FIRST[:-1] + "X")

    def test_no_services_configured_refuses_everything(self) -> None:
        # The right state for a service nobody has been told to trust yet.
        with pytest.raises(AuthenticationError, match=BAD_SERVICE):
            ServiceAuthenticator(services={}).identify(FIRST)

    def test_configured_lists_the_names_sorted(self) -> None:
        assert ServiceAuthenticator(services=SERVICES).configured == ("alpha", "beta", "omega")


class TestNoEarlyReturn:
    def test_the_matching_service_may_sit_anywhere_in_the_mapping(self) -> None:
        authenticator = ServiceAuthenticator(services=SERVICES)
        assert authenticator.identify(FIRST).service == "alpha"
        assert authenticator.identify(LAST).service == "omega"

    def test_the_loop_contains_no_break(self) -> None:
        # Read rather than timed, because a timing test on a CI box proves nothing. If
        # somebody adds a `break` to "optimise" the loop, this is what tells them why not.
        code = "\n".join(
            line
            for line in inspect.getsource(ServiceAuthenticator.identify).splitlines()
            if not line.strip().startswith("#")
        )
        assert "break" not in code
        assert "compare_digest" in code
