from copy import deepcopy
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

import pytest
import respx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from test_managed_runtime import ASSIGNMENT, CLIENT, CONNECTION, DISCOVERY, REGISTRY, managed_context, managed_router, managed_upstream

from lightnow_proxy.discovery_contributions import sanitized_setup_preview
from lightnow_proxy.app import create_app


@pytest.fixture
def setup():
    target = {"schema_version": 1, "server_version_id": str(uuid4()), "snapshot_sha256": "a" * 64,
              "variant_kind": "package", "variant_index": 0}
    template = {**target, "variant": {"registryType": "npm", "identifier": "@example/server", "version": "1.0.0",
        "environmentVariables": [{"name": "API_TOKEN", "isSecret": True}],
        "packageArguments": [{"type": "named", "name": "--config", "format": "filepath"},
                             {"type": "named", "name": "--mode", "choices": ["compact", "full"]}]},
        "public_choice_input_ids": ["package_argument:1"]}
    bound = {**target, "inputs": [{"input_id": "env:0", "provided": True},
        {"input_id": "package_argument:0", "provided": True, "file_binding": True},
        {"input_id": "package_argument:1", "provided": True, "public_choice_index": 0}], "omitted_unknown_fields": True}
    return template, bound


def test_exact_preview_contains_only_structural_facts_and_explicit_public_choice(setup):
    template, bound = setup
    assert sanitized_setup_preview(template, bound) == {"schema_version": 1, "variant_index": 0, "inputs": [
        {"input_id": "env:0", "provided": True, "value_kind": "secret"},
        {"input_id": "package_argument:0", "provided": True, "value_kind": "file"},
        {"input_id": "package_argument:1", "provided": True, "value_kind": "string", "public_choice_index": 0},
    ], "omitted_unknown_fields": True}


@pytest.mark.parametrize("field,value", [
    ("command", "CANARY shell --token private"), ("args", ["CANARY-positional"]),
    ("env", {"KEY": "CANARY-env"}), ("headers", {"Authorization": "CANARY-header"}),
    ("url", "https://CANARY-private.test?token=hidden"), ("runtime_files", [{"content": "CANARY-file"}]),
    ("tools", [{"description": "CANARY-metadata"}]), ("configuration_sha256", "CANARY-hash"),
])
def test_raw_configuration_never_enters_a_contribution_descriptor(setup, field, value, caplog):
    template, bound = setup
    bound[field] = value
    assert sanitized_setup_preview(template, bound) is None
    assert "CANARY" not in caplog.text


@pytest.mark.parametrize("field,value", [("server_version_id", str(uuid4())), ("snapshot_sha256", "b" * 64),
    ("variant_kind", "remote"), ("variant_index", 1)])
def test_target_mismatch_cannot_share_another_version_or_variant(setup, field, value):
    template, bound = setup
    bound[field] = value
    assert sanitized_setup_preview(template, bound) is None


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "unknown", "coerced", "extra", "bad_choice", "bad_file"])
def test_malformed_or_incomplete_bindings_are_unshareable(setup, mutation):
    template, bound = setup
    if mutation == "duplicate":
        bound["inputs"].append(deepcopy(bound["inputs"][0]))
    elif mutation == "missing":
        bound["inputs"].pop()
    elif mutation == "unknown":
        bound["inputs"][0]["input_id"] = "env:99"
    elif mutation == "coerced":
        bound["inputs"][0]["provided"] = "CANARY-value"
    elif mutation == "extra":
        bound["inputs"][0]["value"] = "CANARY-value"
    elif mutation == "bad_choice":
        bound["inputs"][0]["public_choice_index"] = True
    else:
        bound["inputs"][2]["file_binding"] = True
    assert sanitized_setup_preview(template, bound) is None


@pytest.mark.parametrize("kind", ["secret", "path", "url", "not_provided", "no_allowlist", "outside_choices"])
def test_choice_indexes_cannot_disclose_protected_or_unapproved_inputs(setup, kind):
    template, bound = setup
    definition = template["variant"]["packageArguments"][1]
    if kind == "secret":
        definition["isSecret"] = True
    elif kind == "path":
        definition["format"] = "filepath"
    elif kind == "url":
        definition["name"] = "--endpoint"
    elif kind == "not_provided":
        bound["inputs"][2]["provided"] = False
    elif kind == "outside_choices":
        bound["inputs"][2]["public_choice_index"] = 99
    else:
        template.pop("public_choice_input_ids")
    preview = sanitized_setup_preview(template, bound)
    assert preview is not None
    assert "public_choice_index" not in preview["inputs"][2]
    assert preview["omitted_unknown_fields"] is True


