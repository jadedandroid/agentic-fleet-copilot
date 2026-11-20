from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.audit import AuditLogger
from app.config import Settings
from app.main import create_app

ACME = {"x-api-key": "acme-demo-key"}
GLOBEX = {"x-api-key": "globex-demo-key"}


def _settings(tmp_path) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        app_name="test-agentic-fleet-copilot",
        cors_origins=["http://localhost:3000"],
        data_dir=data_dir,
        duckdb_path=data_dir / "telemetry.sqlite3",
        sqlite_audit_path=data_dir / "audit.sqlite3",
        jsonl_audit_path=data_dir / "audit.jsonl",
    )


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return _settings(tmp_path)


@pytest.fixture()
def client(settings) -> TestClient:
    return TestClient(create_app(settings))


def _query(client, question, headers=ACME, allow_remediation=False):
    response = client.post(
        "/api/v1/copilot/query",
        json={"question": question, "allow_remediation": allow_remediation},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_healthz_is_public(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_every_data_route_requires_a_valid_api_key(client):
    assert client.get("/api/v1/fleet/devices").status_code == 401
    assert client.get("/api/v1/fleet/devices", headers={"x-api-key": "wrong"}).status_code == 401
    assert client.post("/api/v1/copilot/query", json={"question": "x"}).status_code == 401
    assert client.get("/api/v1/copilot/stream", params={"question": "x", "apiKey": "wrong"}).status_code == 401


def test_old_tenant_header_is_no_longer_trusted(client):
    response = client.get("/api/v1/fleet/devices", headers={"x-tenant-id": "acme"})
    assert response.status_code == 401


def test_devices_are_scoped_to_the_key_tenant(client):
    acme_ids = {d["device_id"] for d in client.get("/api/v1/fleet/devices", headers=ACME).json()["devices"]}
    globex_ids = {d["device_id"] for d in client.get("/api/v1/fleet/devices", headers=GLOBEX).json()["devices"]}
    assert acme_ids and globex_ids
    assert acme_ids.isdisjoint(globex_ids)


def test_grounded_disk_answer_cites_evidence_and_is_deterministic(client):
    first = _query(client, "Which devices at Acme are low on disk space?")
    second = _query(client, "Which devices at Acme are low on disk space?")
    assert first["intent"] == "low_disk"
    assert first["answer"] == second["answer"]
    assert first["citations"], "answers must cite telemetry evidence"
    cited = {c["device_id"] for c in first["citations"]}
    assert {"acme-lap-001", "acme-lap-003"} <= cited


def test_compliance_question_returns_failing_devices(client):
    body = _query(client, "Show me laptops failing high-severity compliance checks.")
    assert body["intent"] == "failing_compliance"
    assert body["citations"]


def test_cross_tenant_question_is_refused_without_evidence(client):
    body = _query(client, "Show me Globex laptop data")
    assert body["refusal_reason"].startswith("Cross-tenant access is refused")
    assert body["citations"] == []


def test_unsupported_question_is_refused(client):
    body = _query(client, "What is the weather in Paris?")
    assert body["refusal_reason"].startswith("The question is outside")
    assert body["citations"] == []


def test_trend_question_produces_insights(client):
    body = _query(client, "Batteries approaching end-of-life")
    assert body["intent"] == "battery_eol"
    assert body["insights"], "trend answers must include findings"
    assert {"finding", "evidence", "explanation"} <= set(body["insights"][0])


def test_stream_emits_trace_and_final_events(client):
    response = client.get(
        "/api/v1/copilot/stream",
        params={"question": "Which devices at Acme are low on disk space?", "apiKey": "acme-demo-key"},
    )
    assert response.status_code == 200
    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[0] == "start"
    assert "trace" in names
    assert names[-1] == "final"
    assert events[-1][1]["citations"]


def test_stream_rejects_missing_key_before_streaming(client):
    response = client.get("/api/v1/copilot/stream", params={"question": "anything"})
    assert response.status_code == 401


def test_remediation_requires_explicit_approval_then_executes(client):
    body = _query(client, "Which devices at Acme are low on disk space?", allow_remediation=True)
    assert body["awaiting_approval"] is True
    action = body["proposed_action"]
    assert action["tool_name"] == "create_upgrade_order"
    action_id = action["action_id"]

    early = client.post(f"/api/v1/copilot/actions/{action_id}/execute", headers=ACME)
    assert early.status_code == 409, "execution must not run before approval"

    approved = client.post(
        f"/api/v1/copilot/approvals/{action_id}",
        json={"decision": "approve", "reviewer": "ops-lead"},
        headers=ACME,
    )
    assert approved.status_code == 200
    assert approved.json()["graph_resumed"] is True

    executed = client.post(f"/api/v1/copilot/actions/{action_id}/execute", headers=ACME)
    assert executed.status_code == 200
    assert executed.json()["executed"] is True
    assert executed.json()["result"]["status"] == "queued"

    again = client.post(f"/api/v1/copilot/actions/{action_id}/execute", headers=ACME)
    assert again.json()["result"] == {"status": "already_executed"}


def test_other_tenant_cannot_approve_or_see_action(client):
    body = _query(client, "Which devices at Acme are low on disk space?", allow_remediation=True)
    action_id = body["proposed_action"]["action_id"]
    assert client.post(
        f"/api/v1/copilot/approvals/{action_id}",
        json={"decision": "approve", "reviewer": "mallory"},
        headers=GLOBEX,
    ).status_code == 404
    assert client.post(f"/api/v1/copilot/actions/{action_id}/execute", headers=GLOBEX).status_code == 404


def test_rejected_action_cannot_be_executed_or_reapproved(client):
    body = _query(client, "Which devices at Acme are low on disk space?", allow_remediation=True)
    action_id = body["proposed_action"]["action_id"]
    rejected = client.post(
        f"/api/v1/copilot/approvals/{action_id}",
        json={"decision": "reject", "reviewer": "ops-lead", "comment": "not now"},
        headers=ACME,
    )
    assert rejected.json()["status"] == "rejected"
    assert client.post(f"/api/v1/copilot/actions/{action_id}/execute", headers=ACME).status_code == 409
    assert client.post(
        f"/api/v1/copilot/approvals/{action_id}",
        json={"decision": "approve", "reviewer": "ops-lead"},
        headers=ACME,
    ).status_code == 409


def test_execution_rejects_targets_outside_the_tenant(settings, client):
    # Simulates a tampered or stale payload that points at another tenant's device.
    audit = AuditLogger(settings.sqlite_audit_path, settings.jsonl_audit_path)
    action_id = "act-cross-tenant-test"
    audit.record_action(
        action_id,
        "acme",
        {
            "thread_id": "none",
            "tool_name": "create_upgrade_order",
            "tool_payload": {"device_id": "globex-lap-102", "component": "storage", "spec": "x"},
        },
    )
    audit.resolve_action(action_id, "acme", "approve", "ops-lead", None)
    response = client.post(f"/api/v1/copilot/actions/{action_id}/execute", headers=ACME)
    assert response.status_code == 403


def test_audit_log_records_ingest_and_decisions(client):
    body = _query(client, "Which devices at Acme are low on disk space?", allow_remediation=True)
    client.post(
        f"/api/v1/copilot/approvals/{body['proposed_action']['action_id']}",
        json={"decision": "approve", "reviewer": "ops-lead"},
        headers=ACME,
    )
    events = client.get("/api/v1/audit/events", headers=ACME).json()["events"]
    types = {event["event_type"] for event in events}
    assert {"remediation.proposed", "remediation.decided"} <= types


def _device_payload(device_id: str) -> dict:
    return {
        "source": "mdm-sync",
        "observed_at": "2025-01-01T00:00:00Z",
        "devices": [
            {
                "device_id": device_id,
                "employee_id": "emp-900",
                "device_type": "laptop",
                "os_name": "macOS",
                "os_version": "15.2",
                "disk_free_pct": 4.0,
                "ram_used_pct": 40.0,
                "battery_health_pct": 90.0,
                "compliance": [{"check_id": "filevault", "severity": "high", "passed": True}],
            }
        ],
    }


def test_fleet_ingest_is_tenant_scoped(client):
    response = client.post("/api/v1/fleet/snapshots", json=_device_payload("acme-new-001"), headers=ACME)
    assert response.status_code == 201
    acme_ids = {d["device_id"] for d in client.get("/api/v1/fleet/devices", headers=ACME).json()["devices"]}
    globex_ids = {d["device_id"] for d in client.get("/api/v1/fleet/devices", headers=GLOBEX).json()["devices"]}
    assert "acme-new-001" in acme_ids
    assert "acme-new-001" not in globex_ids


def test_ingest_rejects_unknown_fields(client):
    payload = _device_payload("acme-new-002")
    payload["devices"][0]["tenant_id"] = "globex"
    assert client.post("/api/v1/fleet/snapshots", json=payload, headers=ACME).status_code == 422
