"""Uploads accept fleet snapshots and agent records (the take-home format), as JSON arrays or NDJSON."""

from __future__ import annotations

import copy
import json

import pytest

from app.uploads import UploadRejected, parse_upload


def _agent_record(device_id: str, company: str = "acme-001", collected: str = "2026-05-14T09:54:00Z") -> dict:
    return {
        "device_id": device_id,
        "company_id": company,
        "employee_id": f"emp-{device_id}",
        "collected_at": collected,
        "os": {"platform": "darwin", "product_name": "macOS", "product_version": "14.5"},
        "device_identity": {"serial_number": device_id, "model_name": "MacBook Pro"},
        "memory": {"total_memory_bytes": 100, "used_memory_bytes": 40},
        "disk_volumes": [{"size_bytes": 1000, "available_bytes": 250}],
        "compliance_results": [{"check_id": "disk_encryption", "status": "pass", "severity": "high"}],
    }


def _fleet_snapshot() -> dict:
    return {
        "source": "mdm",
        "observed_at": "2026-05-14T09:54:00Z",
        "devices": [
            {
                "device_id": "fleet-1", "employee_id": "emp-f", "device_type": "laptop", "os_name": "macOS",
                "os_version": "15.2", "disk_free_pct": 50.0, "ram_used_pct": 40.0, "battery_health_pct": None,
                "compliance": [],
            }
        ],
    }


def test_a_json_array_of_agent_records_becomes_snapshots():
    raw = json.dumps([_agent_record("MAC-1"), _agent_record("MAC-2")]).encode()
    snapshots = parse_upload(raw, "application/json", "fleet.json", "upload:fleet.json")
    assert sum(len(s.devices) for s in snapshots) == 2
    assert {d.device_id for s in snapshots for d in s.devices} == {"MAC-1", "MAC-2"}


def test_ndjson_agent_records_are_accepted_one_per_line():
    raw = "\n".join(json.dumps(_agent_record(f"MAC-{i}")) for i in range(3)).encode()
    snapshots = parse_upload(raw, "application/x-ndjson", "data.ndjson", "upload:data.ndjson")
    assert sum(len(s.devices) for s in snapshots) == 3


def test_fleet_snapshots_and_agent_records_can_share_one_file():
    raw = json.dumps([_fleet_snapshot(), _agent_record("MAC-9")]).encode()
    snapshots = parse_upload(raw, "application/json", "mixed.json", "upload:mixed.json")
    assert {d.device_id for s in snapshots for d in s.devices} == {"fleet-1", "MAC-9"}


def test_one_upload_must_hold_one_company():
    raw = json.dumps([_agent_record("MAC-1", company="acme-001"), _agent_record("MAC-2", company="globex-002")]).encode()
    with pytest.raises(UploadRejected) as caught:
        parse_upload(raw, "application/json", "x.json", "upload:x.json")
    assert "one company" in caught.value.errors[0]["error"]


def test_a_bad_ndjson_line_is_reported_with_its_line_number():
    good = json.dumps(_agent_record("MAC-1"))
    raw = f"{good}\n{{not json\n".encode()
    with pytest.raises(UploadRejected) as caught:
        parse_upload(raw, "application/x-ndjson", "x.ndjson", "upload:x.ndjson")
    assert caught.value.errors[0]["row"] == 2


def test_an_unrecognized_record_is_rejected_not_skipped():
    raw = json.dumps([_agent_record("MAC-1"), {"hello": "world"}]).encode()
    with pytest.raises(UploadRejected) as caught:
        parse_upload(raw, "application/json", "x.json", "upload:x.json")
    assert "unrecognized record" in caught.value.errors[0]["error"]


def test_an_agent_record_with_an_unknown_model_is_rejected_with_its_line():
    bad = copy.deepcopy(_agent_record("MAC-1"))
    bad["device_identity"]["model_name"] = "Surface Laptop 9"
    raw = "\n".join([json.dumps(_agent_record("MAC-0")), json.dumps(bad)]).encode()
    with pytest.raises(UploadRejected) as caught:
        parse_upload(raw, "application/x-ndjson", "x.ndjson", "upload:x.ndjson")
    assert caught.value.errors[0]["row"] == 2
    assert "unknown model" in caught.value.errors[0]["error"]
