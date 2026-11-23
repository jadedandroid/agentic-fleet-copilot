"""Companies uploaded into a tenant are that tenant's own. Other tenants' companies are still refused."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.intent import mentions_other_tenant, parse_query
from app.main import create_app

IDENTITIES = [
    ("t1", "t1", "Tenant One"),
    ("t1", "acme-001", "Acme"),
    ("t2", "t2", "Tenant Two"),
    ("t2", "globex-002", "Globex"),
]


def test_a_tenants_own_company_is_not_cross_tenant():
    assert mentions_other_tenant("Which devices at Acme are low on disk space?", "t1", IDENTITIES) is False


def test_another_tenants_company_is_cross_tenant():
    assert mentions_other_tenant("Show me Globex laptop data", "t1", IDENTITIES) is True


def test_a_misspelled_rival_company_is_still_caught():
    assert mentions_other_tenant("disk space at Globx", "t1", IDENTITIES) is True


def test_the_router_routes_own_company_questions_normally():
    assert parse_query("Which devices at Acme are low on disk space?", "t1", IDENTITIES).label == "low_disk"
    assert parse_query("Which devices at Acme are low on disk space?", "t2", IDENTITIES).label == "cross_tenant"


def test_two_item_tenant_pairs_still_work():
    assert parse_query("Show me Globex laptop data", "acme-001", [("acme-001", "Acme"), ("globex-002", "Globex")]).label == "cross_tenant"


@pytest.fixture()
def client(tmp_path) -> TestClient:
    data_dir = tmp_path / "data"
    return TestClient(
        create_app(
            Settings(
                app_name="test-ownership",
                cors_origins=["http://localhost:3000"],
                data_dir=data_dir,
                duckdb_path=data_dir / "telemetry.sqlite3",
                sqlite_audit_path=data_dir / "audit.sqlite3",
                jsonl_audit_path=data_dir / "audit.jsonl",
                seed_demo=False,
                bootstrap_admin_key="ownership-bootstrap",
            )
        )
    )


def _tenant_with_admin(client, name: str, email: str) -> dict[str, str]:
    created = client.post(
        "/api/v1/admin/tenants",
        json={"name": name, "admin_email": email, "admin_password": "pass12345"},
        headers={"x-bootstrap-key": "ownership-bootstrap"},
    )
    assert created.status_code == 201, created.text
    login = client.post("/api/v1/auth/login", json={"email": email, "password": "pass12345"})
    return {"authorization": f"Bearer {login.json()['token']}"}


def test_end_to_end_a_company_uploaded_into_a_tenant_becomes_its_own(client):
    owner = _tenant_with_admin(client, "Owner Co", "owner@own.test")
    other = _tenant_with_admin(client, "Other Co", "other@own.test")
    from datetime import UTC, datetime

    record = {
        "device_id": "OWN-1", "company_id": "acme-001", "employee_id": "emp-own",
        "collected_at": datetime.now(tz=UTC).isoformat(),
        "os": {"platform": "darwin", "product_name": "macOS", "product_version": "14.5"},
        "device_identity": {"serial_number": "OWN-1", "model_name": "MacBook Pro"},
        "memory": {"total_memory_bytes": 100, "used_memory_bytes": 40},
        "disk_volumes": [{"size_bytes": 1000, "available_bytes": 50}],
        "compliance_results": [{"check_id": "disk_encryption", "status": "pass", "severity": "high"}],
    }
    upload = client.post(
        "/api/v1/fleet/uploads?filename=acme.json",
        content=json.dumps([record]).encode(),
        headers={**owner, "content-type": "application/json"},
    )
    assert upload.status_code == 201, upload.text

    question = {"question": "Which devices at Acme are low on disk space?", "allow_remediation": False}
    own_answer = client.post("/api/v1/copilot/query", json=question, headers=owner).json()
    assert own_answer["refusal_reason"] is None, "the tenant's own company must be answered"

    rival_answer = client.post("/api/v1/copilot/query", json=question, headers=other).json()
    assert rival_answer["refusal_reason"].startswith("Cross-tenant access is refused")
