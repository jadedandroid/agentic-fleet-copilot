from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any


class ActionNotFoundError(ValueError):
    """The action does not exist for the requesting tenant."""


class ActionStateError(ValueError):
    """The action is not in the state required for the requested transition."""


class AuditLogger:
    def __init__(self, sqlite_path: Path, jsonl_path: Path) -> None:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._jsonl_path = jsonl_path
        self._lock = Lock()
        self._conn = sqlite3.connect(sqlite_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS remediation_actions (
                    action_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    reviewer TEXT,
                    comment TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def append_event(self, event_type: str, tenant_id: str, payload: dict[str, Any]) -> None:
        created_at = datetime.now(tz=UTC).isoformat()
        payload_json = json.dumps(payload, sort_keys=True, default=str)

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit_events (event_type, tenant_id, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                [event_type, tenant_id, payload_json, created_at],
            )
            self._conn.commit()

            with self._jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "event_type": event_type,
                            "tenant_id": tenant_id,
                            "payload": payload,
                            "created_at": created_at,
                        },
                        sort_keys=True,
                        default=str,
                    )
                    + "\n"
                )

    def record_action(self, action_id: str, tenant_id: str, payload: dict[str, Any]) -> None:
        now = datetime.now(tz=UTC).isoformat()
        payload_json = json.dumps(payload, sort_keys=True, default=str)
        with self._lock:
            # Plain INSERT: a proposal is never overwritten, so a status cannot be reset.
            self._conn.execute(
                """
                INSERT INTO remediation_actions
                (action_id, tenant_id, status, payload_json, created_at, updated_at)
                VALUES (?, ?, 'pending', ?, ?, ?)
                """,
                [action_id, tenant_id, payload_json, now, now],
            )
            self._conn.commit()
        self.append_event("remediation.proposed", tenant_id, {"action_id": action_id, **payload})

    def resolve_action(
        self,
        action_id: str,
        tenant_id: str,
        decision: str,
        reviewer: str,
        comment: str | None,
    ) -> datetime:
        decision_at = datetime.now(tz=UTC)
        status = "approved" if decision == "approve" else "rejected"
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE remediation_actions
                SET status = ?, reviewer = ?, comment = ?, updated_at = ?
                WHERE action_id = ? AND tenant_id = ? AND status = 'pending'
                """,
                [status, reviewer, comment, decision_at.isoformat(), action_id, tenant_id],
            )
            if cursor.rowcount == 0:
                self._raise_transition_error(action_id, tenant_id, "pending")
            self._conn.commit()
        self.append_event(
            "remediation.decided",
            tenant_id,
            {
                "action_id": action_id,
                "status": status,
                "reviewer": reviewer,
                "comment": comment,
                "decision_at": decision_at.isoformat(),
            },
        )
        return decision_at

    def begin_execution(self, action_id: str, tenant_id: str) -> None:
        """Atomically move an approved action to `executing` so it cannot run twice."""
        now = datetime.now(tz=UTC).isoformat()
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE remediation_actions SET status = 'executing', updated_at = ?
                WHERE action_id = ? AND tenant_id = ? AND status = 'approved'
                """,
                [now, action_id, tenant_id],
            )
            if cursor.rowcount == 0:
                self._raise_transition_error(action_id, tenant_id, "approved")
            self._conn.commit()

    def revert_execution(self, action_id: str, tenant_id: str) -> None:
        """Return an `executing` action to `approved` after a failed tool call."""
        now = datetime.now(tz=UTC).isoformat()
        with self._lock:
            self._conn.execute(
                """
                UPDATE remediation_actions SET status = 'approved', updated_at = ?
                WHERE action_id = ? AND tenant_id = ? AND status = 'executing'
                """,
                [now, action_id, tenant_id],
            )
            self._conn.commit()

    def get_action(self, action_id: str, tenant_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT action_id, tenant_id, status, payload_json, reviewer, comment, created_at, updated_at
                FROM remediation_actions
                WHERE action_id = ? AND tenant_id = ?
                """,
                [action_id, tenant_id],
            ).fetchone()
        if row is None:
            raise ActionNotFoundError("Action not found for tenant")

        return {
            "action_id": row["action_id"],
            "tenant_id": row["tenant_id"],
            "status": row["status"],
            "payload": json.loads(row["payload_json"]),
            "reviewer": row["reviewer"],
            "comment": row["comment"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def mark_action_executed(self, action_id: str, tenant_id: str, execution_result: dict[str, Any]) -> None:
        updated_at = datetime.now(tz=UTC).isoformat()
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE remediation_actions
                SET status = 'executed', updated_at = ?
                WHERE action_id = ? AND tenant_id = ? AND status = 'executing'
                """,
                [updated_at, action_id, tenant_id],
            )
            if cursor.rowcount == 0:
                self._raise_transition_error(action_id, tenant_id, "executing")
            self._conn.commit()
        self.append_event(
            "remediation.executed",
            tenant_id,
            {"action_id": action_id, "result": execution_result, "executed_at": updated_at},
        )

    def _raise_transition_error(self, action_id: str, tenant_id: str, expected: str) -> None:
        row = self._conn.execute(
            "SELECT status FROM remediation_actions WHERE action_id = ? AND tenant_id = ?",
            [action_id, tenant_id],
        ).fetchone()
        if row is None:
            raise ActionNotFoundError("Action not found for tenant")
        raise ActionStateError(f"Action is '{row['status']}', expected '{expected}'")

    def list_recent_events(self, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT event_type, tenant_id, payload_json, created_at
                FROM audit_events
                WHERE tenant_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                [tenant_id, limit],
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "event_type": row["event_type"],
                    "tenant_id": row["tenant_id"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
            )
        return result
