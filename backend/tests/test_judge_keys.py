"""Saved judge keys: encrypted at rest, per user, expiring, deletable. Keys never appear in responses or audit rows."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import Settings
from app.main import create_app
from app.store import TelemetryStore

BOOTSTRAP = "judge-keys-bootstrap"
PASSWORD = "correct-horse-battery"
EMAIL = "judge@keys.test"
OPENAI_KEY = "sk-test-" + "A" * 30
ACME = {"x-api-key": "acme-demo-key"}


@pytest.fixture()
def secret(monkeypatch) -> str:
    value = Fernet.generate_key().decode()
    monkeypatch.setenv("AFC_JUDGE_KEY_SECRET", value)
    return value


def _client(tmp_path) -> TestClient:
    data_dir = tmp_path / "data"
    return TestClient(
        create_app(
            Settings(
                app_name="test-judge-keys",
                cors_origins=["http://localhost:3000"],
                data_dir=data_dir,
                duckdb_path=data_dir / "telemetry.sqlite3",
                sqlite_audit_path=data_dir / "audit.sqlite3",
                jsonl_audit_path=data_dir / "audit.jsonl",
                seed_demo=False,
                bootstrap_admin_key=BOOTSTRAP,
            )
        )
    )


def _user_token(client: TestClient) -> dict[str, str]:
    created = client.post(
        "/api/v1/admin/tenants",
        json={"name": "Keyco", "admin_email": EMAIL, "admin_password": PASSWORD},
        headers={"x-bootstrap-key": BOOTSTRAP},
    )
    assert created.status_code == 201, created.text
    login = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    return {"authorization": f"Bearer {login.json()['token']}"}


def _save(client, headers, key=OPENAI_KEY, ttl_days=30):
    return client.put("/api/v1/judge-key", json={"openai_api_key": key, "ttl_days": ttl_days}, headers=headers)


def test_saving_is_refused_without_a_server_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("AFC_JUDGE_KEY_SECRET", raising=False)
    client = _client(tmp_path)
    headers = _user_token(client)
    response = _save(client, headers)
    assert response.status_code == 503


def test_service_keys_cannot_save_a_judge_key(tmp_path, secret):
    client = _client(tmp_path)
    assert _save(client, ACME).status_code == 403


def test_a_saved_key_is_listed_by_its_last_four_only_and_encrypted_at_rest(tmp_path, secret):
    client = _client(tmp_path)
    headers = _user_token(client)
    saved = _save(client, headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["last4"] == OPENAI_KEY[-4:]
    assert "expires_at" in saved.json()
    assert OPENAI_KEY not in saved.text

    listed = client.get("/api/v1/judge-key", headers=headers).json()
    assert listed["configured"] is True and OPENAI_KEY not in json.dumps(listed)

    database = (tmp_path / "data" / "telemetry.sqlite3").read_bytes()
    assert OPENAI_KEY.encode() not in database, "the plain key must never reach the database file"
    audit = (tmp_path / "data" / "audit.jsonl").read_text()
    assert OPENAI_KEY not in audit


def test_an_expired_key_is_removed_and_reported_unconfigured(tmp_path, secret):
    client = _client(tmp_path)
    headers = _user_token(client)
    _save(client, headers)
    # Expire the saved key directly in the database, then read through the API, which must drop it.
    store = TelemetryStore(tmp_path / "data" / "telemetry.sqlite3")
    user_id = store.get_user_by_email(EMAIL)["user_id"]
    record = store.get_judge_key(user_id)
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    store.save_judge_key(user_id, "keyco", record["ciphertext"], record["last4"], past, past)

    assert client.get("/api/v1/judge-key", headers=headers).json() == {"configured": False}
    assert store.get_judge_key(user_id) is None


def test_deleting_the_saved_key_removes_it(tmp_path, secret):
    client = _client(tmp_path)
    headers = _user_token(client)
    _save(client, headers)
    assert client.delete("/api/v1/judge-key", headers=headers).json() == {"configured": False}
    assert client.get("/api/v1/judge-key", headers=headers).json() == {"configured": False}


def test_the_judge_uses_a_saved_key_when_none_is_sent(tmp_path, secret, monkeypatch):
    client = _client(tmp_path)
    headers = _user_token(client)
    _save(client, headers)
    now = datetime.now(tz=UTC).isoformat()
    client.post(
        "/api/v1/fleet/snapshots",
        json={
            "source": "judge-test",
            "observed_at": now,
            "devices": [
                {
                    "device_id": "keyco-lap-1", "employee_id": "emp-k", "device_type": "laptop",
                    "os_name": "macOS", "os_version": "15.2", "disk_free_pct": 4.0, "ram_used_pct": 40.0,
                    "battery_health_pct": None, "compliance": [],
                }
            ],
        },
        headers=headers,
    )
    used: dict[str, str] = {}

    def fake_judge(samples, api_key=None, write_report=True):
        used["key"] = api_key
        return {"scores": {"faithfulness": 1.0}, "judge_model": "fake", "note": "test"}

    monkeypatch.setattr(main_module, "run_judge", fake_judge)
    response = client.post(
        "/api/v1/evals/grounding-judge",
        json={"question": "Which devices at Keyco are low on disk space?"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert used["key"] == OPENAI_KEY
    assert OPENAI_KEY not in response.text


def test_the_judge_refuses_when_no_key_is_sent_or_saved(tmp_path, secret):
    client = _client(tmp_path)
    headers = _user_token(client)
    response = client.post("/api/v1/evals/grounding-judge", json={"question": "anything"}, headers=headers)
    assert response.status_code == 409
