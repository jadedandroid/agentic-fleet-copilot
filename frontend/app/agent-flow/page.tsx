"use client";

import { FormEvent, useState } from "react";

import { PipelineDiagram } from "@/components/PipelineDiagram";
import { useAuth } from "@/components/AuthProvider";
import { RequireAuth } from "@/components/RequireAuth";
import { PIPELINE_STAGES } from "@/lib/pipeline";
import { API_BASE } from "@/lib/api";
import { CopilotResponse, TraceStep } from "@/lib/types";

const DEFAULT_QUESTION = "Which devices at Acme are low on disk space?";
// Mirrors EVIDENCE_THRESHOLD in backend/app/copilot.py.
const EVIDENCE_THRESHOLD = 0.7;

type AuditEvent = {
  event_type: string;
  tenant_id: string;
  payload: Record<string, unknown>;
  created_at: string;
};

type Check = { label: string; status: "pass" | "blocked" | "pending" | "n/a"; detail: string };

const STATUS_STYLE: Record<Check["status"], string> = {
  pass: "bg-emerald-900/60 text-emerald-200",
  blocked: "bg-rose-900/60 text-rose-200",
  pending: "bg-amber-900/60 text-amber-200",
  "n/a": "bg-slate-800 text-slate-300"
};

function isEvidenceRefusal(trace: TraceStep): boolean {
  return trace.stage === "guardrail" && trace.summary.startsWith("Refused to propose");
}

// Backend emits `policy` and evidence-level `guardrail` traces while proposing, so they belong to the proposal stage.
function stageTraces(traces: TraceStep[], key: string): TraceStep[] {
  return traces.filter((trace) => {
    if (key === "router") {
      return trace.stage === "router" || trace.stage === "plan";
    }
    if (key === "proposal") {
      return trace.stage === "proposal" || trace.stage === "policy" || trace.stage === "replan" || isEvidenceRefusal(trace);
    }
    if (key === "compose") {
      return trace.stage === "compose" || (trace.stage === "guardrail" && !isEvidenceRefusal(trace));
    }
    return trace.stage === key;
  });
}

function buildChecks(response: CopilotResponse, auditEvents: AuditEvent[]): Check[] {
  const checks: Check[] = [];
  const refused = Boolean(response.refusal_reason);

  checks.push({
    label: "Intent routing",
    status: refused && response.intent !== "unsupported" && response.intent !== "cross_tenant" ? "blocked" : "pass",
    detail: `Routed to "${response.intent}"${refused ? `, refused: ${response.refusal_reason}` : ""}`
  });

  checks.push({
    label: "Tenant scope",
    status: "pass",
    detail: `Retrieval is scoped to the API key's tenant. ${response.citations.length} citation(s) returned.`
  });

  const proposal = response.proposed_action;
  const evidenceTrace = response.traces.find(isEvidenceRefusal);
  if (proposal) {
    checks.push({
      label: "Evidence threshold",
      status: proposal.evidence_score >= EVIDENCE_THRESHOLD ? "pass" : "blocked",
      detail: `Evidence score ${proposal.evidence_score.toFixed(2)} vs required ${EVIDENCE_THRESHOLD.toFixed(2)}`
    });
  } else if (evidenceTrace) {
    checks.push({ label: "Evidence threshold", status: "blocked", detail: evidenceTrace.summary });
  } else {
    checks.push({ label: "Evidence threshold", status: "n/a", detail: "No action was proposed for this question." });
  }

  if (response.awaiting_approval) {
    checks.push({
      label: "Human approval",
      status: "pending",
      detail: "Paused at the approval gate. Approve or reject it on the Agent co-pilot page."
    });
  } else if (proposal) {
    checks.push({ label: "Human approval", status: "pass", detail: `Decision: ${proposal.status}` });
  } else {
    checks.push({ label: "Human approval", status: "n/a", detail: "Nothing needs approval." });
  }

  if (proposal) {
    const recorded = auditEvents.some((event) => event.payload.action_id === proposal.action_id);
    checks.push({
      label: "Audit trail",
      status: recorded ? "pass" : "blocked",
      detail: recorded
        ? `Proposal ${proposal.action_id} is in the audit log.`
        : `No audit event found for ${proposal.action_id}.`
    });
  } else {
    checks.push({ label: "Audit trail", status: "n/a", detail: "Audit events are written for proposals and decisions." });
  }

  return checks;
}

