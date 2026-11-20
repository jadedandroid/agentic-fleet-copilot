"""Reference-time pinning: a query at a fixed as_of is unaffected by rows that arrive later."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ACME = {"x-api-key": "acme-demo-key"}
QUESTION = "Which devices at Acme are low on disk space?"


def _device(disk: float, observed: datetime) -> dict:
    return {
        "source": "as-of-test",
        "observed_at": observed.isoformat(),
        "devices": [
            {
                "device_id": "asof-lap-1",
                "employee_id": "emp-asof",
                "device_type": "laptop",
                "os_name": "macOS",
                "os_version": "15.2",
                "disk_free_pct": disk,
                "ram_used_pct": 40.0,
                "battery_health_pct": None,
                "compliance": [],
            }
        ],
    }


@pytest.fixture()
def client(tmp_path) -> TestClient:
    data_dir = tmp_path / "data"
    settings = Settings(
        app_name="test-as-of",
        cors_origins=["http://localhost:3000"],
        data_dir=data_dir,
        duckdb_path=data_dir / "telemetry.sqlite3",
        sqlite_audit_path=data_dir / "audit.sqlite3",
        jsonl_audit_path=data_dir / "audit.jsonl",
        seed_demo=False,
    )
    return TestClient(create_app(settings))


def _ask(client, as_of: str | None) -> dict:
    payload = {"question": QUESTION, "allow_remediation": False, "context_window_minutes": 14 * 24 * 60}
    if as_of is not None:
        payload["as_of"] = as_of
    response = client.post("/api/v1/copilot/query", json=payload, headers=ACME)
    assert response.status_code == 200, response.text
    return response.json()


def test_rows_after_the_reference_time_do_not_change_a_pinned_answer(client):
    now = datetime.now(tz=UTC)
    client.post("/api/v1/fleet/snapshots", json=_device(80.0, now - timedelta(days=3)), headers=ACME)
    pinned = (now - timedelta(days=1)).isoformat()

    before = _ask(client, pinned)

    # A later, low-disk reading arrives after the pinned time.
    client.post("/api/v1/fleet/snapshots", json=_device(5.0, now - timedelta(hours=2)), headers=ACME)

    after = _ask(client, pinned)
    assert after["answer"] == before["answer"]
    assert after["citations"] == before["citations"]
    # The healthy reading from before the pin is still cited. The later 5% reading must not appear.
    assert all(c.get("value") != 5.0 for c in after["citations"])


def test_the_default_reference_time_sees_the_new_reading(client):
    now = datetime.now(tz=UTC)
    client.post("/api/v1/fleet/snapshots", json=_device(80.0, now - timedelta(days=3)), headers=ACME)
    client.post("/api/v1/fleet/snapshots", json=_device(5.0, now - timedelta(hours=2)), headers=ACME)
    current = _ask(client, None)
    assert "asof-lap-1" in {c["device_id"] for c in current["citations"]}


def test_the_trace_records_the_reference_time_used(client):
    now = datetime.now(tz=UTC)
    client.post("/api/v1/fleet/snapshots", json=_device(80.0, now - timedelta(days=3)), headers=ACME)
    pinned = (now - timedelta(days=1)).replace(microsecond=0).isoformat()
    body = _ask(client, pinned)
    retrieval = next(t for t in body["traces"] if t["stage"] == "retrieval")
    assert retrieval["details"]["as_of"].startswith(pinned[:19])
