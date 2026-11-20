"""Load mapped dataset snapshots through the public API: create tenants, sign in, upload each snapshot.

Works with any client that has httpx-style `post(url, json=..., headers=..., content=...)`, so the same code
serves the CLI script (httpx.Client) and the tests (FastAPI TestClient).
"""

from __future__ import annotations

from typing import Any, Protocol

from .models import FleetSnapshotIn


class HttpClient(Protocol):
    def post(self, url: str, **kwargs: Any) -> Any: ...


def display_name(company: str) -> str:
    """Dataset company IDs look like "acme-001". The display name, used for routing, is "Acme"."""
    return company.split("-")[0].title()


def load_snapshots(
    client: HttpClient,
    snapshots_by_company: dict[str, list[FleetSnapshotIn]],
    bootstrap_key: str,
    admin_password: str,
) -> dict[str, dict[str, Any]]:
    """Create one tenant per company and upload its snapshots. Existing tenants are skipped, never re-uploaded.

    Returns a per-company summary. Skipping an existing tenant keeps reloading idempotent: telemetry is never
    duplicated.
    """
    summary: dict[str, dict[str, Any]] = {}
    for company, snapshots in sorted(snapshots_by_company.items()):
        email = f"admin@{company}.dataset.test"
        created = client.post(
            "/api/v1/admin/tenants",
            json={
                "name": display_name(company),
                "tenant_id": company,
                "admin_email": email,
                "admin_password": admin_password,
            },
            headers={"x-bootstrap-key": bootstrap_key},
        )
        if created.status_code == 409:
            summary[company] = {"status": "skipped", "reason": "tenant already exists", "snapshots": 0, "devices": 0}
            continue
        if created.status_code != 201:
            summary[company] = {"status": "failed", "reason": created.text, "snapshots": 0, "devices": 0}
            continue

        login = client.post("/api/v1/auth/login", json={"email": email, "password": admin_password})
        if login.status_code != 200:
            summary[company] = {"status": "failed", "reason": "login failed after tenant creation", "snapshots": 0, "devices": 0}
            continue
        headers = {"authorization": f"Bearer {login.json()['token']}"}

        uploaded = 0
        devices = 0
        failure: str | None = None
        for index, snapshot in enumerate(snapshots, start=1):
            response = client.post(
                f"/api/v1/fleet/uploads?filename={company}-{index:04d}.json",
                content=snapshot.model_dump_json().encode(),
                headers={**headers, "content-type": "application/json"},
            )
            if response.status_code != 201:
                failure = f"snapshot {index}: {response.status_code} {response.text[:200]}"
                break
            uploaded += 1
            devices += len(snapshot.devices)
        summary[company] = {
            "status": "loaded" if failure is None else "failed",
            "reason": failure or "",
            "snapshots": uploaded,
            "devices": devices,
        }
    return summary
