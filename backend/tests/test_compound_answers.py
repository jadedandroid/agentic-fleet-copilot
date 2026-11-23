"""Factual answers for negated, ranked, and multi-topic questions, checked against a known fleet."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ACME = {"x-api-key": "acme-demo-key"}


def _device(device_id, disk, ram, battery=None, os_version="15.2", compliance_pass=True):
    return {
        "device_id": device_id,
        "employee_id": f"emp-{device_id}",
        "device_type": "laptop",
        "os_name": "macOS",
        "os_version": os_version,
        "disk_free_pct": disk,
        "ram_used_pct": ram,
        "battery_health_pct": battery,
        "compliance": [{"check_id": "filevault", "severity": "high", "passed": compliance_pass}],
    }


FLEET = {
    "low-disk-only": _device("cmp-low-disk-only", disk=5.0, ram=40.0),
    "high-ram-only": _device("cmp-high-ram-only", disk=60.0, ram=95.0),
    "both": _device("cmp-both", disk=4.0, ram=97.0),
    "neither": _device("cmp-neither", disk=70.0, ram=30.0),
}


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> TestClient:
    data_dir = tmp_path_factory.mktemp("compound")
    client = TestClient(
        create_app(
            Settings(
                app_name="test-compound",
                cors_origins=["http://localhost:3000"],
                data_dir=data_dir,
                duckdb_path=data_dir / "telemetry.sqlite3",
                sqlite_audit_path=data_dir / "audit.sqlite3",
                jsonl_audit_path=data_dir / "audit.jsonl",
                seed_demo=False,
            )
        )
    )
    now = datetime.now(tz=UTC)
    for hours_ago in (3, 2, 1):
        observed = (now - timedelta(hours=hours_ago)).isoformat()
        client.post(
            "/api/v1/fleet/snapshots",
            json={"source": "compound-test", "observed_at": observed, "devices": list(FLEET.values())},
            headers=ACME,
        )
    return client


def _ask(client, question):
    response = client.post(
        "/api/v1/copilot/query", json={"question": question, "allow_remediation": True}, headers=ACME
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_negated_low_disk_lists_only_devices_that_are_not_low(client):
    body = _ask(client, "Which devices are NOT low on disk space?")
    assert body["refusal_reason"] is None
    assert set(body["insights"][0]["device_ids"]) == {"cmp-high-ram-only", "cmp-neither"}
    assert "cmp-low-disk-only" not in body["answer"] and "cmp-both" not in body["answer"]
    assert body["proposed_action"] is None, "a 'not' answer is informational and must never propose an action"


def test_ranking_by_ram_returns_the_highest_devices_in_order(client):
    body = _ask(client, "top 2 devices by memory usage")
    assert body["refusal_reason"] is None
    assert body["insights"][0]["device_ids"] == ["cmp-both", "cmp-high-ram-only"]
    assert body["proposed_action"] is None


def test_ranking_by_least_free_storage_puts_the_most_constrained_first(client):
    body = _ask(client, "which device has the least free storage")
    assert body["insights"][0]["device_ids"][0] == "cmp-both"


def test_conjunction_lists_only_devices_that_match_every_condition(client):
    body = _ask(client, "devices low on disk and high ram")
    assert body["insights"][0]["device_ids"] == ["cmp-both"]
    assert body["proposed_action"] is None


def test_every_device_in_a_compound_answer_is_cited(client):
    body = _ask(client, "devices low on disk and high ram")
    cited = {c["device_id"] for c in body["citations"]}
    assert set(body["insights"][0]["device_ids"]) <= cited


def test_negated_battery_question_is_refused_when_no_battery_is_reported(client):
    body = _ask(client, "which batteries are never end-of-life")
    assert body["refusal_reason"].startswith("Battery health is not reported")
    assert body["insights"] == []


def test_negated_compliance_lists_every_device_when_none_fail(client):
    # Every device in this fleet passes its high-severity check, so "not failing" must list all of them.
    body = _ask(client, "which devices are NOT failing high-severity compliance checks")
    assert body["refusal_reason"] is None
    assert set(body["insights"][0]["device_ids"]) == set(d["device_id"] for d in FLEET.values())
    assert body["proposed_action"] is None