def test_remote_variables_have_deterministic_public_ids_and_secret_names_remain_placeholders():
    target = {"schema_version": 1, "server_version_id": str(uuid4()), "snapshot_sha256": "a" * 64,
              "variant_kind": "remote", "variant_index": 0}
    template = {**target, "variant": {"type": "streamable-http", "url": "https://public.example.test/{workspace}",
        "headers": [{"name": "Authorization"}], "variables": {"workspace": {}, "api_token": {}}}}
    bound = {**target, "inputs": [{"input_id": "header:0", "provided": True},
        {"input_id": "remote_variable:0", "provided": True}, {"input_id": "remote_variable:1", "provided": False}],
        "omitted_unknown_fields": False}
    preview = sanitized_setup_preview(template, bound)
    assert [item["value_kind"] for item in preview["inputs"]] == ["secret", "secret", "string"]
    assert "public.example.test" not in json.dumps(preview)


def test_secret_file_bindings_remain_secret_placeholders(setup):
    template, bound = setup
    bound["inputs"][0]["file_binding"] = True
    preview = sanitized_setup_preview(template, bound)
    assert preview["inputs"][0] == {"input_id": "env:0", "provided": True, "value_kind": "secret"}


def test_unknown_or_oversized_templates_never_fabricate_a_preview(setup):
    template, bound = setup
    assert sanitized_setup_preview(template, None) is None
    template["variant"]["description"] = "CANARY" * 3000
    assert sanitized_setup_preview(template, bound) is None
    assert sanitized_setup_preview(None, bound) is None


@pytest.mark.parametrize("field", ["schema_version", "variant_index"])
def test_boolean_coordinates_are_not_coerced_to_integer_coordinates(setup, field):
    template, bound = setup
    bound[field] = True
    assert sanitized_setup_preview(template, bound) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "stdio"])
@pytest.mark.parametrize("preparation", ["not_requested", "ready", "missing", "mismatched", "raw_field"])
async def test_native_discovery_transfers_only_opted_in_preview_and_preserves_private_success(tmp_path, setup, transport, preparation):
    template, bound = setup
    async with managed_upstream(transport, tmp_path) as (config, calls):
        if transport == "http":
            config["headers"] = {"Authorization": "CANARY-header"}
        else:
            config["env"] = {"TOKEN": "CANARY-env"}
        router = managed_router(tmp_path)
        router.registry_client.runtime_client_id = CLIENT
        context = managed_context()
        request = {**context, "id": DISCOVERY, "status": "queued", "etag": '"fixture:1"',
                   "expires_at": (datetime.now(UTC) + timedelta(seconds=120)).isoformat()}
        if preparation != "not_requested":
            request["contribution"] = {"state": "preparing", "template": template}
        if preparation == "mismatched":
            bound["snapshot_sha256"] = "b" * 64
        elif preparation == "raw_field":
            bound["args"] = ["CANARY-positional", "--secret=CANARY-flag"]
        item = {"alias": "docs", "connection_id": CONNECTION, "assignment_id": ASSIGNMENT, "status": "linked",
                "managed": context, "client_config": config, "context": {
                    "contribution_inputs": None if preparation == "missing" else bound}}
        with respx.mock(assert_all_called=True) as mock:
            mock.get(REGISTRY + "/integrations/profiles/default/servers").respond(200, json={"servers": [item]})
            report = mock.put(REGISTRY + f"/integrations/connections/{CONNECTION}/tool-discoveries/{DISCOVERY}").respond(200,
                json={"authorization": {"schema_version": 1, "expires_at": context["expires_at"], "allowed_tools": []}})
            async with AsyncExitStack() as stack:
                if preparation == "ready":
                    app = create_app(router.config, router=router)
                    await stack.enter_async_context(app.router.lifespan_context(app))
                    http = await stack.enter_async_context(httpx2.AsyncClient(
                        transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8080"))
                    downstream = await stack.enter_async_context(Client(streamable_http_client(
                        "http://127.0.0.1:8080/mcp", http_client=http)))
                    assert downstream.server_info.name == "lightnow-local-proxy"
                await router._run_discovery(request)
            body = json.loads(report.calls.last.request.content)
        assert body["status"] == "complete"
        assert calls() == []
        assert router.device_heartbeat_status()["managed_applications_pending"] == 1
        if preparation == "not_requested":
            assert "setup_preview" not in body
        elif preparation == "ready":
            assert body["setup_preview"] == sanitized_setup_preview(template, bound)
            serialized = json.dumps(body["setup_preview"])
            assert "CANARY" not in serialized and str(tmp_path) not in serialized
            assert "tools" not in serialized and "@example/server" not in serialized
        else:
            assert body["setup_preview"] is None
