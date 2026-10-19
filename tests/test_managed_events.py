from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from time import monotonic
from uuid import uuid4

import httpx
import pytest
import respx

from lightnow_proxy.app import create_app
from lightnow_proxy.managed_events import EvidenceStoreError, ManagedEventOutbox
from mcp.types import CallToolResult, TextContent, Tool

from lightnow_proxy.config import UpstreamConfig
from lightnow_proxy.registry import ManagedRuntime
from lightnow_proxy.router import ProfileUpstream
from lightnow_proxy.upstream import ToolInventory
from test_managed_runtime import CLIENT, CONNECTION, DISCOVERY, REGISTRY, managed_context, managed_router

IDENTITY = ("https://registry.example.test/v0.1/integrations/runtime-events", "https://auth.example.test", "actor")


def lease() -> ManagedRuntime:
    return ManagedRuntime.model_validate({**managed_context(), "deadline": monotonic() + 45})


def journal(tmp_path: Path) -> ManagedEventOutbox:
    return ManagedEventOutbox(tmp_path / "private" / "events.sqlite3")


def test_reopen_retries_identical_private_facts_and_acknowledges_only_completed_calls(tmp_path):
    box = journal(tmp_path)
    call = box.begin(lease(), IDENTITY)
    box.record(call, "dispatch_started")
    box.record(call, "succeeded")
    call.release()
    events = box.pending(IDENTITY)
    assert [event["phase"] for event in events] == ["attempted", "dispatch_started", "dispatched", "succeeded"]
    assert len({event["call_id"] for event in events}) == 1
    assert len({event["id"] for event in events}) == 4
    assert journal(tmp_path).pending(IDENTITY) == events
    assert box.path.stat().st_mode & 0o777 == 0o600
    assert box.path.parent.stat().st_mode & 0o777 == 0o700
    assert "private-report-do-not-log" not in repr(call)
    for event in events:
        assert set(event) <= {"schema_version", "id", "call_id", "phase", "occurred_at", "reporting_proof", "duration_ms"}
        box.acknowledge(event["id"])
    assert box.status()["pending"] == 0
    with sqlite3.connect(box.path) as db:
        assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    assert not list(box.path.parent.glob("*.lock"))


@pytest.mark.parametrize("handoff,terminal,reason", [(False, "runtime_failed", "RUNTIME_START_FAILED"),
                                                     (True, "outcome_unknown", "UPSTREAM_OUTCOME_UNKNOWN")])
def test_recovery_skips_live_call_and_never_invents_success_after_owner_exits(tmp_path, handoff, terminal, reason):
    box = journal(tmp_path)
    call = box.begin(lease(), IDENTITY)
    call.observation_id = "8bd4fac0-18ba-43d4-ab53-5e706e2631bd"
    call.original_tool_name = "read"
    if handoff:
        box.record(call, "dispatch_started")
    other = journal(tmp_path)
    assert len(other.pending(IDENTITY)) == (2 if handoff else 1)
    assert not any(event["phase"] == terminal for event in other.pending(IDENTITY))
    call.release()  # The OS does this on process death; another owner can recover.
    facts = other.pending(IDENTITY)
    assert facts[-1]["phase"] == terminal and facts[-1]["reason_code"] == reason
    assert "succeeded" not in {event["phase"] for event in facts}
    if handoff:
        assert facts[-1]["observation_id"] == call.observation_id and facts[-1]["original_tool_name"] == "read"
    assert other.pending(IDENTITY) == facts


def test_actor_issuer_and_registry_binding_cannot_relabel_pending_facts(tmp_path):
    box = journal(tmp_path)
    call = box.begin(lease(), IDENTITY)
    box.record(call, "denied", "TOOL_NOT_SELECTED")
    call.release()
    for changed in [(IDENTITY[0], IDENTITY[1], "other-actor"), (IDENTITY[0], "https://other-issuer.test", IDENTITY[2]),
                    ("https://other-registry.test/runtime-events", IDENTITY[1], IDENTITY[2])]:
        assert box.pending(changed) == []
    assert [event["phase"] for event in box.pending(IDENTITY)] == ["attempted", "denied"]


