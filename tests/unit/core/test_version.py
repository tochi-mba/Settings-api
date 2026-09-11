"""Which version a running service claims to be, and where that claim comes from.

`settings_api.core.version` answers one question -- "what is running?" -- and it is asked
in the two places an operator actually looks: the FastAPI app's OpenAPI document and the
health endpoints. Both call :func:`~settings_api.core.version.service_version`, so a wrong
answer here is a wrong answer on the one surface used to confirm a deploy landed.

The module has two ways to answer and this file pins both, plus the fact that a caller
cannot tell them apart:

**Installed distribution metadata wins.** That is what the deployment actually shipped. The
source constant in ``settings_api/__init__.py`` is only what the checkout *said* at the
moment it was written, and a wheel built from an older tree would keep reporting the newer
number if the constant were consulted first.

**A checkout that was never installed still answers.** Running out of ``src/`` -- a
developer's ``make run``, a container that copied the tree without installing it --
produces no distribution metadata at all, and a service that raised
:class:`importlib.metadata.PackageNotFoundError` from ``/health`` would be down for a
reason that has nothing to do with its health.

**The two names have to stay in step.** ``DISTRIBUTION_NAME`` is a string, so nothing but a
test notices when it stops naming the distribution this project ships as. The failure is
silent and permanent: every lookup misses, the fallback catches it, and the service quietly
reports the checkout constant forever -- in production, where it is least likely to be
right. The same goes for the constant itself drifting from ``pyproject.toml``.
"""

from __future__ import annotations

import re
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest

from settings_api import __version__
from settings_api.core import version as version_module
from settings_api.core.version import DISTRIBUTION_NAME, service_version

NOT_INSTALLED = [
    "settings-api-was-never-installed",
    "no-such-distribution-anywhere",
    "zzz.not.a.real.dist",
]
"""Names chosen to miss.

Each is checked against the real metadata index inside the test before it is used, so a
name that started resolving (someone installs a package by that name) fails loudly here
rather than turning the fallback test into one that silently exercises the happy path.
"""

OTHER_INSTALLED = "pytest"
"""A distribution that is certainly installed and is certainly not this one.

Its version differs from ``settings_api.__version__``, which is the only reason pointing
``DISTRIBUTION_NAME`` at it can distinguish "read the metadata" from "return the constant".
"""


def _pyproject() -> dict[str, Any]:
    """The project table, read from source rather than from installed metadata.

    Deliberately not via :mod:`importlib.metadata`: the checks below are about two
    hand-written strings in the tree agreeing with each other, and reading one of them
    through the install would let a stale venv answer the question instead.
    """
    root = Path(__file__).resolve().parents[3]
    with (root / "pyproject.toml").open("rb") as handle:
        loaded: dict[str, Any] = tomllib.load(handle)
    project: dict[str, Any] = loaded["project"]
    return project


