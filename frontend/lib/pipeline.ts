export type PipelineStage = {
  key: string;
  title: string;
  description: string;
  guardrail: string;
};

// Keys match the `stage` field on backend trace events, so the live panel can align them.
export const PIPELINE_STAGES: PipelineStage[] = [
  {
    key: "router",
    title: "Route intent",
    description: "Classify the question into one supported analysis, then record a plan of steps before any data is read. Out-of-scope questions are refused here.",
    guardrail: "Unsupported and cross-tenant questions are refused here, before any data is read."
  },
  {
    key: "retrieval",
    title: "Retrieve telemetry",
    description: "Load the caller's devices, metric points, and compliance results for the time window.",
    guardrail: "Every query is scoped to the tenant that the API key maps to."
  },
  {
    key: "analysis",
    title: "Analyze",
    description: "Run a deterministic analysis: thresholds, persistence across snapshots, and trend fits.",
    guardrail: "No telemetry for the tenant in the window means a refusal, not a guess."
  },
  {
    key: "compose",
    title: "Compose answer",
    description: "Write the answer from the analysis finding, with every device cited back to its snapshot.",
    guardrail: "Answers use only cited telemetry. The LLM is not in this path."
  },
  {
    key: "proposal",
    title: "Propose action",
    description: "Map the finding to one catalog tool. If the evidence is weak, widen the lookback once and analyze again. This is the only loop in the graph.",
    guardrail: "Proposals need an evidence score of at least 0.70. Weaker evidence is refused with a trace."
  },
  {
    key: "approval",
    title: "Human approval gate",
    description: "The graph pauses on an interrupt. Nothing executes until a reviewer approves or rejects.",
    guardrail: "A rejected action can never be executed or re-approved."
  },
  {
    key: "execute",
    title: "Execute",
    description: "Run the approved tool once. Repeat calls return the stored result.",
    guardrail: "Execution is refused before approval (409) and for targets outside the tenant (403)."
  },
  {
    key: "audit",
    title: "Audit",
    description: "Record the proposal, the decision, and the execution in the append-only audit log.",
    guardrail: "Audit events are written to SQLite and JSONL. Tenant views only show that tenant's events."
  }
];
