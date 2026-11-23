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
    from .identity import hash_password

    # Demo only: a short password is acceptable here. Real accounts keep the 12-character minimum.
    if len(password) < 6:
        raise ValueError("The demo password must be at least 6 characters")
    existing = store.get_user_by_email(DEMO_EMAIL)
    if existing is not None and existing["tenant_id"] == DEMO_TENANT and store.is_tenant_active(DEMO_TENANT):
        # The demo password follows the environment, so it can be changed by restarting with a new value.
        store.set_user_password(existing["user_id"], hash_password(password))
        return False
    if store.tenant_exists(DEMO_TENANT) or existing is not None:
        return False
    store.create_tenant_with_admin(DEMO_TENANT, "Demo", str(uuid4()), DEMO_EMAIL, hash_password(password), now)
    for snapshot in build_demo_snapshots("acme", now):
        store.ingest_fleet_snapshot(DEMO_TENANT, snapshot)
    return True
