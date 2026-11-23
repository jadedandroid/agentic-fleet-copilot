"""Tenancy: sign-in, sessions, tenant lifecycle, roles, and user-supplied uploads."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

BOOTSTRAP_KEY = "platform-root-key-for-tests"
BOOT = {"x-bootstrap-key": BOOTSTRAP_KEY}
ACME_KEY = {"x-api-key": "acme-demo-key"}
GLOBEX_KEY = {"x-api-key": "globex-demo-key"}
ADMIN_PASSWORD = "correct-horse-battery"


def _settings(tmp_path, *, bootstrap: str | None = BOOTSTRAP_KEY, ttl_hours: int = 8) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        app_name="test-tenancy",
        cors_origins=["http://localhost:3000"],
        data_dir=data_dir,
        duckdb_path=data_dir / "telemetry.sqlite3",
        sqlite_audit_path=data_dir / "audit.sqlite3",
        jsonl_audit_path=data_dir / "audit.jsonl",
        seed_demo=True,
        bootstrap_admin_key=bootstrap,
        session_ttl_hours=ttl_hours,
    )


@pytest.fixture()
def client(tmp_path) -> TestClient:
    return TestClient(create_app(_settings(tmp_path)))


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _create_tenant(client, name: str, email: str, password: str = ADMIN_PASSWORD) -> dict:
    response = client.post(
        "/api/v1/admin/tenants",
        json={"name": name, "admin_email": email, "admin_password": password},
        headers=BOOT,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _login(client, email: str, password: str = ADMIN_PASSWORD) -> dict:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def _device(device_id: str, employee_id: str = "emp-1") -> dict:
    return {
        "device_id": device_id,
        "employee_id": employee_id,
        "device_type": "laptop",
        "os_name": "macOS",
        "os_version": "15.2",
        "disk_free_pct": 50.0,
        "ram_used_pct": 40.0,
        "battery_health_pct": 90.0,
        "compliance": [{"check_id": "filevault", "severity": "high", "passed": True}],
    }


def _device_count(client, headers) -> int:
    return len(client.get("/api/v1/fleet/devices", headers=headers).json()["devices"])


# --- sign-in ------------------------------------------------------------------------------------


def test_login_returns_a_session_and_me_reports_the_tenant(client):
    _create_tenant(client, "Initech", "ops@initech.test")
    session = _login(client, "ops@initech.test")
    assert session["tenant_id"] == "initech"
    assert session["role"] == "admin"

    me = client.get("/api/v1/auth/me", headers=_bearer(session["token"])).json()
    assert me["tenant_id"] == "initech"
    assert me["auth"] == "session"


def test_wrong_password_and_unknown_email_return_the_same_error(client):
    _create_tenant(client, "Initech", "ops@initech.test")
    wrong = client.post("/api/v1/auth/login", json={"email": "ops@initech.test", "password": "nope-nope-nope"})
    unknown = client.post("/api/v1/auth/login", json={"email": "ghost@initech.test", "password": "nope-nope-nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_session_token_only_sees_its_own_tenant(client):
    _create_tenant(client, "Initech", "ops@initech.test")
    token = _login(client, "ops@initech.test")["token"]
    assert _device_count(client, _bearer(token)) == 0
    assert _device_count(client, ACME_KEY) > 0


def test_logout_revokes_the_session(client):
    _create_tenant(client, "Initech", "ops@initech.test")
    token = _login(client, "ops@initech.test")["token"]
    assert client.post("/api/v1/auth/logout", headers=_bearer(token)).status_code == 200
    assert client.get("/api/v1/auth/me", headers=_bearer(token)).status_code == 401


def test_expired_session_is_rejected(tmp_path):
    client = TestClient(create_app(_settings(tmp_path, ttl_hours=0)))
    _create_tenant(client, "Initech", "ops@initech.test")
    token = _login(client, "ops@initech.test")["token"]
    assert client.get("/api/v1/auth/me", headers=_bearer(token)).status_code == 401


def test_session_tokens_are_not_stored_in_plain_text(tmp_path):
    client = TestClient(create_app(_settings(tmp_path)))
    _create_tenant(client, "Initech", "ops@initech.test")
    token = _login(client, "ops@initech.test")["token"]
    db_bytes = (tmp_path / "data" / "telemetry.sqlite3").read_bytes()
    assert token.encode() not in db_bytes


# --- platform administration ----------------------------------------------------------------------


def test_tenant_creation_requires_the_bootstrap_key(client):
    body = {"name": "Initech", "admin_email": "ops@initech.test", "admin_password": ADMIN_PASSWORD}
    assert client.post("/api/v1/admin/tenants", json=body).status_code == 401
    assert client.post("/api/v1/admin/tenants", json=body, headers={"x-bootstrap-key": "wrong"}).status_code == 401


def test_platform_admin_is_disabled_without_a_configured_key(tmp_path):
    client = TestClient(create_app(_settings(tmp_path, bootstrap=None)))
    body = {"name": "Initech", "admin_email": "ops@initech.test", "admin_password": ADMIN_PASSWORD}
    response = client.post("/api/v1/admin/tenants", json=body, headers={"x-bootstrap-key": "anything"})
    assert response.status_code == 403


def test_tenant_id_is_derived_from_the_name_and_never_reused(client):
    created = _create_tenant(client, "Initech Corp", "ops@initech.test")
    assert created["tenant_id"] == "initech-corp"
    duplicate = client.post(
        "/api/v1/admin/tenants",
        json={"name": "Initech Corp", "admin_email": "other@initech.test", "admin_password": ADMIN_PASSWORD},
        headers=BOOT,
    )
    assert duplicate.status_code == 409


def test_admin_email_must_be_unique_across_the_platform(client):
    _create_tenant(client, "Initech", "ops@initech.test")
    clash = client.post(
        "/api/v1/admin/tenants",
        json={"name": "Hooli", "admin_email": "ops@initech.test", "admin_password": ADMIN_PASSWORD},
        headers=BOOT,
    )
    assert clash.status_code == 409


def test_passwords_shorter_than_twelve_characters_are_refused(client):
    response = client.post(
        "/api/v1/admin/tenants",
        json={"name": "Initech", "admin_email": "ops@initech.test", "admin_password": "abcd"},
        headers=BOOT,
    )
    assert response.status_code == 422


# --- tenant deletion -------------------------------------------------------------------------------


def test_deleting_a_tenant_removes_its_data_and_sessions_but_not_others(client):
    _create_tenant(client, "Initech", "ops@initech.test")
    token = _login(client, "ops@initech.test")["token"]
    upload = client.post(
        "/api/v1/fleet/uploads?filename=fleet.json",
        content=json.dumps(
            {"source": "test", "observed_at": "2026-01-01T00:00:00Z", "devices": [_device("initech-lap-1")]}
        ),
        headers={**_bearer(token), "content-type": "application/json"},
    )
    assert upload.status_code == 201, upload.text
    acme_before = _device_count(client, ACME_KEY)
    globex_before = _device_count(client, GLOBEX_KEY)

    deleted = client.delete("/api/v1/admin/tenants/initech", headers=BOOT)
    assert deleted.status_code == 200
    assert deleted.json()["removed_rows"]["devices"] == 1
    assert deleted.json()["removed_rows"]["users"] == 1

    assert client.get("/api/v1/auth/me", headers=_bearer(token)).status_code == 401
    assert _device_count(client, ACME_KEY) == acme_before
    assert _device_count(client, GLOBEX_KEY) == globex_before


def test_deleted_tenant_keeps_an_audit_tombstone_visible_to_the_platform_only(client):
    _create_tenant(client, "Initech", "ops@initech.test")
    client.delete("/api/v1/admin/tenants/initech", headers=BOOT)

    events = client.get("/api/v1/admin/tenants/initech/audit", headers=BOOT).json()["events"]
    types = {event["event_type"] for event in events}
    assert {"tenant.created", "tenant.deleted"} <= types
    assert client.get("/api/v1/admin/tenants/initech/audit").status_code == 401


def test_deleted_tenant_id_cannot_be_recreated_and_cannot_be_deleted_twice(client):
    _create_tenant(client, "Initech", "ops@initech.test")
    assert client.delete("/api/v1/admin/tenants/initech", headers=BOOT).status_code == 200
    assert client.delete("/api/v1/admin/tenants/initech", headers=BOOT).status_code == 409
    recreate = client.post(
        "/api/v1/admin/tenants",
        json={"name": "Initech", "admin_email": "new@initech.test", "admin_password": ADMIN_PASSWORD},
        headers=BOOT,
    )
    assert recreate.status_code == 409


def test_deleting_a_seeded_tenant_revokes_its_service_key(client):
    assert _device_count(client, ACME_KEY) > 0
    assert client.delete("/api/v1/admin/tenants/acme", headers=BOOT).status_code == 200
    assert client.get("/api/v1/fleet/devices", headers=ACME_KEY).status_code == 401
    assert _device_count(client, GLOBEX_KEY) > 0


def test_deleting_an_unknown_tenant_returns_404(client):
    assert client.delete("/api/v1/admin/tenants/nobody", headers=BOOT).status_code == 404


# --- roles ----------------------------------------------------------------------------------------


def test_analyst_cannot_approve_but_admin_can_and_reviewer_is_the_signed_in_user(client):
    # Service keys are tenant admins. An admin creates an analyst in acme.
    created = client.post(
        "/api/v1/tenant/users",
        json={"email": "analyst@acme.test", "password": ADMIN_PASSWORD, "role": "analyst"},
        headers=ACME_KEY,
    )
    assert created.status_code == 201, created.text
    analyst = _login(client, "analyst@acme.test")["token"]

    body = client.post(
        "/api/v1/copilot/query",
        json={"question": "Which devices at Acme are low on disk space?", "allow_remediation": True},
        headers=ACME_KEY,
    ).json()
    action_id = body["proposed_action"]["action_id"]

    as_analyst = client.post(
        f"/api/v1/copilot/approvals/{action_id}",
        json={"decision": "approve", "reviewer": "someone-else"},
        headers=_bearer(analyst),
    )
    assert as_analyst.status_code == 403

    approved = client.post(
        f"/api/v1/copilot/approvals/{action_id}",
        json={"decision": "approve", "reviewer": "someone-else"},
        headers=ACME_KEY,
    )
    assert approved.status_code == 200
    assert approved.json()["reviewer"] == "someone-else"


def test_session_admin_approval_records_the_signed_in_identity(client):
    client.post(
        "/api/v1/tenant/users",
        json={"email": "lead@acme.test", "password": ADMIN_PASSWORD, "role": "admin"},
        headers=ACME_KEY,
    )
    lead = _login(client, "lead@acme.test")["token"]
    action_id = client.post(
        "/api/v1/copilot/query",
        json={"question": "Which devices at Acme are low on disk space?", "allow_remediation": True},
        headers=ACME_KEY,
    ).json()["proposed_action"]["action_id"]

    response = client.post(
        f"/api/v1/copilot/approvals/{action_id}",
        json={"decision": "approve", "reviewer": "impersonated-name"},
        headers=_bearer(lead),
    )
    assert response.status_code == 200
    assert response.json()["reviewer"] == "lead@acme.test"


def test_analyst_cannot_create_users(client):
    _create_tenant(client, "Initech", "ops@initech.test")
    admin = _login(client, "ops@initech.test")["token"]
    client.post(
        "/api/v1/tenant/users",
        json={"email": "analyst@initech.test", "password": ADMIN_PASSWORD, "role": "analyst"},
        headers=_bearer(admin),
    )
    analyst = _login(client, "analyst@initech.test")["token"]
    response = client.post(
        "/api/v1/tenant/users",
        json={"email": "third@initech.test", "password": ADMIN_PASSWORD, "role": "admin"},
        headers=_bearer(analyst),
    )
    assert response.status_code == 403


def test_tenant_admin_sees_only_its_own_users(client):
    _create_tenant(client, "Initech", "ops@initech.test")
    token = _login(client, "ops@initech.test")["token"]
    users = client.get("/api/v1/tenant/users", headers=_bearer(token)).json()["users"]
    assert [user["email"] for user in users] == ["ops@initech.test"]
    assert "password_hash" not in users[0]


# --- uploads --------------------------------------------------------------------------------------


CSV_HEADER = (
    "observed_at,device_id,employee_id,device_type,os_name,os_version,"
    "disk_free_pct,ram_used_pct,battery_health_pct,compliance_json"
)


def _csv_upload(client, headers, body: str, filename: str = "fleet.csv"):
    return client.post(
        f"/api/v1/fleet/uploads?filename={filename}",
        content=body.encode(),
        headers={**headers, "content-type": "text/csv"},
    )


def test_csv_upload_groups_rows_into_snapshots_for_the_caller_only(client):
    _create_tenant(client, "Initech", "ops@initech.test")
    token = _login(client, "ops@initech.test")["token"]
    acme_before = _device_count(client, ACME_KEY)
    rows = "\n".join(
        [
            CSV_HEADER,
            '2026-01-01T00:00:00Z,init-lap-1,emp-1,laptop,macOS,15.2,12.5,40,88,"[{""check_id"":""filevault"",""severity"":""high"",""passed"":true}]"',
            "2026-01-01T00:00:00Z,init-desk-1,emp-2,desktop,macOS,14.6,70,30,,",
            "2026-01-02T00:00:00Z,init-lap-1,emp-1,laptop,macOS,15.2,9.0,41,88,",
        ]
    )
    response = _csv_upload(client, _bearer(token), rows)
    assert response.status_code == 201, response.text
    assert response.json()["snapshot_count"] == 2
    assert response.json()["device_count"] == 3
    assert _device_count(client, _bearer(token)) == 2
    assert _device_count(client, ACME_KEY) == acme_before


def test_one_bad_row_rejects_the_whole_csv_and_names_the_row(client):
    _create_tenant(client, "Initech", "ops@initech.test")
    token = _login(client, "ops@initech.test")["token"]
    rows = "\n".join(
        [
            CSV_HEADER,
            "2026-01-01T00:00:00Z,init-lap-1,emp-1,laptop,macOS,15.2,12.5,40,88,",
            "2026-01-01T00:00:00Z,init-lap-2,emp-1,laptop,macOS,15.2,not-a-number,40,88,",
        ]
    )
    response = _csv_upload(client, _bearer(token), rows)
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert errors[0]["row"] == 3
    assert "disk_free_pct" in errors[0]["error"]
    assert _device_count(client, _bearer(token)) == 0


def test_csv_missing_required_columns_is_rejected(client):
    _create_tenant(client, "Initech", "ops@initech.test")
    token = _login(client, "ops@initech.test")["token"]
    response = _csv_upload(client, _bearer(token), "observed_at,device_id\n2026-01-01T00:00:00Z,x\n")
    assert response.status_code == 422
    assert "Missing required columns" in response.json()["detail"]["errors"][0]["error"]


def test_duplicate_device_in_one_snapshot_is_rejected(client):
    _create_tenant(client, "Initech", "ops@initech.test")
    token = _login(client, "ops@initech.test")["token"]
    rows = "\n".join(
        [
            CSV_HEADER,
            "2026-01-01T00:00:00Z,init-lap-1,emp-1,laptop,macOS,15.2,12.5,40,88,",
            "2026-01-01T00:00:00Z,init-lap-1,emp-1,laptop,macOS,15.2,12.5,40,88,",
        ]
    )
    response = _csv_upload(client, _bearer(token), rows)
    assert response.status_code == 422
    assert "Duplicate device_id" in response.json()["detail"]["errors"][0]["error"]


def test_json_upload_uses_the_fleet_schema(client):
    _create_tenant(client, "Initech", "ops@initech.test")
    token = _login(client, "ops@initech.test")["token"]
    payload = {"source": "mdm", "observed_at": "2026-01-01T00:00:00Z", "devices": [_device("init-json-1")]}
    response = client.post(
        "/api/v1/fleet/uploads?filename=snap.json",
        content=json.dumps(payload),
        headers={**_bearer(token), "content-type": "application/json"},
    )
    assert response.status_code == 201, response.text
    assert _device_count(client, _bearer(token)) == 1


def test_json_upload_rejects_unknown_fields(client):
    _create_tenant(client, "Initech", "ops@initech.test")
    token = _login(client, "ops@initech.test")["token"]
    payload = {"source": "mdm", "observed_at": "2026-01-01T00:00:00Z", "devices": [_device("x")], "tenant_id": "acme"}
    response = client.post(
        "/api/v1/fleet/uploads",
        content=json.dumps(payload),
        headers={**_bearer(token), "content-type": "application/json"},
    )
    assert response.status_code == 422


def test_upload_with_an_unsupported_type_is_refused(client):
    response = client.post(
        "/api/v1/fleet/uploads?filename=fleet.xlsx",
        content=b"binary",
        headers={**ACME_KEY, "content-type": "application/vnd.ms-excel"},
    )
    assert response.status_code == 415


def test_upload_requires_authentication(client):
    response = _csv_upload(client, {}, CSV_HEADER + "\n")
    assert response.status_code == 401
