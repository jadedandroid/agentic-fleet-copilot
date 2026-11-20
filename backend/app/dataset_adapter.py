"""Map raw agent telemetry records (NDJSON) onto the fleet snapshot schema.

Every field is mapped explicitly. An unknown model, an unexpected compliance status, or a missing field is an
error that is reported with its line number, so the mapping cannot drift silently. Nothing is dropped.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import DeviceIn, FleetSnapshotIn

SOURCE_NAME = "dataset:device-telemetry"

# Model names seen in the dataset. An unlisted model is an error until it is classified here.
MODEL_DEVICE_TYPES: dict[str, str] = {
    "MacBook Pro": "laptop",
    "MacBook Air": "laptop",
    "ThinkPad X1 Carbon": "laptop",
    "Dell XPS 15": "laptop",
    "Mac mini": "desktop",
}

COMPLIANCE_PASSED: dict[str, bool] = {"pass": True, "fail": False}


@dataclass(frozen=True)
class RecordError:
    line: int
    device_id: str | None
    message: str


class DatasetRejected(Exception):
    def __init__(self, errors: list[RecordError]) -> None:
        super().__init__(f"{len(errors)} record(s) could not be mapped")
        self.errors = errors


@dataclass(frozen=True)
class MappedRecord:
    company_id: str
    collected_at: datetime
    device: DeviceIn


def read_ndjson(path: Path) -> tuple[list[tuple[int, dict[str, Any]]], list[RecordError]]:
    """Parse every non-empty line. Lines that are not JSON objects become errors with their line numbers."""
    records: list[tuple[int, dict[str, Any]]] = []
    errors: list[RecordError] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as error:
                errors.append(RecordError(number, None, f"not valid JSON: {error.msg}"))
                continue
            if not isinstance(parsed, dict):
                errors.append(RecordError(number, None, "record is not a JSON object"))
                continue
            records.append((number, parsed))
    return records, errors


def map_record(record: dict[str, Any]) -> MappedRecord:
    """Map one agent record to a company, a timestamp, and a DeviceIn. Raises KeyError or ValueError on bad data."""
    identity = record["device_identity"]
    model = identity["model_name"]
    if model not in MODEL_DEVICE_TYPES:
        raise ValueError(f"unknown model '{model}'; add it to MODEL_DEVICE_TYPES")

    memory = record["memory"]
    total = memory["total_memory_bytes"]
    if total <= 0:
        raise ValueError("total_memory_bytes must be positive")
    ram_used_pct = round(100.0 * memory["used_memory_bytes"] / total, 2)

    volumes = record["disk_volumes"]
    if not volumes:
        raise ValueError("no disk_volumes reported")
    size = sum(volume["size_bytes"] for volume in volumes)
    if size <= 0:
        raise ValueError("disk size must be positive")
    disk_free_pct = round(100.0 * sum(volume["available_bytes"] for volume in volumes) / size, 2)

    compliance: list[dict[str, Any]] = []
    for check in record["compliance_results"]:
        status = check["status"]
        if status not in COMPLIANCE_PASSED:
            raise ValueError(f"unknown compliance status '{status}' for {check['check_id']}")
        compliance.append({"check_id": check["check_id"], "severity": check["severity"], "passed": COMPLIANCE_PASSED[status]})

    collected = datetime.fromisoformat(record["collected_at"].replace("Z", "+00:00"))
    if collected.tzinfo is None:
        collected = collected.replace(tzinfo=UTC)

    device = DeviceIn.model_validate(
        {
            "device_id": record["device_id"],
            "employee_id": record["employee_id"],
            "device_type": MODEL_DEVICE_TYPES[model],
            "os_name": record["os"]["product_name"],
            "os_version": record["os"]["product_version"],
            "disk_free_pct": disk_free_pct,
            "ram_used_pct": ram_used_pct,
            # Design capacity is not in the dataset, so health cannot be derived. It stays empty rather than guessed.
            "battery_health_pct": None,
            "compliance": compliance,
        }
    )
    return MappedRecord(company_id=record["company_id"], collected_at=collected, device=device)


def build_snapshots(
    records: Iterable[tuple[int, dict[str, Any]]], parse_errors: Iterable[RecordError] = ()
) -> dict[str, list[FleetSnapshotIn]]:
    """Map every record, then group by (company, collected_at). All-or-nothing: any error rejects the dataset."""
    errors: list[RecordError] = list(parse_errors)
    groups: dict[tuple[str, datetime], list[DeviceIn]] = {}

    for line, record in records:
        device_id = record.get("device_id")
        try:
            mapped = map_record(record)
        except (KeyError, TypeError, AttributeError) as error:
            errors.append(RecordError(line, device_id, f"missing or malformed field: {error}"))
            continue
        except (ValueError, ValidationError) as error:
            message = str(error) if isinstance(error, ValueError) else _first_validation_message(error)
            errors.append(RecordError(line, device_id, message))
            continue
        groups.setdefault((mapped.company_id, mapped.collected_at), []).append(mapped.device)

    if errors:
        raise DatasetRejected(errors)

    by_company: dict[str, list[FleetSnapshotIn]] = {}
    for (company, collected), devices in groups.items():
        by_company.setdefault(company, []).append(
            FleetSnapshotIn(source=SOURCE_NAME, observed_at=collected, devices=devices)
        )
    return by_company


def _first_validation_message(error: ValidationError) -> str:
    first = error.errors()[0]
    return f"{'.'.join(str(part) for part in first['loc'])}: {first['msg']}"