function AgentFlowWorkspace() {
  const { authorizedFetch } = useAuth();
  const [question, setQuestion] = useState(DEFAULT_QUESTION);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [response, setResponse] = useState<CopilotResponse | null>(null);
  const [checks, setChecks] = useState<Check[]>([]);

  const runLive = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setRunning(true);
    setError("");
    setResponse(null);
    setChecks([]);
    try {
      const queryResponse = await authorizedFetch("/api/v1/copilot/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, allow_remediation: true })
      });
      if (!queryResponse.ok) {
        throw new Error(`Query failed with status ${queryResponse.status}.`);
      }
      const body = (await queryResponse.json()) as CopilotResponse;

      const auditResponse = await authorizedFetch("/api/v1/audit/events");
      const auditEvents = auditResponse.ok
        ? ((await auditResponse.json()) as { events: AuditEvent[] }).events
        : [];

      setResponse(body);
      setChecks(buildChecks(body, auditEvents));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Run failed. Is the backend reachable?");
    } finally {
      setRunning(false);
    }
  };

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-8 p-6">
      <header className="space-y-2 pt-4">
        <p className="text-sm uppercase tracking-wide text-cyan-300">Agent-flow</p>
        <h1 className="text-3xl font-bold">How a question becomes a cited, approved action</h1>
        <p className="max-w-3xl text-slate-300">
          Every question passes through the same stages. Each stage has a guardrail that can stop the run. The live
          panel below runs one question and shows which stages ran and which checks passed.
        </p>
      </header>

      <section className="space-y-3">
        <h2 className="text-xl font-semibold text-cyan-300">The pipeline</h2>
        <PipelineDiagram />
      </section>

      <section className="space-y-4 rounded-xl border border-slate-800 bg-panel p-5">
        <div>
          <h2 className="text-xl font-semibold text-cyan-300">Live run</h2>
          <p className="text-sm text-slate-400">
            Runs the query as your signed-in tenant. It only asks questions and never approves or executes anything.
          </p>
        </div>
        <form onSubmit={runLive} className="flex flex-col gap-3 md:flex-row">
          <input
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            aria-label="Question"
            className="flex-1 rounded-md border border-slate-700 bg-slate-900 px-3 py-2"
          />
          <button
            type="submit"
            disabled={running || !question.trim()}
            className="rounded-md bg-cyan-500 px-4 py-2 font-semibold text-slate-900 transition hover:bg-cyan-400 disabled:opacity-50"
          >
            {running ? "Running..." : "Run through pipeline"}
          </button>
        </form>

        {error && <p className="rounded-md bg-rose-900/60 p-3 text-sm text-rose-100">{error}</p>}

        {response && (
          <div className="grid gap-4 lg:grid-cols-2">
            <div className="space-y-2">
              <h3 className="font-semibold text-slate-100">Stages</h3>
              <ol className="space-y-2">
                {PIPELINE_STAGES.map((stage) => {
                  const traces = stageTraces(response.traces, stage.key);
                  const reached = traces.length > 0;
                  const isGate = stage.key === "approval" || stage.key === "execute" || stage.key === "audit";
                  let status = reached ? "ran" : "not reached";
                  if (stage.key === "approval" && response.awaiting_approval) status = "waiting for reviewer";
                  if (isGate && !reached) status = response.awaiting_approval ? "waiting" : "not reached";
                  return (
                    <li key={stage.key} className="rounded-md bg-panelSoft p-3 text-sm">
                      <div className="flex items-center justify-between">
                        <span className="font-medium">{stage.title}</span>
                        <span className="text-xs text-slate-400">{status}</span>
                      </div>
                      {traces.map((trace, index) => (
                        <p key={`${trace.stage}-${index}`} className="mt-1 text-xs text-slate-300">
                          {trace.summary}
                        </p>
                      ))}
                    </li>
                  );
                })}
              </ol>
            </div>

            <div className="space-y-2">
              <h3 className="font-semibold text-slate-100">Guardrail checks</h3>
              <ul className="space-y-2">
                {checks.map((check) => (
                  <li key={check.label} className="rounded-md bg-panelSoft p-3 text-sm">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium">{check.label}</span>
                      <span className={`rounded px-2 py-0.5 text-xs uppercase ${STATUS_STYLE[check.status]}`}>
                        {check.status}
                      </span>
                    </div>
                    <p className="mt-1 text-xs text-slate-300">{check.detail}</p>
                  </li>
                ))}
              </ul>
              {response.answer && (
                <p className="rounded-md border border-slate-800 p-3 text-sm text-slate-200">{response.answer}</p>
              )}
            </div>
          </div>
        )}
      </section>
    </main>
  );
}

export default function AgentFlowPage() {
  return (
    <RequireAuth>
      <AgentFlowWorkspace />
    </RequireAuth>
  );
}
