from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from uuid import uuid4

try:
    import duckdb
except ModuleNotFoundError:  # pragma: no cover - exercised via runtime environment
    duckdb = None  # type: ignore[assignment]

from typing import Any

from .models import FleetSnapshotIn, TelemetrySnapshotIn

TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

# Fleet metrics persisted per device for each ingested snapshot.
FLEET_METRIC_FIELDS = ("disk_free_pct", "ram_used_pct", "battery_health_pct")


def _ts(value: datetime) -> str:
    """Serialize a datetime as a fixed-width UTC string.

    All writes and cutoff comparisons use this one format so that lexicographic
    ordering in SQLite matches chronological ordering.
    """
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime(TS_FORMAT)


@dataclass(frozen=True)
class SnapshotPointRecord:
    snapshot_id: str
    metric: str
    value: float
    observed_at: datetime


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_id: str
    source: str
    captured_at: datetime


@dataclass(frozen=True)
class DeviceRecord:
    tenant_id: str
    device_id: str
    employee_id: str
    device_type: str
    os_name: str
    os_version: str
    snapshot_id: str
    updated_at: datetime


@dataclass(frozen=True)
class MetricRecord:
    snapshot_id: str
    device_id: str
    metric: str
    value: float
    observed_at: datetime


@dataclass(frozen=True)
class ComplianceRecord:
    snapshot_id: str
    device_id: str
    check_id: str
    severity: str
    passed: bool
    observed_at: datetime


class TelemetryStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        if duckdb is not None:
            self._conn = duckdb.connect(str(db_path))
            self._dialect = "duckdb"
        else:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._dialect = "sqlite"
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telemetry_snapshots (
                    snapshot_id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    source VARCHAR NOT NULL,
                    captured_at TIMESTAMP NOT NULL,
                    tags_json VARCHAR NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telemetry_points (
                    point_id VARCHAR PRIMARY KEY,
                    snapshot_id VARCHAR NOT NULL,
                    tenant_id VARCHAR NOT NULL,
                    metric VARCHAR NOT NULL,
                    value DOUBLE NOT NULL,
                    observed_at TIMESTAMP NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tenant_metric_time ON telemetry_points(tenant_id, metric, observed_at)"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    tenant_id VARCHAR NOT NULL,
                    device_id VARCHAR NOT NULL,
                    employee_id VARCHAR NOT NULL,
                    device_type VARCHAR NOT NULL,
                    os_name VARCHAR NOT NULL,
                    os_version VARCHAR NOT NULL,
                    snapshot_id VARCHAR NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (tenant_id, device_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS device_metrics (
                    point_id VARCHAR PRIMARY KEY,
                    snapshot_id VARCHAR NOT NULL,
                    tenant_id VARCHAR NOT NULL,
                    device_id VARCHAR NOT NULL,
                    metric VARCHAR NOT NULL,
                    value DOUBLE NOT NULL,
                    observed_at TIMESTAMP NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_device_metrics_lookup ON device_metrics(tenant_id, observed_at)"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS compliance_results (
                    result_id VARCHAR PRIMARY KEY,
                    snapshot_id VARCHAR NOT NULL,
                    tenant_id VARCHAR NOT NULL,
                    device_id VARCHAR NOT NULL,
                    check_id VARCHAR NOT NULL,
                    severity VARCHAR NOT NULL,
                    passed BOOLEAN NOT NULL,
                    observed_at TIMESTAMP NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_compliance_lookup ON compliance_results(tenant_id, observed_at)"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    tenant_id VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL,
                    deleted_at VARCHAR
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    email VARCHAR NOT NULL UNIQUE,
                    password_hash VARCHAR NOT NULL,
                    role VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_companies (
                    tenant_id VARCHAR NOT NULL,
                    company_id VARCHAR NOT NULL,
                    PRIMARY KEY (tenant_id, company_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS judge_keys (
                    user_id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR NOT NULL,
                    ciphertext VARCHAR NOT NULL,
                    last4 VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL,
                    expires_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash VARCHAR PRIMARY KEY,
                    user_id VARCHAR NOT NULL,
                    tenant_id VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL,
                    expires_at VARCHAR NOT NULL
                )
                """
            )
            if self._dialect == "sqlite":
                self._conn.commit()

    @staticmethod
    def _normalize_datetime(value: datetime | str) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _window(window_minutes: int, as_of: datetime | None) -> tuple[str, str]:
        """Inclusive [start, end] bounds. `end` is the query's reference time, so later rows are never read."""
        end = as_of if as_of is not None else datetime.now(tz=UTC)
        return _ts(end - timedelta(minutes=window_minutes)), _ts(end)

    def _commit(self) -> None:
        if self._dialect == "sqlite":
            self._conn.commit()

    def insert_snapshot(self, tenant_id: str, snapshot: TelemetrySnapshotIn) -> str:
        snapshot_id = str(uuid4())
        captured_at = max(point.observed_at for point in snapshot.points)

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO telemetry_snapshots (snapshot_id, tenant_id, source, captured_at, tags_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                [snapshot_id, tenant_id, snapshot.source, _ts(captured_at), json.dumps(snapshot.tags)],
            )
            rows = [
                [str(uuid4()), snapshot_id, tenant_id, point.metric, point.value, _ts(point.observed_at)]
                for point in snapshot.points
            ]
            self._conn.executemany(
                """
                INSERT INTO telemetry_points (point_id, snapshot_id, tenant_id, metric, value, observed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._commit()
        return snapshot_id

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Run the enclosed writes atomically. Holds the store lock for the whole block."""
        with self._lock:
            self._conn.execute("BEGIN TRANSACTION")
            try:
                yield
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def ingest_fleet_snapshot(self, tenant_id: str, snapshot: FleetSnapshotIn) -> str:
        """Persist one fleet snapshot: a citable snapshot row, the device inventory, metrics, and compliance."""
        with self._lock:
            snapshot_id = self._write_fleet_snapshot(tenant_id, snapshot)
            self._commit()
        return snapshot_id

    def ingest_fleet_snapshots(self, tenant_id: str, snapshots: Sequence[FleetSnapshotIn]) -> list[str]:
        """Persist several fleet snapshots as one atomic batch: all of them land, or none do."""
        with self._transaction():
            return [self._write_fleet_snapshot(tenant_id, snapshot) for snapshot in snapshots]

    def _write_fleet_snapshot(self, tenant_id: str, snapshot: FleetSnapshotIn) -> str:
        """Write one snapshot's rows. The caller holds the lock and owns the commit."""
        snapshot_id = str(uuid4())
        observed = _ts(snapshot.observed_at)
        device_count = len(snapshot.devices)

        self._conn.execute(
            """
            INSERT INTO telemetry_snapshots (snapshot_id, tenant_id, source, captured_at, tags_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            [snapshot_id, tenant_id, snapshot.source, observed, json.dumps({"kind": "fleet", "devices": str(device_count)})],
        )
        for device in snapshot.devices:
            self._conn.execute(
                "DELETE FROM devices WHERE tenant_id = ? AND device_id = ?",
                [tenant_id, device.device_id],
            )
            self._conn.execute(
                """
                INSERT INTO devices
                (tenant_id, device_id, employee_id, device_type, os_name, os_version, snapshot_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    tenant_id,
                    device.device_id,
                    device.employee_id,
                    device.device_type,
                    device.os_name,
                    device.os_version,
                    snapshot_id,
                    observed,
                ],
            )
            metric_values = {
                "disk_free_pct": device.disk_free_pct,
                "ram_used_pct": device.ram_used_pct,
                "battery_health_pct": device.battery_health_pct,
            }
            metric_rows = [
                [str(uuid4()), snapshot_id, tenant_id, device.device_id, name, float(metric_values[name]), observed]
                for name in FLEET_METRIC_FIELDS
                if metric_values[name] is not None
            ]
            self._conn.executemany(
                """
                INSERT INTO device_metrics (point_id, snapshot_id, tenant_id, device_id, metric, value, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                metric_rows,
            )
            compliance_rows = [
                [str(uuid4()), snapshot_id, tenant_id, device.device_id, check.check_id, check.severity, check.passed, observed]
                for check in device.compliance
            ]
            self._conn.executemany(
                """
                INSERT INTO compliance_results
                (result_id, snapshot_id, tenant_id, device_id, check_id, severity, passed, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                compliance_rows,
            )
        return snapshot_id

    # --- tenants -------------------------------------------------------------------------------

    def ensure_tenant(self, tenant_id: str, name: str, created_at: datetime) -> None:
        """Register a tenant if it is unknown. Existing rows are never reactivated, so a deleted tenant stays deleted."""
        with self._lock:
            exists = self._conn.execute("SELECT 1 FROM tenants WHERE tenant_id = ?", [tenant_id]).fetchone()
            if not exists:
                self._conn.execute(
                    "INSERT INTO tenants (tenant_id, name, status, created_at) VALUES (?, ?, 'active', ?)",
                    [tenant_id, name, _ts(created_at)],
                )
                self._commit()

    def tenant_exists(self, tenant_id: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM tenants WHERE tenant_id = ?", [tenant_id]).fetchone() is not None

    def is_tenant_active(self, tenant_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT status FROM tenants WHERE tenant_id = ?", [tenant_id]).fetchone()
        return bool(row) and row[0] == "active"

    def list_tenants(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tenant_id, name, status, created_at, deleted_at FROM tenants ORDER BY tenant_id"
            ).fetchall()
        return [
            {"tenant_id": r[0], "name": r[1], "status": r[2], "created_at": r[3], "deleted_at": r[4]} for r in rows
        ]

    def term_identities(self) -> list[tuple[str, str, str]]:
        """(owner tenant, term, display name) for tenants and for every company uploaded into a tenant."""
        with self._lock:
            tenants = [(r[0], r[0], r[1]) for r in self._conn.execute("SELECT tenant_id, name FROM tenants").fetchall()]
            companies = [
                (r[0], r[1], r[1].split("-")[0].title())
                for r in self._conn.execute("SELECT tenant_id, company_id FROM tenant_companies").fetchall()
            ]
        return tenants + companies

    def add_tenant_companies(self, tenant_id: str, company_ids: Iterable[str]) -> None:
        """Record which companies a tenant holds, so questions about them are the tenant's own."""
        with self._lock:
            for company in sorted(set(company_ids)):
                self._conn.execute(
                    "INSERT OR IGNORE INTO tenant_companies (tenant_id, company_id) VALUES (?, ?)", [tenant_id, company]
                )
            self._commit()

    def tenant_identities(self) -> list[tuple[str, str]]:
        """(tenant_id, display name) for every tenant, including deleted ones, so mentions are still refused."""
        with self._lock:
            return [(r[0], r[1]) for r in self._conn.execute("SELECT tenant_id, name FROM tenants").fetchall()]

    def create_tenant_with_admin(
        self,
        tenant_id: str,
        name: str,
        user_id: str,
        email: str,
        password_hash: str,
        created_at: datetime,
    ) -> None:
        """Create a tenant and its first admin in one transaction."""
        stamp = _ts(created_at)
        with self._transaction():
            self._conn.execute(
                "INSERT INTO tenants (tenant_id, name, status, created_at) VALUES (?, ?, 'active', ?)",
                [tenant_id, name, stamp],
            )
            self._conn.execute(
                "INSERT INTO users (user_id, tenant_id, email, password_hash, role, created_at) "
                "VALUES (?, ?, ?, ?, 'admin', ?)",
                [user_id, tenant_id, email, password_hash, stamp],
            )

    def create_user(
        self, user_id: str, tenant_id: str, email: str, password_hash: str, role: str, created_at: datetime
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO users (user_id, tenant_id, email, password_hash, role, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [user_id, tenant_id, email, password_hash, role, _ts(created_at)],
            )
            self._commit()

    def list_users(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, email, role, created_at FROM users WHERE tenant_id = ? ORDER BY email",
                [tenant_id],
            ).fetchall()
        return [{"user_id": r[0], "email": r[1], "role": r[2], "created_at": r[3]} for r in rows]

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id, tenant_id, email, password_hash, role FROM users WHERE email = ?", [email]
            ).fetchone()
        if not row:
            return None
        return {"user_id": row[0], "tenant_id": row[1], "email": row[2], "password_hash": row[3], "role": row[4]}

    def create_session(
        self, token_hash: str, user_id: str, tenant_id: str, created_at: datetime, expires_at: datetime
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (token_hash, user_id, tenant_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                [token_hash, user_id, tenant_id, _ts(created_at), _ts(expires_at)],
            )
            self._commit()

    def get_session(self, token_hash: str, now: datetime) -> dict[str, Any] | None:
        """Return the live session for a token hash. Expired sessions are treated as absent."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT s.user_id, s.tenant_id, u.email, u.role
                FROM sessions s JOIN users u ON u.user_id = s.user_id
                WHERE s.token_hash = ? AND s.expires_at > ?
                """,
                [token_hash, _ts(now)],
            ).fetchone()
        if not row:
            return None
        return {"user_id": row[0], "tenant_id": row[1], "email": row[2], "role": row[3]}

    def save_judge_key(
        self, user_id: str, tenant_id: str, ciphertext: str, last4: str, created_at: datetime, expires_at: datetime
    ) -> None:
        """Replace the user's saved judge key. Only the encrypted form and its last four characters are stored."""
        with self._lock:
            self._conn.execute("DELETE FROM judge_keys WHERE user_id = ?", [user_id])
            self._conn.execute(
                "INSERT INTO judge_keys (user_id, tenant_id, ciphertext, last4, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [user_id, tenant_id, ciphertext, last4, created_at.isoformat(), expires_at.isoformat()],
            )
            self._commit()

    def get_judge_key(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT ciphertext, last4, created_at, expires_at FROM judge_keys WHERE user_id = ?", [user_id]
            ).fetchone()
        if not row:
            return None
        return {"ciphertext": row[0], "last4": row[1], "created_at": row[2], "expires_at": row[3]}

    def delete_judge_key(self, user_id: str) -> bool:
        with self._lock:
            existed = self._conn.execute("SELECT 1 FROM judge_keys WHERE user_id = ?", [user_id]).fetchone() is not None
            self._conn.execute("DELETE FROM judge_keys WHERE user_id = ?", [user_id])
            self._commit()
        return existed

    def set_user_password(self, user_id: str, password_hash: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET password_hash = ? WHERE user_id = ?", [password_hash, user_id])
            self._commit()

    def delete_session(self, token_hash: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE token_hash = ?", [token_hash])
            self._commit()

    def delete_tenant_data(self, tenant_id: str, deleted_at: datetime) -> dict[str, int]:
        """Hard-delete one tenant's data and identities, then tombstone the tenant. Audit rows are kept.

        Returns the number of rows removed per table. Runs as one transaction.
        """
        tables = (
            "sessions",
            "tenant_companies",
            "users",
            "compliance_results",
            "device_metrics",
            "devices",
            "telemetry_points",
            "telemetry_snapshots",
        )
        with self._transaction():
            counts: dict[str, int] = {}
            for table in tables:
                counts[table] = int(
                    self._conn.execute(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = ?", [tenant_id]).fetchone()[0]
                )
                self._conn.execute(f"DELETE FROM {table} WHERE tenant_id = ?", [tenant_id])
            self._conn.execute(
                "UPDATE tenants SET status = 'deleted', deleted_at = ? WHERE tenant_id = ?",
                [_ts(deleted_at), tenant_id],
            )
        return counts

    def has_devices(self, tenant_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM devices WHERE tenant_id = ?", [tenant_id]
            ).fetchone()
        return bool(row[0])

    def list_devices(self, tenant_id: str) -> list[DeviceRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT tenant_id, device_id, employee_id, device_type, os_name, os_version, snapshot_id, updated_at
                FROM devices WHERE tenant_id = ? ORDER BY device_id
                """,
                [tenant_id],
            ).fetchall()
        return [self._device_from_row(row) for row in rows]

    def get_device(self, tenant_id: str, device_id: str) -> DeviceRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT tenant_id, device_id, employee_id, device_type, os_name, os_version, snapshot_id, updated_at
                FROM devices WHERE tenant_id = ? AND device_id = ?
                """,
                [tenant_id, device_id],
            ).fetchone()
        return self._device_from_row(row) if row else None

    def employee_has_device(self, tenant_id: str, employee_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM devices WHERE tenant_id = ? AND employee_id = ?",
                [tenant_id, employee_id],
            ).fetchone()
        return bool(row[0])

    def get_device_metrics(self, tenant_id: str, window_minutes: int, as_of: datetime | None = None) -> list[MetricRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT snapshot_id, device_id, metric, value, observed_at
                FROM device_metrics
                WHERE tenant_id = ? AND observed_at >= ? AND observed_at <= ?
                ORDER BY device_id, metric, observed_at ASC
                """,
                [tenant_id, *self._window(window_minutes, as_of)],
            ).fetchall()
        return [
            MetricRecord(
                snapshot_id=row[0],
                device_id=row[1],
                metric=row[2],
                value=float(row[3]),
                observed_at=self._normalize_datetime(row[4]),
            )
            for row in rows
        ]

    def get_compliance_results(self, tenant_id: str, window_minutes: int, as_of: datetime | None = None) -> list[ComplianceRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT snapshot_id, device_id, check_id, severity, passed, observed_at
                FROM compliance_results
                WHERE tenant_id = ? AND observed_at >= ? AND observed_at <= ?
                ORDER BY device_id, check_id, observed_at ASC
                """,
                [tenant_id, *self._window(window_minutes, as_of)],
            ).fetchall()
        return [
            ComplianceRecord(
                snapshot_id=row[0],
                device_id=row[1],
                check_id=row[2],
                severity=row[3],
                passed=bool(row[4]),
                observed_at=self._normalize_datetime(row[5]),
            )
            for row in rows
        ]

    def get_recent_snapshots(
        self, tenant_id: str, window_minutes: int, limit: int = 20, as_of: datetime | None = None
    ) -> list[SnapshotRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT snapshot_id, source, captured_at
                FROM telemetry_snapshots
                WHERE tenant_id = ? AND captured_at >= ? AND captured_at <= ?
                ORDER BY captured_at DESC
                LIMIT ?
                """,
                [tenant_id, *self._window(window_minutes, as_of), limit],
            ).fetchall()

        return [
            SnapshotRecord(
                snapshot_id=row[0],
                source=row[1],
                captured_at=self._normalize_datetime(row[2]),
            )
            for row in rows
        ]

    def get_points(self, tenant_id: str, window_minutes: int, as_of: datetime | None = None) -> list[SnapshotPointRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT snapshot_id, metric, value, observed_at
                FROM telemetry_points
                WHERE tenant_id = ? AND observed_at >= ? AND observed_at <= ?
                ORDER BY observed_at ASC
                """,
                [tenant_id, *self._window(window_minutes, as_of)],
            ).fetchall()
        return [
            SnapshotPointRecord(
                snapshot_id=row[0],
                metric=row[1],
                value=float(row[2]),
                observed_at=self._normalize_datetime(row[3]),
            )
            for row in rows
        ]

    def _device_from_row(self, row: tuple) -> DeviceRecord:
        return DeviceRecord(
            tenant_id=row[0],
            device_id=row[1],
            employee_id=row[2],
            device_type=row[3],
            os_name=row[4],
            os_version=row[5],
            snapshot_id=row[6],
            updated_at=self._normalize_datetime(row[7]),
        )