def _normalise(name: str) -> str:
    """Fold a distribution name the way packaging tools do (PEP 503).

    ``settings_api`` and ``Settings-API`` name the same distribution, so comparing raw
    strings would fail a rename that changes nothing about what gets found.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


class TestInstalledMetadataIsWhatIsReported:
    """The deployed artefact's own metadata is the answer, when there is one."""

    def test_the_version_reported_is_the_installed_distributions_version(self) -> None:
        installed = metadata.version(DISTRIBUTION_NAME)

        assert service_version() == installed

    def test_installed_metadata_is_preferred_over_the_source_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Point the lookup at a different installed distribution and the answer moves.

        This is the only assertion here that can tell the two code paths apart. Against the
        real name both paths return ``0.1.0``, so a version() call that was never made
        would look exactly like one that was; against ``pytest`` the metadata answer and
        the source constant are different strings, and only the metadata one is correct.
        """
        other = metadata.version(OTHER_INSTALLED)
        # If these ever matched, the test below would pass without proving anything.
        assert other != __version__

        monkeypatch.setattr(version_module, "DISTRIBUTION_NAME", OTHER_INSTALLED)

        assert service_version() == other


class TestAnUninstalledCheckoutStillAnswers:
    """No distribution metadata is a normal way to run, not a failure."""

    @pytest.mark.parametrize("absent", NOT_INSTALLED)
    def test_a_checkout_with_no_installed_metadata_reports_the_source_version(
        self, absent: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Confirm the premise against the real metadata index first: the fallback is only
        # being exercised if this name genuinely has no distribution behind it.
        with pytest.raises(metadata.PackageNotFoundError):
            metadata.version(absent)

        monkeypatch.setattr(version_module, "DISTRIBUTION_NAME", absent)

        assert service_version() == __version__

    @pytest.mark.parametrize("absent", NOT_INSTALLED)
    def test_a_missing_distribution_is_never_raised_at_the_caller(
        self, absent: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``/health`` must not report a service as down because of how it was installed.

        ``PackageNotFoundError`` is a ``ModuleNotFoundError``, so a caller that only
        guarded against ``LookupError`` -- or, more likely, against nothing -- would turn a
        missing dist-info directory into a 500 on the one endpoint an operator uses to ask
        whether the deploy worked.
        """
        monkeypatch.setattr(version_module, "DISTRIBUTION_NAME", absent)

        reported = service_version()

        assert isinstance(reported, str)

    def test_the_fallback_does_not_depend_on_the_missing_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every uninstalled name lands on the same version, not on something derived.

        A fallback that reported, say, the name it failed to find would put a string that
        is not a version into the OpenAPI document's ``version`` field.
        """
        answers = set()
        for absent in NOT_INSTALLED:
            monkeypatch.setattr(version_module, "DISTRIBUTION_NAME", absent)
            answers.add(service_version())

        assert answers == {__version__}


class TestTheTwoPathsAreIndistinguishable:
    """A caller gets the same kind of answer either way, and never has to ask which."""

    @pytest.mark.parametrize(
        ("name", "installed"),
        [(DISTRIBUTION_NAME, True), (OTHER_INSTALLED, True), (NOT_INSTALLED[0], False)],
        ids=["this-distribution", "another-distribution", "not-installed"],
    )
    def test_the_answer_is_always_a_non_empty_string(
        self, name: str, installed: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same type, and never the empty string, whichever path produced it.

        The value is rendered straight into JSON responses and the OpenAPI document, where
        ``""`` reads as "unknown build" and ``None`` would fail the response model.
        """
        monkeypatch.setattr(version_module, "DISTRIBUTION_NAME", name)

        reported = service_version()

        assert isinstance(reported, str)
        assert reported != ""

    @pytest.mark.parametrize("name", [DISTRIBUTION_NAME, NOT_INSTALLED[0]])
    def test_asking_twice_gives_the_same_answer(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The app builds its OpenAPI version once and health answers on every request.

        Those two callers must agree, so nothing here may depend on call order or on state
        left behind by the first lookup.
        """
        monkeypatch.setattr(version_module, "DISTRIBUTION_NAME", name)

        assert service_version() == service_version()


class TestTheNamesStayInStep:
    """Both hand-written strings are only checked by this class."""

    def test_the_name_looked_up_resolves_to_this_project(self) -> None:
        """``DISTRIBUTION_NAME`` finds *this* distribution, not merely some distribution.

        A typo here never raises: the lookup misses, the fallback catches it, and the
        service reports the checkout constant in production for the rest of time.
        """
        found = metadata.distribution(DISTRIBUTION_NAME)
        declared: str = _pyproject()["name"]

        # Compared normalised, because a rename between `settings-api` and `settings_api`
        # finds the same distribution and is not a fault worth failing a build over.
        assert _normalise(str(found.metadata["Name"])) == _normalise(declared)

    def test_the_fallback_constant_matches_the_version_in_pyproject(self) -> None:
        """The two places the version is written down agree.

        They drift the moment a release bumps ``pyproject.toml`` alone -- and because the
        fallback is the path taken by every uninstalled checkout, the drift shows up as a
        developer's service claiming the previous release's number.
        """
        declared: str = _pyproject()["version"]

        assert __version__ == declared
