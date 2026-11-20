"""Oracle evaluation on the real device telemetry dataset.

Expected answers are computed directly from the NDJSON file, with the thresholds restated here, so the copilot is
never used to grade itself. Every check is deterministic. The suite is skipped when the dataset file is absent.

Known limit: the oracle and the analytics share the same definitions (latest reading, 75% sustained share, and so
on). This checks the implementation against the written definitions, not whether the definitions are right.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.dataset_adapter import build_snapshots, read_ndjson
from app.dataset_loader import load_snapshots
from app.main import create_app

DATASET = Path(__file__).resolve().parents[2] / "device-telemetry-dataset.ndjson"
BOOTSTRAP = "dataset-eval-bootstrap-key"
ADMIN_PASSWORD = "dataset-eval-password-123"
ACME = "acme-001"
GLOBEX = "globex-002"
AS_OF = "2026-06-12T12:00:00Z"  # just after the last record, so the 30-day window covers the whole dataset
WINDOW_MINUTES = 43200

# Definitions restated from the take-home brief and the analytics thresholds.
DISK_LOW_PCT = 15.0
RAM_HIGH_PCT = 90.0
SUSTAINED_SHARE = 0.75
MIN_TREND_SAMPLES = 3
DRIFT_DELTA = 0.2
MIN_DRIFT_SAMPLES = 4
MACOS_MIN_MAJOR = 15
LAPTOP_MODELS = {"MacBook Pro", "MacBook Air", "ThinkPad X1 Carbon", "Dell XPS 15"}

pytestmark = pytest.mark.skipif(not DATASET.exists(), reason=f"dataset not found at {DATASET}")


# --- oracle: computed from the raw file, not from the copilot -------------------------------------------------


def _disk_free_pct(record: dict[str, Any]) -> float:
    volumes = record["disk_volumes"]
    return 100.0 * sum(v["available_bytes"] for v in volumes) / sum(v["size_bytes"] for v in volumes)


def _ram_used_pct(record: dict[str, Any]) -> float:
    memory = record["memory"]
    return 100.0 * memory["used_memory_bytes"] / memory["total_memory_bytes"]


def _major(version: str) -> int:
    return int(version.split(".")[0])


def _by_device(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["device_id"]].append(record)
    for series in grouped.values():
        series.sort(key=lambda r: r["collected_at"])
    return grouped


def _latest(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {device: series[-1] for device, series in _by_device(records).items()}


def oracle_low_disk(records: list[dict[str, Any]]) -> set[str]:
    return {d for d, r in _latest(records).items() if _disk_free_pct(r) < DISK_LOW_PCT}


def oracle_failing_high_compliance_laptops(records: list[dict[str, Any]]) -> set[str]:
    hits = set()
    for device, record in _latest(records).items():
        if record["device_identity"]["model_name"] not in LAPTOP_MODELS:
            continue
        if any(c["severity"] == "high" and c["status"] == "fail" for c in record["compliance_results"]):
            hits.add(device)
    return hits


def oracle_macos_older_than_15(records: list[dict[str, Any]]) -> set[str]:
    return {
        d
        for d, r in _latest(records).items()
        if r["os"]["product_name"] == "macOS" and _major(r["os"]["product_version"]) < MACOS_MIN_MAJOR
    }


def _sustained(records: list[dict[str, Any]], measure, over) -> set[str]:
    hits = set()
    for device, series in _by_device(records).items():
        if len(series) < MIN_TREND_SAMPLES:
            continue
        share = sum(1 for r in series if over(measure(r))) / len(series)
        if share >= SUSTAINED_SHARE:
            hits.add(device)
    return hits


def oracle_consistently_low_storage(records: list[dict[str, Any]]) -> set[str]:
    return _sustained(records, _disk_free_pct, lambda v: v < DISK_LOW_PCT)


def oracle_consistently_ram_constrained(records: list[dict[str, Any]]) -> set[str]:
    return _sustained(records, _ram_used_pct, lambda v: v >= RAM_HIGH_PCT)


def oracle_compliance_drift(records: list[dict[str, Any]]) -> set[str]:
    hits = set()
    per_check: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for device, series in _by_device(records).items():
        for record in series:
            for check in record["compliance_results"]:
                per_check[(device, check["check_id"])].append(check["status"] == "pass")
    for (device, _check), passes in per_check.items():
        if len(passes) < MIN_DRIFT_SAMPLES:
            continue
        half = len(passes) // 2
        old_fail = sum(1 for p in passes[:half] if not p) / half
        new_fail = sum(1 for p in passes[half:] if not p) / (len(passes) - half)
        if new_fail - old_fail >= DRIFT_DELTA:
            hits.add(device)
    return hits


# --- fixtures and helpers --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def raw_records() -> list[dict[str, Any]]:
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> TestClient:
    data_dir = tmp_path_factory.mktemp("dataset-eval")
    settings = Settings(
        app_name="dataset-eval",
        cors_origins=["http://localhost:3000"],
        data_dir=data_dir,
        duckdb_path=data_dir / "telemetry.sqlite3",
        sqlite_audit_path=data_dir / "audit.sqlite3",
        jsonl_audit_path=data_dir / "audit.jsonl",
        seed_demo=False,
        bootstrap_admin_key=BOOTSTRAP,
    )
    client = TestClient(create_app(settings))
    records, parse_errors = read_ndjson(DATASET)
    snapshots = build_snapshots(records, parse_errors)
    summary = load_snapshots(client, snapshots, BOOTSTRAP, ADMIN_PASSWORD)
    assert all(item["status"] == "loaded" for item in summary.values()), summary
    return client


def _token(client: TestClient, company: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": f"admin@{company}.dataset.test", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"authorization": f"Bearer {response.json()['token']}"}


def _ask(client: TestClient, company: str, question: str, as_of: str = AS_OF) -> dict[str, Any]:
    response = client.post(
        "/api/v1/copilot/query",
        json={"question": question, "as_of": as_of, "context_window_minutes": WINDOW_MINUTES, "allow_remediation": True},
        headers=_token(client, company),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _devices(body: dict[str, Any]) -> set[str]:
    """Devices the answer actually flags. "scope checked" citations only show what was examined, so they are excluded."""
    devices = {
        c["device_id"]
        for c in body.get("citations", [])
        if c.get("device_id") and c.get("reason") != "scope checked"
    }
    for insight in body.get("insights", []):
        devices.update(insight.get("device_ids", []))
    return devices


def _acme_records(raw_records):
    return [r for r in raw_records if r["company_id"] == ACME]


# --- take-home examples ----------------------------------------------------------------------------------------


def test_low_disk_at_acme_matches_the_oracle(client, raw_records):
    body = _ask(client, ACME, "Which devices at Acme are low on disk space?")
    assert body["intent"] == "low_disk"
    assert _devices(body) == oracle_low_disk(_acme_records(raw_records))


def test_low_disk_citations_carry_the_raw_values(client, raw_records):
    body = _ask(client, ACME, "Which devices at Acme are low on disk space?")
    latest = _latest(_acme_records(raw_records))
    assert body["citations"], "answers must cite evidence"
    for citation in body["citations"]:
        expected = _disk_free_pct(latest[citation["device_id"]])
        assert citation["metric"] == "disk_free_pct"
        assert abs(citation["value"] - expected) <= 0.01, citation


def test_laptops_failing_high_severity_compliance_match_the_oracle(client, raw_records):
    body = _ask(client, ACME, "Show me laptops failing high-severity compliance checks.")
    assert _devices(body) == oracle_failing_high_compliance_laptops(_acme_records(raw_records))


def test_macos_older_than_15_matches_the_oracle(client, raw_records):
    body = _ask(client, ACME, "How many devices are running an OS older than macOS 15?")
    assert body["intent"] == "os_age"
    assert _devices(body) == oracle_macos_older_than_15(_acme_records(raw_records))


# --- insight and trend examples --------------------------------------------------------------------------------


def test_consistently_low_storage_matches_the_oracle(client, raw_records):
    body = _ask(client, ACME, "Devices consistently low on storage")
    assert _devices(body) == oracle_consistently_low_storage(_acme_records(raw_records))


def test_consistently_ram_constrained_matches_the_oracle(client, raw_records):
    body = _ask(client, ACME, "Devices consistently constrained by RAM")
    assert _devices(body) == oracle_consistently_ram_constrained(_acme_records(raw_records))


def test_compliance_drift_matches_the_oracle(client, raw_records):
    body = _ask(client, ACME, "Compliance drift over time")
    assert body["intent"] == "compliance_drift"
    assert _devices(body) == oracle_compliance_drift(_acme_records(raw_records))


def test_battery_question_is_refused_when_health_is_not_reported(client):
    # The dataset has no design capacity, so battery health is never reported. The copilot must refuse, not claim
    # that no battery is failing.
    body = _ask(client, ACME, "Batteries approaching end-of-life")
    assert body["refusal_reason"].startswith("Battery health is not reported")
    assert body["insights"] == []
    assert body["proposed_action"] is None


# --- guardrails and reproducibility on real data ----------------------------------------------------------------


def test_cross_tenant_question_is_refused_on_the_dataset(client):
    body = _ask(client, ACME, "Show me Globex laptop data")
    assert body["refusal_reason"]
    assert body["citations"] == []


def test_a_reference_time_before_the_data_refuses_instead_of_guessing(client):
    body = _ask(client, ACME, "Which devices at Acme are low on disk space?", as_of="2026-01-01T00:00:00Z")
    assert body["refusal_reason"]
    assert body["proposed_action"] is None


def test_the_same_question_and_as_of_give_identical_answers(client):
    first = _ask(client, ACME, "Which devices at Acme are low on disk space?")
    second = _ask(client, ACME, "Which devices at Acme are low on disk space?")
    assert first["answer"] == second["answer"]
    assert first["citations"] == second["citations"]


def test_citations_name_the_employee_who_holds_the_device(client, raw_records):
    body = _ask(client, ACME, "Which devices at Acme are low on disk space?")
    latest = _latest(_acme_records(raw_records))
    assert body["citations"]
    for citation in body["citations"]:
        assert citation["employee_id"] == latest[citation["device_id"]]["employee_id"], citation


def test_answers_name_the_company_whose_fleet_they_describe(client):
    body = _ask(client, ACME, "Which devices at Acme are low on disk space?")
    assert body["company"] == "Acme"