def test_expired_and_rejected_reports_remain_visible_and_keep_original_ids(tmp_path):
    box = journal(tmp_path)
    call = box.begin(lease(), IDENTITY)
    box.record(call, "runtime_failed", "RUNTIME_START_FAILED")
    call.release()
    events = box.pending(IDENTITY)
    box.reject(events[0]["id"], "REPORT_REJECTED_403")
    with sqlite3.connect(box.path) as db:
        db.execute("UPDATE calls SET report_until=0")
    assert box.pending(IDENTITY) == []
    assert box.status()["pending"] == box.status()["unresolved"] == 2
    with sqlite3.connect(box.path) as db:
        assert [json.loads(row[0])["id"] for row in db.execute("SELECT payload FROM events ORDER BY sequence")] == [event["id"] for event in events]


def test_missing_receipt_capacity_exhaustion_and_corruption_fail_closed(tmp_path, monkeypatch):
    box = journal(tmp_path)
    with pytest.raises(EvidenceStoreError):
        box.begin(lease().model_copy(update={"reporting_proof": None}), IDENTITY)
    monkeypatch.setattr("lightnow_proxy.managed_events.MAX_PENDING_CALLS", 1)
    call = box.begin(lease(), IDENTITY)
    try:
        with pytest.raises(EvidenceStoreError):
            box.begin(lease(), IDENTITY)
    finally:
        call.release()
    broken = tmp_path / "broken" / "events.sqlite3"
    broken.parent.mkdir()
    broken.write_text("not a database")
    with pytest.raises(EvidenceStoreError):
        ManagedEventOutbox(broken)


@pytest.mark.parametrize("handoff", [False, True])
def test_os_process_exit_recovers_committed_facts_without_dispatch_retry(tmp_path, handoff):
    box = journal(tmp_path)
    subprocess.run([sys.executable, "-c", """
import json, os, sys
from pathlib import Path
from time import monotonic
from uuid import uuid4
from lightnow_proxy.managed_events import ManagedEventOutbox
from mcp.types import CallToolResult, TextContent, Tool

from lightnow_proxy.config import UpstreamConfig
from lightnow_proxy.registry import ManagedRuntime
value = json.load(sys.stdin)
box = ManagedEventOutbox(Path(sys.argv[1]))
call = box.begin(ManagedRuntime.model_validate({**value['lease'], 'deadline': monotonic() + 45}), tuple(value['identity']))
if value['handoff']:
    box.record(call, 'dispatch_started')
os._exit(0)
""", str(box.path)], input=json.dumps({"lease": lease().model_dump(mode="json"), "identity": IDENTITY, "handoff": handoff}),
                   text=True, check=True, timeout=10)
    facts = box.pending(IDENTITY)
    assert facts[-1]["phase"] == ("outcome_unknown" if handoff else "runtime_failed")
    assert len(facts) == (3 if handoff else 2)
    assert box.pending(IDENTITY) == facts


@pytest.mark.asyncio
async def test_delivery_retries_only_identical_facts_and_requires_exact_v2_acknowledgement(tmp_path):
    router = managed_router(tmp_path)
    box = router._event_outbox()
    identity = await router.registry_client.managed_report_identity()
    call = box.begin(lease(), identity)
    box.record(call, "denied", "TOOL_NOT_SELECTED")
    call.release()
    original = box.pending(identity)
    delivered = []
    response_mode = "unavailable"
    def endpoint(request):
        value = json.loads(request.content)
        delivered.append(value)
        assert "X-Tenant" not in request.headers
        if response_mode == "unavailable":
            return httpx.Response(503)
        return httpx.Response(201, json={"event": value if response_mode == "valid" else {"id": "wrong"}})
    with respx.mock(assert_all_called=True) as api:
        api.post(REGISTRY + "/integrations/runtime-events").mock(side_effect=endpoint)
        await router.flush_managed_events()
        assert box.pending(identity) == original
        response_mode = "wrong-ack"
        await router.flush_managed_events()
        assert box.pending(identity) == original
        response_mode = "valid"
        await router.flush_managed_events()
    assert delivered[0] == delivered[1] == delivered[2] == original[0]
    assert delivered[3] == original[1]
    assert router.managed_event_status()["pending"] == 0
    assert router.managed_event_status()["delivery_error"] is None


