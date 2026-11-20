"""Parse user-supplied telemetry (JSON or CSV) into validated fleet snapshots.

Parsing is all-or-nothing: every row is validated first, and any error rejects the whole upload.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from .models import ComplianceCheckIn, DeviceIn, FleetSnapshotIn

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_ROWS = 10_000
MAX_DEVICES_PER_SNAPSHOT = 500

CSV_REQUIRED_COLUMNS = (
    "observed_at",
    "device_id",
    "employee_id",
    "device_type",
    "os_name",
    "os_version",
    "disk_free_pct",
    "ram_used_pct",
)


class UploadRejected(Exception):
    """Raised with every validation error found, so the caller can show them all."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("Upload rejected")
        self.errors = errors


class UnsupportedUploadFormat(Exception):
    pass


def _error(row: int | None, message: str) -> dict[str, Any]:
    return {"row": row, "error": message}


def _pydantic_messages(error: ValidationError) -> list[str]:
    return [f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in error.errors()]


def parse_upload(raw: bytes, content_type: str, filename: str, source_name: str) -> list[FleetSnapshotIn]:
    media = content_type.split(";")[0].strip().lower()
    lowered = filename.lower()
    if "json" in media or lowered.endswith(".json"):
        return _parse_json(raw)
    if "csv" in media or lowered.endswith(".csv"):
        return _parse_csv(raw, source_name)
    raise UnsupportedUploadFormat("Upload must be JSON (application/json, .json) or CSV (text/csv, .csv)")


def _parse_json(raw: bytes) -> list[FleetSnapshotIn]:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise UploadRejected([_error(None, "Body is not valid UTF-8 JSON")]) from None
    try:
        snapshot = FleetSnapshotIn.model_validate(data)
    except ValidationError as error:
        raise UploadRejected([_error(None, message) for message in _pydantic_messages(error)]) from None
    return [snapshot]


def _parse_csv(raw: bytes, source_name: str) -> list[FleetSnapshotIn]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UploadRejected([_error(None, "CSV must be UTF-8 encoded")]) from None

    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    missing = [column for column in CSV_REQUIRED_COLUMNS if column not in header]
    if missing:
        raise UploadRejected([_error(None, f"Missing required columns: {', '.join(missing)}")])

    errors: list[dict[str, Any]] = []
    grouped: dict[datetime, list[DeviceIn]] = {}
    seen_in_snapshot: dict[datetime, set[str]] = {}
    data_rows = 0

    for row in reader:
        data_rows += 1
        line = reader.line_num
        if data_rows > MAX_UPLOAD_ROWS:
            errors.append(_error(line, f"More than {MAX_UPLOAD_ROWS} rows; split the file"))
            break

        observed, device, row_errors = _parse_csv_row(row)
        if row_errors:
            errors.extend(_error(line, message) for message in row_errors)
            continue

        assert observed is not None and device is not None
        device_ids = seen_in_snapshot.setdefault(observed, set())
        if device.device_id in device_ids:
            errors.append(_error(line, f"Duplicate device_id {device.device_id} in the same observed_at snapshot"))
            continue
        device_ids.add(device.device_id)
        grouped.setdefault(observed, []).append(device)

    if data_rows == 0:
        errors.append(_error(None, "CSV has no data rows"))
    if errors:
        raise UploadRejected(errors)

    snapshots: list[FleetSnapshotIn] = []
    for observed, devices in grouped.items():
        if len(devices) > MAX_DEVICES_PER_SNAPSHOT:
            raise UploadRejected(
                [_error(None, f"observed_at {observed.isoformat()} has {len(devices)} devices; max is {MAX_DEVICES_PER_SNAPSHOT}")]
            )
        try:
            snapshots.append(FleetSnapshotIn(source=source_name, observed_at=observed, devices=devices))
        except ValidationError as error:
            raise UploadRejected([_error(None, message) for message in _pydantic_messages(error)]) from None
    return snapshots


def _parse_csv_row(row: dict[str, str | None]) -> tuple[datetime | None, DeviceIn | None, list[str]]:
    def cell(name: str) -> str:
        return (row.get(name) or "").strip()

    messages: list[str] = []

    observed_raw = cell("observed_at")
    try:
        observed = datetime.fromisoformat(observed_raw.replace("Z", "+00:00"))
    except ValueError:
        return None, None, [f"observed_at must be ISO 8601, got '{observed_raw}'"]
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)

    numbers: dict[str, float | None] = {}
    for name in ("disk_free_pct", "ram_used_pct", "battery_health_pct"):
        value = cell(name)
        if value == "" and name == "battery_health_pct":
            numbers[name] = None
            continue
        try:
            numbers[name] = float(value)
        except ValueError:
            messages.append(f"{name} must be a number, got '{value}'")

    compliance_raw = cell("compliance_json") or "[]"
    compliance: list[dict[str, Any]] = []
    try:
        parsed = json.loads(compliance_raw)
        if not isinstance(parsed, list):
            messages.append("compliance_json must be a JSON array")
        else:
            compliance = parsed
    except json.JSONDecodeError:
        messages.append("compliance_json is not valid JSON")

    if messages:
        return None, None, messages

    try:
        device = DeviceIn.model_validate(
            {
                "device_id": cell("device_id"),
                "employee_id": cell("employee_id"),
                "device_type": cell("device_type"),
                "os_name": cell("os_name"),
                "os_version": cell("os_version"),
                "disk_free_pct": numbers["disk_free_pct"],
                "ram_used_pct": numbers["ram_used_pct"],
                "battery_health_pct": numbers["battery_health_pct"],
                "compliance": [ComplianceCheckIn.model_validate(item).model_dump() for item in compliance],
            }
        )
    except ValidationError as error:
        return None, None, _pydantic_messages(error)
    return observed, device, []
