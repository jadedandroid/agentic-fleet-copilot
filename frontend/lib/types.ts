export type TraceStep = {
  stage: string;
  summary: string;
  details: Record<string, unknown>;
};

export type Citation = {
  snapshot_id: string;
  observed_at: string;
  reason: string;
  device_id: string | null;
  employee_id: string | null;
  metric: string | null;
  value: number | null;
};

export type Severity = "low" | "medium" | "high";

export type Insight = {
  kind: string;
  finding: string;
  evidence: string[];
  explanation: string;
  severity: Severity;
  device_ids: string[];
};

export type ProposedAction = {
  action_id: string;
  title: string;
  summary: string;
  risk_level: Severity;
  tool_name: string;
  tool_payload: Record<string, unknown>;
  evidence_score: number;
  requires_approval: boolean;
  status: "pending" | "approved" | "rejected";
};

export type CopilotResponse = {
  company: string | null;
  answer: string;
  intent: string;
  refusal_reason: string | null;
  traces: TraceStep[];
  citations: Citation[];
  insights: Insight[];
  proposed_action: ProposedAction | null;
  thread_id: string;
  awaiting_approval: boolean;
};

export type ApprovalDecisionResponse = {
  action_id: string;
  status: "approved" | "rejected";
  reviewer: string;
  decision_at: string;
  graph_resumed: boolean;
};

export type ActionExecutionResponse = {
  action_id: string;
  executed: boolean;
  tool_name: string;
  result: Record<string, unknown>;
};

export type SimDevice = {
  device_id: string;
  employee_id: string;
  device_type: "laptop" | "desktop";
  os_name: string;
  os_version: string;
};

export type FleetDeviceRecord = SimDevice & {
  tenant_id: string;
  snapshot_id: string;
  updated_at: string;
};

export type ComplianceCheckInput = {
  check_id: string;
  severity: Severity;
  passed: boolean;
};

export type DeviceSnapshotInput = SimDevice & {
  disk_free_pct: number;
  ram_used_pct: number;
  battery_health_pct: number | null;
  compliance: ComplianceCheckInput[];
};

export type FleetSnapshotIn = {
  source: string;
  observed_at: string;
  devices: DeviceSnapshotInput[];
};