@pytest.mark.asyncio
async def test_required_uploader_runs_with_optional_telemetry_and_presence_disabled(tmp_path):
    router = managed_router(tmp_path)
    assert not router.config.local_proxy.telemetry_enabled
    assert not router.device_heartbeat_enabled()
    identity = await router.registry_client.managed_report_identity()
    box = router._event_outbox()
    call = box.begin(lease(), identity)
    box.record(call, "denied", "TOOL_NOT_SELECTED")
    call.release()
    def endpoint(request):
        return httpx.Response(201, json={"event": json.loads(request.content)})
    with respx.mock(assert_all_called=True) as api:
        api.post(REGISTRY + "/integrations/runtime-events").mock(side_effect=endpoint)
        app = create_app(router.config, router=router)
        async with app.router.lifespan_context(app):
            async with asyncio.timeout(3):
                while box.status()["pending"]:
                    await asyncio.sleep(0.01)
    assert router.managed_event_status()["pending"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,phase", [("upstream_error", "upstream_failed"), ("transport_loss", "outcome_unknown"),
                                          ("cancelled", "outcome_unknown")])
async def test_response_transport_loss_and_cancellation_have_distinct_immutable_outcomes(tmp_path, failure, phase):
    router = managed_router(tmp_path)
    router.registry_client.runtime_client_id = CLIENT
    managed = lease()
    dispatches = 0
    class DispatchOnce:
        async def call_tool(self, config, name, arguments, *, authorize):
            nonlocal dispatches
            await authorize(ToolInventory([Tool(name="read", input_schema={})], 1, 1))
            dispatches += 1
            if failure == "transport_loss":
                raise TimeoutError("private transport detail")
            if failure == "cancelled":
                raise asyncio.CancelledError()
            return CallToolResult(is_error=True, content=[TextContent(type="text", text="private upstream response")])
    router.upstream_client = DispatchOnce()
    base = REGISTRY + f"/integrations/connections/{CONNECTION}/tool-discoveries"
    with respx.mock(assert_all_called=True) as api:
        api.post(base).respond(201, json={"data": {"id": DISCOVERY}}, headers={"ETag": '"1"'})
        api.put(base + "/" + DISCOVERY).respond(200, json={"authorization": {
            "schema_version": 1, "expires_at": managed.expires_at, "allowed_tools": ["read"],
            "requested_tool": {"name": "read", "allowed": True, "reason_code": None},
            "reporting_proof": "bound-original-lifetime-receipt",
        }})
        upstream = ProfileUpstream(config=UpstreamConfig(url="https://private-upstream.test/mcp"), managed=managed)
        if failure == "upstream_error":
            result = await router._call_managed_tool(upstream, "read", {"secret": "private request"})
            assert result.is_error
        else:
            with pytest.raises(TimeoutError if failure == "transport_loss" else asyncio.CancelledError):
                await router._call_managed_tool(upstream, "read", {"secret": "private request"})
    identity = await router.registry_client.managed_report_identity()
    facts = router._event_outbox().pending(identity)
    assert dispatches == 1
    assert facts[0]["reporting_proof"] == managed.reporting_proof
    assert all(fact["reporting_proof"] == "bound-original-lifetime-receipt" for fact in facts[1:])
    assert [fact["phase"] for fact in facts] == ["attempted", "dispatch_started"] + (["dispatched"] if failure == "upstream_error" else []) + [phase]
    assert facts[-1]["reason_code"] == ("UPSTREAM_FAILED" if failure == "upstream_error" else "UPSTREAM_OUTCOME_UNKNOWN")
    assert "private " not in json.dumps(facts)
    assert "private-upstream" not in json.dumps(facts)


