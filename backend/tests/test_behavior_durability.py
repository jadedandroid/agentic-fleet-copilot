"""Behavior under restart and under abuse: durable approvals and login throttling."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ACME = {"x-api-key": "acme-demo-key"}
EMAIL = "lead@durable.test"
PASSWORD = "correct-horse-battery"
QUESTION = "Which devices at Acme are low on disk space?"


def _settings(data_dir) -> Settings:
    return Settings(
        app_name="test-durability",
        cors_origins=["http://localhost:3000"],
        data_dir=data_dir,
        duckdb_path=data_dir / "telemetry.sqlite3",
        sqlite_audit_path=data_dir / "audit.sqlite3",
        jsonl_audit_path=data_dir / "audit.jsonl",
        seed_demo=True,
        bootstrap_admin_key="durability-bootstrap-key",
    )


def _login(client: TestClient, password: str = PASSWORD):
    return client.post("/api/v1/auth/login", json={"email": EMAIL, "password": password})


def _seed_admin(client: TestClient) -> None:
    created = client.post(
        "/api/v1/tenant/users",
        json={"email": EMAIL, "password": PASSWORD, "role": "admin"},
        headers=ACME,
    )
    assert created.status_code == 201, created.text


# --- durable approvals --------------------------------------------------------------------------------


def test_a_pending_approval_survives_a_backend_restart(tmp_path):
    data_dir = tmp_path / "data"
    first = TestClient(create_app(_settings(data_dir)))
    _seed_admin(first)
    proposal = first.post(
        "/api/v1/copilot/query", json={"question": QUESTION, "allow_remediation": True}, headers=ACME
    ).json()
    action_id = proposal["proposed_action"]["action_id"]
    assert proposal["awaiting_approval"] is True

    # A restart is a new app instance over the same data directory.
    restarted = TestClient(create_app(_settings(data_dir)))
    token = _login(restarted).json()["token"]
    approved = restarted.post(
        f"/api/v1/copilot/approvals/{action_id}",
        json={"decision": "approve", "reviewer": "ignored"},
        headers={"authorization": f"Bearer {token}"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["graph_resumed"] is True

    executed = restarted.post(f"/api/v1/copilot/actions/{action_id}/execute", headers=ACME)
    assert executed.status_code == 200
    assert executed.json()["executed"] is True


def test_a_rejected_approval_after_restart_is_still_final(tmp_path):
    data_dir = tmp_path / "data"
    first = TestClient(create_app(_settings(data_dir)))
    proposal = first.post(
        "/api/v1/copilot/query", json={"question": QUESTION, "allow_remediation": True}, headers=ACME
    ).json()
    action_id = proposal["proposed_action"]["action_id"]

    restarted = TestClient(create_app(_settings(data_dir)))
    rejected = restarted.post(
        f"/api/v1/copilot/approvals/{action_id}",
        json={"decision": "reject", "reviewer": "ops-lead"},
        headers=ACME,
    )
    assert rejected.json()["status"] == "rejected"
    assert restarted.post(f"/api/v1/copilot/actions/{action_id}/execute", headers=ACME).status_code == 409


# --- login throttling -----------------------------------------------------------------------------------


def test_repeated_wrong_passwords_lock_the_account_even_for_the_right_password(tmp_path):
    client = TestClient(create_app(_settings(tmp_path / "data")))
    _seed_admin(client)
    for _ in range(5):
        assert _login(client, "wrong-password-here").status_code == 401

    locked = _login(client)  # the correct password is refused while the lock holds
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) > 0


def test_unknown_emails_are_throttled_too(tmp_path):
    client = TestClient(create_app(_settings(tmp_path / "data")))
    for _ in range(5):
        response = client.post("/api/v1/auth/login", json={"email": "nobody@nowhere.test", "password": "x" * 12})
        assert response.status_code == 401
    blocked = client.post("/api/v1/auth/login", json={"email": "nobody@nowhere.test", "password": "x" * 12})
    assert blocked.status_code == 429


def test_a_successful_sign_in_resets_the_failure_count(tmp_path):
    client = TestClient(create_app(_settings(tmp_path / "data")))
    _seed_admin(client)
    for _ in range(4):
        assert _login(client, "wrong-password-here").status_code == 401
    assert _login(client).status_code == 200

    # The reset means four more failures are allowed before the lock, not one.
    for _ in range(4):
        assert _login(client, "wrong-password-here").status_code == 401
    assert _login(client).status_code == 200


def test_a_lock_on_one_email_does_not_block_another(tmp_path):
    client = TestClient(create_app(_settings(tmp_path / "data")))
    _seed_admin(client)
    for _ in range(5):
        _login(client, "wrong-password-here")
    other = client.post("/api/v1/auth/login", json={"email": "someone-else@x.test", "password": "y" * 12})
    assert other.status_code == 401  # rejected for the password, not for a lock


def test_one_source_spraying_many_accounts_is_throttled(tmp_path):
    client = TestClient(create_app(_settings(tmp_path / "data")))
    for index in range(20):
        response = client.post(
            "/api/v1/auth/login", json={"email": f"guess{index}@spray.test", "password": "x" * 12}
        )
        assert response.status_code == 401
    blocked = client.post("/api/v1/auth/login", json={"email": "another@spray.test", "password": "x" * 12})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
