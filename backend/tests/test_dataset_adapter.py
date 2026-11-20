"""Dataset adapter: explicit mapping, and every unexpected value is reported rather than dropped."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.dataset_adapter import DatasetRejected, build_snapshots, map_record, read_ndjson


def _record(**overrides) -> dict:
    base = {
        "device_id": "MAC-1",
        "company_id": "acme-001",
        "employee_id": "emp-1",
        "collected_at": "2026-05-14T09:54:00Z",
        "agent_version": "42.0.0",
        "os": {"platform": "darwin", "product_name": "macOS", "product_version": "14.5"},
        "device_identity": {"serial_number": "MAC-1", "model_name": "MacBook Pro"},
        "memory": {"total_memory_bytes": 100, "used_memory_bytes": 40, "free_memory_bytes": 60},
        "disk_volumes": [
            {"volume_name": "Macintosh HD", "size_bytes": 1000, "available_bytes": 250},
            {"volume_name": "Data", "size_bytes": 1000, "available_bytes": 250},
        ],
        "battery": {"battery_present": True, "percentage": 80, "cycle_count": 100, "full_charge_capacity": 5000},
        "compliance_results": [
            {"check_id": "disk_encryption", "status": "pass", "severity": "high"},
            {"check_id": "os_up_to_date", "status": "fail", "severity": "medium"},
        ],
    }
    base.update(overrides)
    return base


def test_macos_record_maps_percentages_and_compliance():
    mapped = map_record(_record())
    device = mapped.device
    assert mapped.company_id == "acme-001"
    assert device.device_type == "laptop"
    assert device.os_name == "macOS" and device.os_version == "14.5"
    assert device.ram_used_pct == 40.0
    assert device.disk_free_pct == 25.0  # 500 available of 2000 total across volumes
    assert [(c.check_id, c.passed) for c in device.compliance] == [("disk_encryption", True), ("os_up_to_date", False)]


def test_missing_battery_leaves_health_empty_rather_than_guessing():
    record = _record(battery=None)
    assert map_record(record).device.battery_health_pct is None


def test_windows_record_keeps_os_and_maps_desktop_model():
    record = _record(
        device_id="WIN-1",
        os={"platform": "win32", "product_name": "Windows", "product_version": "11 23H2"},
        device_identity={"serial_number": "WIN-1", "model_name": "Mac mini"},
    )
    device = map_record(record).device
    assert device.os_name == "Windows" and device.os_version == "11 23H2"
    assert device.device_type == "desktop"


def test_unknown_model_is_reported_not_guessed():
    record = _record(device_identity={"serial_number": "X", "model_name": "Surface Laptop 9"})
    with pytest.raises(ValueError, match="unknown model"):
        map_record(record)


def test_unknown_compliance_status_is_reported():
    record = _record()
    record["compliance_results"][0]["status"] = "unknown"
    with pytest.raises(ValueError, match="unknown compliance status"):
        map_record(record)


def test_missing_field_is_reported_with_its_line_number():
    record = _record()
    del record["memory"]
    with pytest.raises(DatasetRejected) as caught:
        build_snapshots([(7, record)])
    assert caught.value.errors[0].line == 7
    assert "memory" in caught.value.errors[0].message


def test_one_bad_record_rejects_the_whole_dataset():
    good = _record()
    bad = copy.deepcopy(_record(device_id="MAC-2"))
    bad["disk_volumes"] = []
    with pytest.raises(DatasetRejected) as caught:
        build_snapshots([(1, good), (2, bad)])
    assert len(caught.value.errors) == 1
    assert caught.value.errors[0].device_id == "MAC-2"


def test_records_sharing_a_timestamp_form_one_snapshot_per_company():
    first = _record(device_id="MAC-1")
    second = _record(device_id="MAC-2")
    other_company = _record(device_id="MAC-3", company_id="globex-002")
    grouped = build_snapshots([(1, first), (2, second), (3, other_company)])
    assert len(grouped["acme-001"]) == 1 and len(grouped["acme-001"][0].devices) == 2
    assert len(grouped["globex-002"]) == 1


def test_malformed_json_line_is_reported_with_its_line_number(tmp_path: Path):
    path = tmp_path / "data.ndjson"
    path.write_text(json.dumps(_record()) + "\n{not json\n", encoding="utf-8")
    records, parse_errors = read_ndjson(path)
    assert len(records) == 1
    assert parse_errors[0].line == 2
    with pytest.raises(DatasetRejected):
        build_snapshots(records, parse_errors)