def test_report_receipt_survives_business_expiry_without_persisting_execution_context(tmp_path, monkeypatch):
    box = journal(tmp_path)
    managed = lease()
    box.remember(managed, IDENTITY)
    with sqlite3.connect(box.path) as db:
        stored = db.execute("SELECT * FROM report_receipts").fetchone()
    assert managed.reporting_proof in stored
    assert managed.context_proof not in json.dumps(stored)
    monkeypatch.setattr("lightnow_proxy.managed_events.time", lambda: managed.expires_at + 60)
    assert box.begin_cached((IDENTITY[0], IDENTITY[1], "other-actor"), managed.profile, str(managed.assignment_id)) is None
    assert box.begin_cached(IDENTITY, "other-profile", str(managed.assignment_id)) is None
    assert box.begin_cached(IDENTITY, managed.profile, str(uuid4())) is None
    call = box.begin_cached(IDENTITY, managed.profile, str(managed.assignment_id))
    assert call is not None
    box.record(call, "runtime_failed", "CONTROL_PLANE_UNAVAILABLE")
    call.release()
    facts = box.pending(IDENTITY)
    assert [event["phase"] for event in facts] == ["attempted", "runtime_failed"]
    assert all(event["reporting_proof"] == managed.reporting_proof for event in facts)
    assert all("original_tool_name" not in event and "observation_id" not in event for event in facts)
    monkeypatch.setattr("lightnow_proxy.managed_events.time", lambda: managed.issued_at + 7 * 86400 + 1)
    assert box.begin_cached(IDENTITY, managed.profile, str(managed.assignment_id)) is None
    box.unattributed()
    assert box.pending(IDENTITY) == []
    assert journal(tmp_path).status()["unattributed_attempts"] == 1
    assert journal(tmp_path).status()["pending"] == journal(tmp_path).status()["unresolved"] == 2


def test_report_receipt_cache_has_its_own_capacity_without_deleting_queued_facts(tmp_path, monkeypatch):
    monkeypatch.setattr("lightnow_proxy.managed_events.MAX_REPORT_RECEIPTS", 2)
    box = journal(tmp_path)
    receipts = [lease().model_copy(update={"assignment_id": uuid4()}) for _ in range(3)]
    pending = box.begin(receipts[0], IDENTITY)
    box.record(pending, "denied", "TOOL_NOT_SELECTED")
    pending.release()
    for managed in receipts:
        box.remember(managed, IDENTITY)
    with sqlite3.connect(box.path) as db:
        retained = {row[0] for row in db.execute("SELECT assignment_id FROM report_receipts")}
    assert retained == {str(receipts[1].assignment_id), str(receipts[2].assignment_id)}
    assert len(box.pending(IDENTITY)) == 2


@pytest.mark.asyncio
async def test_failed_first_lookup_records_a_durable_unattributed_gap_without_inventing_a_scope(tmp_path):
    router = managed_router(tmp_path)
    with respx.mock(assert_all_called=True) as api:
        api.get(REGISTRY + "/integrations/profiles/default/servers").respond(503)
        with pytest.raises(httpx.HTTPStatusError):
            await router.call_tool("default", "ln_11111111111141118111111111111111__read", {"secret": "private request"})
    box = router._event_outbox()
    assert box.status()["unattributed_attempts"] == 1
    assert box.status()["pending"] == 0
    assert ManagedEventOutbox(box.path).status()["unattributed_attempts"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [False, True])
