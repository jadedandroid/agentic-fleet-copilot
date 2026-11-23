"""Platform operator: sees every tenant's data when it names one; tenants stay inside their own tenant."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

BOOTSTRAP_KEY = "platform-root-key-for-tests"
OPERATOR = {"authorization": f"Bearer {BOOTSTRAP_KEY}"}
ACME = {"x-api-key": "acme-demo-key"}
GLOBEX = {"x-api-key": "globex-demo-key"}


@pytest.fixture()
def client(tmp_path) -> TestClient:
    data_dir = tmp_path / "data"
    settings = Settings(
        app_name="test-operator",
        cors_origins=["http://localhost:3000"],
        data_dir=data_dir,
        duckdb_path=data_dir / "telemetry.sqlite3",
        sqlite_audit_path=data_dir / "audit.sqlite3",
        jsonl_audit_path=data_dir / "audit.jsonl",
        seed_demo=True,
        bootstrap_admin_key=BOOTSTRAP_KEY,
    )
    return TestClient(create_app(settings))


def _devices(client: TestClient, headers: dict[str, str]) -> list[dict]:
    response = client.get("/api/v1/fleet/devices", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["devices"]


def _operator(tenant: str) -> dict[str, str]:
    return {**OPERATOR, "x-tenant-id": tenant}


def test_operator_sees_each_tenant_exactly_as_that_tenant_does(client):
    acme_own = _devices(client, ACME)
    globex_own = _devices(client, GLOBEX)
    assert acme_own and globex_own

    assert _devices(client, _operator("acme")) == acme_own
    assert _devices(client, _operator("globex")) == globex_own


def test_operator_must_name_a_tenant(client):
    response = client.get("/api/v1/fleet/devices", headers=OPERATOR)
    assert response.status_code == 400
    assert "X-Tenant-Id" in response.json()["detail"]


def test_operator_unknown_tenant_is_not_found(client):
    response = client.get("/api/v1/fleet/devices", headers=_operator("no-such-tenant"))
    assert response.status_code == 404


def test_tenant_cannot_name_another_tenant(client):
    response = client.get("/api/v1/fleet/devices", headers={**ACME, "x-tenant-id": "globex"})
    assert response.status_code == 403
    assert "Cross-tenant access is refused" in response.json()["detail"]


def test_tenant_may_name_its_own_tenant(client):
    response = client.get("/api/v1/fleet/devices", headers={**ACME, "x-tenant-id": "acme"})
    assert response.status_code == 200


def test_operator_access_is_written_to_the_tenant_audit_trail(client):
    _devices(client, _operator("acme"))

    events = client.get("/api/v1/audit/events?limit=100", headers=ACME).json()["events"]
    assert "platform.scoped_access" in json.dumps(events)

    # Globex's trail is untouched by a request scoped to acme.
    globex_events = client.get("/api/v1/audit/events?limit=100", headers=GLOBEX).json()["events"]
    assert "platform.scoped_access" not in json.dumps(globex_events)


def test_operator_cannot_manage_tenant_users_or_judge_keys(client):
    users = client.get("/api/v1/tenant/users", headers=_operator("acme"))
    assert users.status_code == 403

    judge = client.get("/api/v1/judge-key", headers=_operator("acme"))
    assert judge.status_code == 403


def test_operator_approval_needs_a_tenant_and_a_real_action(client):
    missing = client.post(
        "/api/v1/copilot/approvals/no-such-action",
        json={"decision": "approve", "reviewer": "x"},
        headers=OPERATOR,
    )
    assert missing.status_code == 400

    unknown = client.post(
        "/api/v1/copilot/approvals/no-such-action",
        json={"decision": "approve", "reviewer": "x"},
        headers=_operator("acme"),
    )
    assert unknown.status_code == 404


def test_operator_copilot_stream_takes_tenant_as_query_param(client):
    response = client.get(
        "/api/v1/copilot/stream",
        params={"question": "Which devices are low on disk space?", "apiKey": BOOTSTRAP_KEY, "tenantId": "acme"},
    )
    assert response.status_code == 200
    assert '"tenant_id": "acme"' in response.text

    missing = client.get(
        "/api/v1/copilot/stream",
        params={"question": "Which devices are low on disk space?", "apiKey": BOOTSTRAP_KEY},
    )
    assert missing.status_code == 400
