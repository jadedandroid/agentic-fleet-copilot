"""The agent's explicit loop: a plan before retrieval, and one bounded re-plan when evidence is weak."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ACME = {"x-api-key": "acme-demo-key"}


def _client(tmp_path) -> TestClient:
    data_dir = tmp_path / "data"
    return TestClient(
        create_app(
            Settings(
                app_name="test-loop",
                cors_origins=["http://localhost:3000"],
                data_dir=data_dir,
                duckdb_path=data_dir / "telemetry.sqlite3",
                sqlite_audit_path=data_dir / "audit.sqlite3",
                jsonl_audit_path=data_dir / "audit.jsonl",
                seed_demo=False,
            )
        )
    )


def _ingest_low_disk(client: TestClient, device_id: str, observed: datetime) -> None:
    response = client.post(
        "/api/v1/fleet/snapshots",
        json={
            "source": "loop-test",
            "observed_at": observed.isoformat(),
            "devices": [
                {
                    "device_id": device_id,
                    "employee_id": f"emp-{device_id}",
                    "device_type": "laptop",
                    "os_name": "macOS",
                    "os_version": "15.2",
                    "disk_free_pct": 5.0,
                    "ram_used_pct": 40.0,
                    "battery_health_pct": None,
                    "compliance": [],
                }
            ],
        },
        headers=ACME,
    )
    assert response.status_code == 201, response.text


def _ask(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/copilot/query",
        json={"question": "Which devices at Acme are low on disk space?", "allow_remediation": True},
        headers=ACME,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_plan_is_recorded_before_any_data_is_read(tmp_path):
    client = _client(tmp_path)
    now = datetime.now(tz=UTC)
    _ingest_low_disk(client, "loop-lap-1", now - timedelta(days=2))
    body = _ask(client)
    stages = [trace["stage"] for trace in body["traces"]]
    assert stages.index("plan") < stages.index("retrieval")
    plan = next(trace for trace in body["traces"] if trace["stage"] == "plan")
    assert any("propose an action only if" in step for step in plan["details"]["steps"])


def test_weak_evidence_widens_the_lookback_once_and_then_proposes(tmp_path):
    client = _client(tmp_path)
    now = datetime.now(tz=UTC)
    # One reading inside the 14-day default window, one older reading only visible after widening to 30 days.
    _ingest_low_disk(client, "loop-lap-2", now - timedelta(days=20))
    _ingest_low_disk(client, "loop-lap-2", now - timedelta(days=2))

    body = _ask(client)
    stages = [trace["stage"] for trace in body["traces"]]
    assert stages.count("replan") == 1
    assert stages.index("replan") < len(stages) - 1
    assert body["proposed_action"] is not None
    assert body["proposed_action"]["evidence_score"] >= 0.7
    assert body["awaiting_approval"] is True


def test_a_single_reading_is_re_planned_once_and_then_refused_without_looping(tmp_path):
    client = _client(tmp_path)
    now = datetime.now(tz=UTC)
    _ingest_low_disk(client, "loop-lap-3", now - timedelta(days=2))

    body = _ask(client)
    stages = [trace["stage"] for trace in body["traces"]]
    assert stages.count("replan") == 1, "the loop must be bounded to a single widening"
    assert body["proposed_action"] is None
    assert body["awaiting_approval"] is False
    refusals = [t for t in body["traces"] if t["stage"] == "guardrail" and "Refused to propose" in t["summary"]]
    assert refusals, "after the second pass, weak evidence is refused with a visible trace"