async def test_unresolved_managed_attempt_never_captures_arguments_or_raw_errors(tmp_path, known):
    router = managed_router(tmp_path)
    router.config.local_proxy.telemetry_enabled = True
    router.config.local_proxy.capture_tool_arguments = True
    managed = lease()
    if known:
        await router._remember_reporter(managed)
    optional = []
    def legacy_event(request):
        optional.append(json.loads(request.content))
        return httpx.Response(201)
    with respx.mock(assert_all_called=True) as api:
        api.get(REGISTRY + "/integrations/profiles/default/servers").respond(503)
        api.post(REGISTRY + "/integrations/runtime-events").mock(side_effect=legacy_event)
        with pytest.raises(httpx.HTTPStatusError):
            await router.call_tool("default", managed.tool_namespace + "__read", {"value": "private request"})
    assert len(optional) == 1
    assert optional[0].get("arguments") is None and optional[0]["arguments_captured"] is False
    assert optional[0].get("error_message") is None
    assert "private request" not in json.dumps(optional)
    identity = await router.registry_client.managed_report_identity()
    facts = router._event_outbox().pending(identity)
    assert len(facts) == (2 if known else 0)
    if known:
        assert facts[-1]["phase"] == "runtime_failed" and facts[-1]["reason_code"] == "CONTROL_PLANE_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,phase,reason", [
    (403, {"status": 403, "message": "private denial detail"}, "denied", "PROFILE_ACCESS_DENIED"),
    (503, {"error": {"code": "ORGANIZATION_ACCESS_UNAVAILABLE", "reason": "SOURCE_EXPIRED"}}, "runtime_failed", "SOURCE_EXPIRED"),
])
async def test_report_only_receipt_retains_original_context_for_closed_control_plane_decisions(tmp_path, status, body, phase, reason):
    router = managed_router(tmp_path)
    managed = lease()
    await router._remember_reporter(managed)
    with respx.mock(assert_all_called=True) as api:
        api.get(REGISTRY + "/integrations/profiles/default/servers").respond(status, json=body)
        with pytest.raises(httpx.HTTPStatusError):
            await router.call_tool("default", managed.tool_namespace + "__read", {"value": "private request"})
    facts = router._event_outbox().pending(await router.registry_client.managed_report_identity())
    assert [fact["phase"] for fact in facts] == ["attempted", phase]
    assert facts[-1]["reason_code"] == reason
    assert all(fact["reporting_proof"] == managed.reporting_proof for fact in facts)
    assert all("original_tool_name" not in fact for fact in facts)
    assert "private " not in json.dumps(facts)


def test_preflight_reservations_transfer_atomically_and_capacity_does_not_create_orphan_locks(tmp_path, monkeypatch):
    box = journal(tmp_path)
    box.unattributed()
    box.unattributed()
    call = box.begin(lease(), IDENTITY, reserved=True)
    box.record(call, "denied", "TOOL_NOT_SELECTED")
    call.release()
    assert box.status()["unattributed_attempts"] == 1
    locks = set(box.path.parent.glob("*.lock"))
    monkeypatch.setattr("lightnow_proxy.managed_events.MAX_PENDING_CALLS", 1)
    box.unattributed()
    with pytest.raises(EvidenceStoreError):
        box.begin(lease(), IDENTITY, reserved=True)
    assert box.status()["unattributed_attempts"] == 2
    assert len(box.pending(IDENTITY)) == 2
    assert set(box.path.parent.glob("*.lock")) == locks


def test_process_exit_during_preflight_keeps_a_durable_gap_without_inventing_an_owner(tmp_path):
    box = journal(tmp_path)
    subprocess.run([sys.executable, "-c", """
import os, sys
from pathlib import Path
from lightnow_proxy.managed_events import ManagedEventOutbox
ManagedEventOutbox(Path(sys.argv[1])).unattributed()
os._exit(0)
""", str(box.path)], check=True, timeout=10)
    assert ManagedEventOutbox(box.path).status()["unattributed_attempts"] == 1
    assert box.status()["pending"] == 0


