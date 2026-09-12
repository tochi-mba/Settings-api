"""The HTTP contract, pinned.

The sixteen ``operation_id``s are MCP tool names: renaming one breaks every client with a
tool bound to it. The structural assertions are invariants 6 and 7 checked against the
generated schema rather than against a comment -- no path and no parameter anywhere names
an account or a profile.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from settings_api.api.app import create_app
from tests.conftest import build_settings

OPERATIONS = frozenset(
    {
        "check_liveness",
        "check_readiness",
        "describe_settings",
        "export_settings",
        "read_settings_events",
        "get_settings",
        "get_namespace",
        "get_setting",
        "update_settings",
        "set_setting",
        "import_settings",
        "reset_setting",
        "reset_namespace",
        "forget_settings",
        "resolve_settings",
        "set_setting_for_user",
    }
)

FORBIDDEN_NAMES = {"account_id", "account", "user_id", "subject", "profile", "profile_id", "sub"}


@pytest.fixture(scope="module")
def schema(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    app = create_app(build_settings(tmp_path_factory.mktemp("openapi")))
    document: dict[str, Any] = app.openapi()
    return document


def operations(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
    ]


def test_the_exact_set_of_sixteen_operation_ids(schema: dict[str, Any]) -> None:
    found = {operation["operationId"] for _, _, operation in operations(schema)}
    assert found == OPERATIONS, (
        "operation_ids are MCP tool names and public API: renaming one breaks every "
        f"client with a tool bound to it. Difference: {found ^ OPERATIONS}"
    )


class TestEveryOperation:
    def test_has_a_summary_and_a_longer_description(self, schema: dict[str, Any]) -> None:
        for _, _, operation in operations(schema):
            assert operation["summary"].strip(), operation["operationId"]
            assert len(operation["description"]) > len(operation["summary"]), operation[
                "operationId"
            ]

    def test_documents_a_response_for_every_failure_it_can_produce(
        self, schema: dict[str, Any]
    ) -> None:
        expected_failures = {
            "get_namespace": {"401", "403", "404"},
            "get_setting": {"401", "403", "404"},
            "set_setting": {"401", "403", "404", "409", "412", "422"},
            "update_settings": {"400", "401", "403", "404", "409", "412", "422"},
            "import_settings": {"400", "401", "403", "404", "409", "412", "422"},
            "reset_setting": {"401", "403", "404", "409", "412"},
            "reset_namespace": {"401", "403", "404", "412"},
            "resolve_settings": {"304", "401", "403", "404", "503"},
            "set_setting_for_user": {"401", "403", "404", "409", "422", "503"},
            "check_readiness": {"503"},
        }
        for _, _, operation in operations(schema):
            wanted = expected_failures.get(operation["operationId"], set())
            assert wanted <= set(operation["responses"]), operation["operationId"]

    def test_no_path_or_parameter_names_an_account_or_a_profile(
        self, schema: dict[str, Any]
    ) -> None:
        # Invariants 6 and 7, against the generated contract. A cross-account read and a
        # per-profile setting are not forbidden; they are inexpressible.
        for path, _, operation in operations(schema):
            segments = {segment.strip("{}") for segment in path.split("/")}
            assert not (segments & FORBIDDEN_NAMES), path
            for parameter in operation.get("parameters", []):
                assert parameter["name"] not in FORBIDDEN_NAMES, (path, parameter["name"])

    def test_the_two_internal_operations_are_tagged_for_services_only(
        self, schema: dict[str, Any]
    ) -> None:
        for _, _, operation in operations(schema):
            if operation["operationId"] in {"resolve_settings", "set_setting_for_user"}:
                assert operation["tags"] == ["internal"]
            else:
                assert "internal" not in operation["tags"]


class TestRequestBodies:
    def test_every_request_body_forbids_extra_fields(self, schema: dict[str, Any]) -> None:
        bodies = {
            name: component
            for name, component in schema["components"]["schemas"].items()
            if name.endswith("Request")
        }
        assert set(bodies) == {
            "SetSettingRequest",
            "UpdateSettingsRequest",
            "ImportSettingsRequest",
        }
        for name, component in bodies.items():
            assert component.get("additionalProperties") is False, name

    def test_the_write_descriptions_carry_the_two_load_bearing_paragraphs(
        self, schema: dict[str, Any]
    ) -> None:
        by_id = {operation["operationId"]: operation for _, _, operation in operations(schema)}
        for name in ("update_settings", "set_setting"):
            assert "Do not change these on your own initiative" in by_id[name]["description"]
            assert "never retroactive" in by_id[name]["description"]


def test_the_contract_is_stable_across_settings(tmp_path: Path) -> None:
    # The operation set does not depend on the deployment's configuration.
    narrowed = create_app(build_settings(tmp_path, allowed_namespaces=("spotify",), services={}))
    assert {op["operationId"] for _, _, op in operations(narrowed.openapi())} == OPERATIONS
