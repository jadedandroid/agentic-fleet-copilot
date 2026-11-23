"""The optional local demo account: created only when AFC_DEMO_ACCOUNT_PASSWORD is set, and it can sign in."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

DEMO_PASSWORD = "demo-password-for-tests-123"


def _client(tmp_path) -> TestClient:
    data_dir = tmp_path / "data"
    return TestClient(
        create_app(
            Settings(
                app_name="test-demo",
                cors_origins=["http://localhost:3000"],
                data_dir=data_dir,
                duckdb_path=data_dir / "telemetry.sqlite3",
                sqlite_audit_path=data_dir / "audit.sqlite3",
                jsonl_audit_path=data_dir / "audit.jsonl",
                seed_demo=False,
            )
        )
    )


def test_no_demo_account_unless_the_password_is_set(tmp_path, monkeypatch):
    monkeypatch.delenv("AFC_DEMO_ACCOUNT_PASSWORD", raising=False)
    client = _client(tmp_path)
    response = client.post("/api/v1/auth/login", json={"email": "admin@demo.local", "password": DEMO_PASSWORD})
    assert response.status_code == 401


def test_the_demo_account_signs_in_and_has_sample_devices(tmp_path, monkeypatch):
    monkeypatch.setenv("AFC_DEMO_ACCOUNT_PASSWORD", DEMO_PASSWORD)
    client = _client(tmp_path)
    login = client.post("/api/v1/auth/login", json={"email": "admin@demo.local", "password": DEMO_PASSWORD})
    assert login.status_code == 200, login.text
    assert login.json()["tenant_id"] == "demo" and login.json()["role"] == "admin"
    devices = client.get("/api/v1/fleet/devices", headers={"authorization": f"Bearer {login.json()['token']}"}).json()
    assert len(devices["devices"]) > 0


def test_a_short_demo_password_is_refused_at_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("AFC_DEMO_ACCOUNT_PASSWORD", "short")
    try:
        _client(tmp_path)
    except ValueError as error:
        assert "at least 12" in str(error)
    else:
        raise AssertionError("a short demo password must not start the server")


def test_restarting_with_a_new_demo_password_changes_it(tmp_path, monkeypatch):
    monkeypatch.setenv("AFC_DEMO_ACCOUNT_PASSWORD", DEMO_PASSWORD)
    client = _client(tmp_path)
    assert client.post("/api/v1/auth/login", json={"email": "admin@demo.local", "password": DEMO_PASSWORD}).status_code == 200

    new_password = "a-second-demo-password-456"
    monkeypatch.setenv("AFC_DEMO_ACCOUNT_PASSWORD", new_password)
    restarted = _client(tmp_path)
    assert restarted.post("/api/v1/auth/login", json={"email": "admin@demo.local", "password": new_password}).status_code == 200
    assert restarted.post("/api/v1/auth/login", json={"email": "admin@demo.local", "password": DEMO_PASSWORD}).status_code == 401