@pytest.mark.asyncio
async def test_removed_assignment_is_denied_using_its_original_report_receipt(tmp_path):
    router = managed_router(tmp_path)
    managed = lease()
    await router._remember_reporter(managed)
    with respx.mock(assert_all_called=True) as api:
        api.get(REGISTRY + "/integrations/profiles/default/servers").respond(200, json={"servers": [], "organization_admission": {"required": True, "policy_revision": 1}})
        with pytest.raises(Exception, match="MANAGED_ASSIGNMENT_UNAVAILABLE"):
            await router.call_tool("default", managed.tool_namespace + "__read", {"value": "private request"})
    box = router._event_outbox()
    facts = box.pending(await router.registry_client.managed_report_identity())
    assert [fact["phase"] for fact in facts] == ["attempted", "denied"]
    assert facts[-1]["reason_code"] == "ASSIGNMENT_UNAVAILABLE"
    assert all(fact["reporting_proof"] == managed.reporting_proof for fact in facts)
    assert box.status()["unattributed_attempts"] == 0


def test_bound_name_receipt_survives_recovery_without_mutating_prior_fact(tmp_path):
    box = journal(tmp_path)
    call = box.begin(lease(), IDENTITY)
    original = box.pending(IDENTITY)[0]
    call.observation_id = DISCOVERY
    call.original_tool_name = "read"
    call.reporting_proof = "bound-name-receipt"
    box.record(call, "dispatch_started")
    call.release()
    facts = journal(tmp_path).pending(IDENTITY)
    assert facts[0] == original
    assert [item["phase"] for item in facts] == ["attempted", "dispatch_started", "outcome_unknown"]
    assert all(item["reporting_proof"] == "bound-name-receipt" and item["observation_id"] == DISCOVERY for item in facts[1:])


def test_shared_actor_journal_cannot_relabel_one_clients_cached_failure_as_another(tmp_path):
    first = managed_router(tmp_path)
    second = managed_router(tmp_path)
    installation = uuid4()
    first.config.local_proxy.device_installation_id = second.config.local_proxy.device_installation_id = installation
    first.config.local_proxy.client_instance_id = uuid4()
    second.config.local_proxy.client_instance_id = uuid4()
    original = lease()
    other = original.model_copy(update={"runtime_client_id": uuid4(), "reporting_proof": "second-client-original-receipt"})
    box_a, box_b = first._event_outbox(), second._event_outbox()
    assert box_a.path == box_b.path
    box_a.remember(original, IDENTITY)
    assert box_b.begin_cached(IDENTITY, original.profile, str(original.assignment_id)) is None
    box_b.remember(other, IDENTITY)
    for box, managed in [(box_a, original), (box_b, other)]:
        call = box.begin_cached(IDENTITY, managed.profile, str(managed.assignment_id))
        assert call is not None
        box.record(call, "runtime_failed", "CONTROL_PLANE_UNAVAILABLE")
        call.release()
        assert next(fact for fact in box.pending(IDENTITY) if fact["call_id"] == call.id)["reporting_proof"] == managed.reporting_proof


def test_receipt_cache_migration_retains_unbound_receipts_without_assigning_them_to_a_client(tmp_path):
    path = tmp_path / "events.sqlite3"
    managed = lease()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE report_receipts(identity TEXT,profile TEXT,assignment_id TEXT,proof TEXT,report_until INTEGER,PRIMARY KEY(identity,profile,assignment_id))")
        db.execute("INSERT INTO report_receipts VALUES(?,?,?,?,?)", (json.dumps(IDENTITY), managed.profile, str(managed.assignment_id), managed.reporting_proof, managed.issued_at + 7 * 86400))
    box = ManagedEventOutbox(path, runtime_key="owned-client")
    assert box.begin_cached(IDENTITY, managed.profile, str(managed.assignment_id)) is None
    box.remember(managed, IDENTITY)
    with sqlite3.connect(path) as db:
        assert {row[0] for row in db.execute("SELECT runtime_key FROM report_receipts")} == {"", "owned-client"}
    assert box.status()["pending"] == 0
