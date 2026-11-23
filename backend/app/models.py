from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, StrictFloat, StrictInt, StrictStr

DEFAULT_LOOKBACK_MINUTES = 20160  # 14 days; trend questions need history
MAX_LOOKBACK_MINUTES = 43200  # 30 days

Severity = Literal["low", "medium", "high"]
ToolName = Literal[
    "create_upgrade_order",
    "open_remediation_ticket",
    "flag_device_for_replacement",
    "notify_employee",
]


class TelemetryPointIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: StrictStr = Field(min_length=1, max_length=120)
    value: StrictFloat
    observed_at: datetime


class TelemetrySnapshotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: StrictStr = Field(min_length=1, max_length=120)
    points: list[TelemetryPointIn] = Field(min_length=1, max_length=1000)
    tags: dict[StrictStr, StrictStr] = Field(default_factory=dict)


class ComplianceCheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: StrictStr = Field(min_length=1, max_length=120)
    severity: Severity
    passed: StrictBool


class DeviceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: StrictStr = Field(min_length=1, max_length=120)
    employee_id: StrictStr = Field(min_length=1, max_length=120)
    device_type: Literal["laptop", "desktop"]
    os_name: StrictStr = Field(min_length=1, max_length=60)
    os_version: StrictStr = Field(min_length=1, max_length=60)
    disk_free_pct: StrictFloat = Field(ge=0, le=100)
    ram_used_pct: StrictFloat = Field(ge=0, le=100)
    battery_health_pct: StrictFloat | None = Field(default=None, ge=0, le=100)
    compliance: list[ComplianceCheckIn] = Field(default_factory=list, max_length=50)


class FleetSnapshotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: StrictStr = Field(min_length=1, max_length=120)
    observed_at: datetime
    devices: list[DeviceIn] = Field(min_length=1, max_length=500)


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: StrictStr
    observed_at: datetime
    reason: StrictStr
    device_id: StrictStr | None = None
    # The employee who holds the cited device, so an answer names the person as well as the machine.
    employee_id: StrictStr | None = None
    metric: StrictStr | None = None
    value: StrictFloat | None = None


class TraceStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: StrictStr
    summary: StrictStr
    details: dict[StrictStr, Any] = Field(default_factory=dict)


class Insight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: StrictStr
    finding: StrictStr
    evidence: list[StrictStr]
    explanation: StrictStr
    severity: Severity
    device_ids: list[StrictStr]


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: StrictStr
    title: StrictStr
    summary: StrictStr
    risk_level: Severity
    tool_name: ToolName
    tool_payload: dict[StrictStr, Any]
    evidence_score: StrictFloat
    requires_approval: StrictBool = True
    status: Literal["pending", "approved", "rejected"] = "pending"


class CopilotQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: StrictStr = Field(min_length=1, max_length=500)
    context_window_minutes: StrictInt = Field(default=DEFAULT_LOOKBACK_MINUTES, ge=5, le=MAX_LOOKBACK_MINUTES)
    allow_remediation: StrictBool = False
    # Ask the LLM planner to place questions the rules cannot. Only takes effect when the judge is enabled.
    llm_planner: StrictBool = False
    # Reference time for the lookback window. Omit for the server's current time. Pin it for reproducible answers.
    as_of: datetime | None = None


class CopilotQueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: StrictStr
    intent: StrictStr
    refusal_reason: StrictStr | None = None
    traces: list[TraceStep]
    citations: list[Citation]
    insights: list[Insight]
    proposed_action: ProposedAction | None = None
    thread_id: StrictStr
    awaiting_approval: StrictBool = False
    # Display name of the caller's company (tenant), so answers say whose fleet they describe.
    company: StrictStr | None = None


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    reviewer: StrictStr = Field(min_length=1, max_length=120)
    comment: StrictStr | None = Field(default=None, max_length=500)


class ApprovalDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: StrictStr
    status: Literal["approved", "rejected"]
    reviewer: StrictStr
    decision_at: datetime
    graph_resumed: StrictBool


class ActionExecutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: StrictStr
    executed: StrictBool
    tool_name: StrictStr
    result: dict[StrictStr, Any]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: StrictStr = Field(min_length=3, max_length=254)
    password: StrictStr = Field(min_length=1, max_length=256)


class TenantCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StrictStr = Field(min_length=2, max_length=80)
    # Optional stable ID. When omitted it is derived from the name.
    tenant_id: StrictStr | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,58}[a-z0-9]$")
    admin_email: StrictStr = Field(min_length=3, max_length=254)
    admin_password: StrictStr = Field(min_length=12, max_length=256)


class UserCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: StrictStr = Field(min_length=3, max_length=254)
    password: StrictStr = Field(min_length=12, max_length=256)
    role: Literal["admin", "analyst"]


class GroundingJudgeRequest(BaseModel):
    """Ask the copilot a question, then score its answer with an OpenAI key.

    Pass `openai_api_key` to use it for this request only. Omit it to use the key you saved, if one is still valid.
    The key is a SecretStr, so it is never shown in reprs or logs.
    """

    model_config = ConfigDict(extra="forbid")

    question: StrictStr = Field(min_length=1, max_length=500)
    openai_api_key: SecretStr | None = Field(default=None, min_length=20, max_length=300)
    as_of: datetime | None = None
    context_window_minutes: StrictInt = Field(default=DEFAULT_LOOKBACK_MINUTES, ge=5, le=MAX_LOOKBACK_MINUTES)


class JudgeKeySaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    openai_api_key: SecretStr = Field(min_length=20, max_length=300)
    # Saved keys expire, so a forgotten key stops working on its own.
    ttl_days: StrictInt = Field(default=30, ge=1, le=90)
