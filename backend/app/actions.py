from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


def _ref(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


class ActionToolbox:
    """State-changing tools. Each runs only after an explicit human approval."""

    tool_names = (
        "create_upgrade_order",
        "open_remediation_ticket",
        "flag_device_for_replacement",
        "notify_employee",
    )

    def create_upgrade_order(self, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "order_id": _ref("uo"),
            "tenant_id": tenant_id,
            "device_id": payload["device_id"],
            "component": payload["component"],
            "spec": payload["spec"],
            "status": "queued",
            "queued_at": datetime.now(tz=UTC).isoformat(),
        }

    def open_remediation_ticket(self, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "ticket_id": _ref("rt"),
            "tenant_id": tenant_id,
            "device_id": payload["device_id"],
            "check_id": payload["check_id"],
            "note": payload["note"],
            "status": "open",
            "opened_at": datetime.now(tz=UTC).isoformat(),
        }

    def flag_device_for_replacement(self, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "tenant_id": tenant_id,
            "device_id": payload["device_id"],
            "reason": payload["reason"],
            "flagged": True,
            "flagged_at": datetime.now(tz=UTC).isoformat(),
        }

    def notify_employee(self, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "tenant_id": tenant_id,
            "employee_id": payload["employee_id"],
            "message": payload["message"],
            "delivered": True,
            "delivered_at": datetime.now(tz=UTC).isoformat(),
        }

    def execute(self, tool_name: str, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in self.tool_names:
            raise ValueError(f"Unknown tool: {tool_name}")
        handler: Callable[[str, dict[str, Any]], dict[str, Any]] = getattr(self, tool_name)
        return handler(tenant_id, payload)
