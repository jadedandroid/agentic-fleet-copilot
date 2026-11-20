"""Deterministic demo fleets: 15 snapshots per tenant, one every 12 hours, spanning 7 days.

The newest snapshot is 5 minutes before `now`, so default 14-day windows always include them.
Values are pure functions of the snapshot index, so every run produces identical telemetry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .models import ComplianceCheckIn, DeviceIn, FleetSnapshotIn

SNAPSHOT_COUNT = 15
SNAPSHOT_INTERVAL = timedelta(hours=12)
NEWEST_OFFSET = timedelta(minutes=5)
CHECK_SEVERITY = {
    "filevault_enabled": "high",
    "firewall_enabled": "medium",
    "screen_lock_enabled": "medium",
    "edr_agent_running": "high",
}


def _wobble(index: int, amplitude: float) -> float:
    """Deterministic jitter in [-amplitude, +amplitude]."""
    return amplitude * ((index * 7) % 3 - 1)


def _linear(start: float, end: float) -> Callable[[int], float]:
    """Value that moves from `start` at the oldest snapshot to `end` at the newest."""
    return lambda i: start + (end - start) * i / (SNAPSHOT_COUNT - 1)


def _flat(value: float) -> Callable[[int], float]:
    return lambda i: value


@dataclass(frozen=True)
class DeviceSpec:
    device_id: str
    employee_id: str
    device_type: str
    os_name: str
    os_version: str
    disk: Callable[[int], float]
    ram: Callable[[int], float]
    battery: Callable[[int], float] | None = None
    # check_id -> first snapshot index (0 = oldest) where the check fails. 0 means it always fails.
    fails_from: dict[str, int] = field(default_factory=dict)
    # Checks that are recorded for the device but always pass.
    passing: tuple[str, ...] = ()

    def at(self, index: int) -> DeviceIn:
        compliance = [
            ComplianceCheckIn(
                check_id=check_id,
                severity=CHECK_SEVERITY[check_id],
                passed=not (check_id in self.fails_from and index >= self.fails_from[check_id]),
            )
            for check_id in (*self.fails_from.keys(), *self.passing)
        ]
        return DeviceIn(
            device_id=self.device_id,
            employee_id=self.employee_id,
            device_type=self.device_type,  # type: ignore[arg-type]
            os_name=self.os_name,
            os_version=self.os_version,
            disk_free_pct=round(self.disk(index), 2),
            ram_used_pct=round(self.ram(index), 2),
            battery_health_pct=None if self.battery is None else round(self.battery(index), 2),
            compliance=compliance,
        )


ACME_DEVICES: tuple[DeviceSpec, ...] = (
    DeviceSpec(
        "acme-lap-001", "emp-acme-001", "laptop", "macOS", "15.3",
        disk=lambda i: 8.0 + _wobble(i, 1.5), ram=lambda i: 60 + _wobble(i, 3),
        battery=_flat(95), fails_from={"filevault_enabled": 0},
        passing=("firewall_enabled", "screen_lock_enabled", "edr_agent_running"),
    ),
    DeviceSpec(
        "acme-lap-002", "emp-acme-002", "laptop", "macOS", "14.6",
        disk=_flat(40), ram=lambda i: 94 + _wobble(i, 1),
        battery=_linear(92, 84),
        passing=("filevault_enabled", "firewall_enabled", "screen_lock_enabled", "edr_agent_running"),
    ),
    DeviceSpec(
        "acme-lap-003", "emp-acme-003", "laptop", "macOS", "13.6",
        disk=lambda i: 10.0 + _wobble(i, 2), ram=lambda i: 50 + _wobble(i, 3),
        battery=_flat(88), fails_from={"screen_lock_enabled": 0},
        passing=("filevault_enabled", "firewall_enabled", "edr_agent_running"),
    ),
    DeviceSpec(
        "acme-lap-004", "emp-acme-004", "laptop", "macOS", "15.1",
        disk=_flat(55), ram=_flat(45), battery=_flat(97),
        passing=("filevault_enabled", "firewall_enabled", "screen_lock_enabled", "edr_agent_running"),
    ),
    DeviceSpec(
        "acme-lap-005", "emp-acme-005", "laptop", "macOS", "15.2",
        disk=_flat(50), ram=lambda i: 55 + _wobble(i, 2), battery=_flat(93),
        fails_from={"filevault_enabled": 7},
        passing=("firewall_enabled", "screen_lock_enabled", "edr_agent_running"),
    ),
    DeviceSpec(
        "acme-desk-001", "emp-acme-006", "desktop", "macOS", "14.2",
        disk=_flat(30), ram=lambda i: 60 + _wobble(i, 2),
        fails_from={"edr_agent_running": 0},
        passing=("filevault_enabled", "firewall_enabled", "screen_lock_enabled"),
    ),
)

GLOBEX_DEVICES: tuple[DeviceSpec, ...] = (
    DeviceSpec(
        "globex-lap-101", "emp-globex-101", "laptop", "macOS", "15.4",
        disk=_flat(50), ram=_flat(40), battery=_flat(95),
        passing=("filevault_enabled", "firewall_enabled", "screen_lock_enabled", "edr_agent_running"),
    ),
    DeviceSpec(
        "globex-lap-102", "emp-globex-102", "laptop", "macOS", "13.5",
        disk=lambda i: 9.0 + _wobble(i, 1), ram=lambda i: 50 + _wobble(i, 2), battery=_flat(90),
        fails_from={"filevault_enabled": 0},
        passing=("firewall_enabled", "screen_lock_enabled", "edr_agent_running"),
    ),
    DeviceSpec(
        "globex-desk-101", "emp-globex-103", "desktop", "Windows", "11",
        disk=_flat(40), ram=_flat(50),
        passing=("filevault_enabled", "firewall_enabled", "screen_lock_enabled", "edr_agent_running"),
    ),
)

DEMO_FLEETS: dict[str, tuple[DeviceSpec, ...]] = {"acme": ACME_DEVICES, "globex": GLOBEX_DEVICES}


def build_demo_snapshots(tenant_id: str, now: datetime | None = None) -> list[FleetSnapshotIn]:
    """Return the demo snapshots for a tenant in chronological order (oldest first)."""
    if tenant_id not in DEMO_FLEETS:
        raise KeyError(f"No demo fleet defined for tenant '{tenant_id}'")
    reference = now or datetime.now(timezone.utc)
    newest = reference - NEWEST_OFFSET
    snapshots: list[FleetSnapshotIn] = []
    for index in range(SNAPSHOT_COUNT):
        observed = newest - SNAPSHOT_INTERVAL * (SNAPSHOT_COUNT - 1 - index)
        snapshots.append(
            FleetSnapshotIn(
                source=f"demo-{tenant_id}",
                observed_at=observed,
                devices=[spec.at(index) for spec in DEMO_FLEETS[tenant_id]],
            )
        )
    return snapshots
