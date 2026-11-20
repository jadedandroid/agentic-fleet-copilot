"""Idempotent demo seeding: a tenant is only seeded when it has no devices yet."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from .fixtures import build_demo_snapshots
from .store import TelemetryStore


def seed_if_empty(store: TelemetryStore, tenants: Iterable[str], now: datetime | None = None) -> list[str]:
    """Seed each listed tenant that has no devices. Returns the tenants that were seeded."""
    seeded: list[str] = []
    for tenant in tenants:
        if store.has_devices(tenant):
            continue
        for snapshot in build_demo_snapshots(tenant, now):
            store.ingest_fleet_snapshot(tenant, snapshot)
        seeded.append(tenant)
    return seeded
