"""Idempotent demo seeding: a tenant is only seeded when it has no devices yet."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from uuid import uuid4

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


DEMO_TENANT = "demo"
DEMO_EMAIL = "admin@demo.local"


def ensure_demo_account(store: TelemetryStore, password: str, now: datetime) -> bool:
    """Create a local demo tenant with an admin login and the Acme sample fleet. Returns True when created.

    Runs only when AFC_DEMO_ACCOUNT_PASSWORD is set. Do not enable it on a public server.
    """
    from .identity import hash_password, validate_password

    validate_password(password)
    if store.tenant_exists(DEMO_TENANT) or store.get_user_by_email(DEMO_EMAIL) is not None:
        return False
    store.create_tenant_with_admin(DEMO_TENANT, "Demo", str(uuid4()), DEMO_EMAIL, hash_password(password), now)
    for snapshot in build_demo_snapshots("acme", now):
        store.ingest_fleet_snapshot(DEMO_TENANT, snapshot)
    return True
