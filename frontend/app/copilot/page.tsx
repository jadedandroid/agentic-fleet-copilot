"use client";

import { useEffect, useRef, useState } from "react";

import { CitationPanel } from "@/components/CitationPanel";
import { RequireAuth } from "@/components/RequireAuth";
import { useAuth } from "@/components/AuthProvider";
import { InsightPanel } from "@/components/InsightPanel";
import { JudgePanel } from "@/components/JudgePanel";
import { API_BASE, readError } from "@/lib/api";
import { TracePanel } from "@/components/TracePanel";
import {
  ActionExecutionResponse,
  ApprovalDecisionResponse,
  Citation,
  CopilotResponse,
  Insight,
  ProposedAction,
  TraceStep
} from "@/lib/types";

const DEFAULT_QUESTION = "Which devices at Acme are low on disk space?";
const STREAM_DROPPED_MESSAGE = "Streaming connection dropped. Ensure backend is reachable.";

type Phase = "idle" | "running" | "awaiting_approval" | "executing" | "done" | "error";

function CopilotWorkspace() {
  const apiBase = API_BASE;
  const { session, authorizedFetch } = useAuth();
  const token = session?.token ?? "";
  // The platform operator must pick a tenant before it can ask anything.
  const needsTenant = session?.role === "platform_admin" && !session.scopeTenantId;
  const [question, setQuestion] = useState(DEFAULT_QUESTION);
  const [allowRemediation, setAllowRemediation] = useState(true);
  // Only works when the server has the judge enabled with a key. Unrecognized questions are then planned by the LLM.
  const [llmPlanner, setLlmPlanner] = useState(false);
  // The LLM rewrites the answer; the server keeps its draft unless every number and ID in the rewrite is in the evidence.
  const [llmInterpret, setLlmInterpret] = useState(false);
  // Blank means the server's current time. A fixed value makes answers reproducible, e.g. for the dataset.
  const [asOf, setAsOf] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [intent, setIntent] = useState("");
  const [answer, setAnswer] = useState("");
  const [refusalReason, setRefusalReason] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [traces, setTraces] = useState<TraceStep[]>([]);
  const [insights, setInsights] = useState<Insight[]>([]);
  const [citations, setCitations] = useState<Citation[]>([]);
  const [company, setCompany] = useState<string | null>(null);
  const [proposedAction, setProposedAction] = useState<ProposedAction | null>(null);
  const [decisionResult, setDecisionResult] = useState<ApprovalDecisionResponse | null>(null);
  const [executionResult, setExecutionResult] = useState<ActionExecutionResponse | null>(null);
  const streamRef = useRef<EventSource | null>(null);

  const closeStream = () => {
    if (!streamRef.current) {
      return;
    }
    streamRef.current.close();
    streamRef.current = null;
  };

  useEffect(() => {
    return () => {
      if (streamRef.current) {
        streamRef.current.close();
        streamRef.current = null;
      }
    };
  }, []);

  const runCopilot = () => {
    closeStream();
    setPhase("running");
    setError("");
    setIntent("");
    setAnswer("");
    setRefusalReason(null);
    setTraces([]);
    setInsights([]);
    setCitations([]);
    setProposedAction(null);
    setDecisionResult(null);
    setExecutionResult(null);

    // EventSource cannot send custom headers, so the session token travels as a query parameter.
    const params = new URLSearchParams({
      apiKey: token,
      question,
      allowRemediation: String(allowRemediation),
      llmPlanner: String(llmPlanner),
      llmInterpret: String(llmInterpret),
      contextWindowMinutes: "525600"
    });
    if (session?.scopeTenantId) {
      params.set("tenantId", session.scopeTenantId);
    }
    if (asOf) {
      params.set("asOf", new Date(asOf).toISOString());
    }
    const source = new EventSource(`${apiBase}/api/v1/copilot/stream?${params.toString()}`);
    streamRef.current = source;

    source.addEventListener("trace", (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as TraceStep;
      setTraces((current) => [...current, payload]);
    });

    source.addEventListener("insights", (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as { items: Insight[] };
      setInsights(payload.items);
    });

    source.addEventListener("proposed_action", (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as ProposedAction;
      setProposedAction(payload);
    });

    source.addEventListener("interrupt", () => {
      setPhase("awaiting_approval");
    });

    source.addEventListener("final", (event) => {
      const payload = JSON.parse((event as MessageEvent).data) as CopilotResponse;
      setIntent(payload.intent);
      setCompany(payload.company);
      setAnswer(payload.answer);
      setRefusalReason(payload.refusal_reason);
      setCitations(payload.citations);
      setInsights(payload.insights);
      setProposedAction(payload.proposed_action);
      setPhase(payload.awaiting_approval ? "awaiting_approval" : "done");
      closeStream();
    });

    source.onerror = () => {
      setError(STREAM_DROPPED_MESSAGE);
      setPhase("error");
      closeStream();
    };
  };

  const executeAction = async (actionId: string) => {
    setPhase("executing");
    const response = await authorizedFetch(`/api/v1/copilot/actions/${actionId}/execute`, { method: "POST" });
    if (!response.ok) {
      setError(await readError(response));
      setPhase("error");
      return;
    }
    setExecutionResult((await response.json()) as ActionExecutionResponse);
    setPhase("done");
  };

  const decideAction = async (decision: "approve" | "reject") => {
    if (!proposedAction) {
      return;
    }
    setError("");
    const response = await authorizedFetch(`/api/v1/copilot/approvals/${proposedAction.action_id}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        decision,
        reviewer: "web-admin",
        comment: decision === "approve" ? "Approved from dashboard" : "Rejected from dashboard"
      })
    });
    if (!response.ok) {
      setError(await readError(response));
      return;
    }
    const result = (await response.json()) as ApprovalDecisionResponse;
    setDecisionResult(result);
    setProposedAction((current) => (current ? { ...current, status: result.status } : current));

    if (decision === "approve") {
      await executeAction(proposedAction.action_id);
    } else {
      setPhase("done");
    }
  };

  const awaitingApproval = phase === "awaiting_approval" && proposedAction !== null;
  const busy = phase === "running" || phase === "executing";

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-7xl flex-col gap-6 p-6">
      <header>
        <p className="text-sm uppercase tracking-wide text-cyan-300">Deterministic operations copilot</p>
        <h1 className="text-3xl font-bold">Agentic Fleet Copilot</h1>
      </header>

      <section className="grid gap-4 rounded-xl border border-slate-800 bg-panel p-4 md:grid-cols-4">
        <div className="text-sm">
          Signed in tenant
          <p className="mt-2 text-slate-300">
            <span className="text-cyan-300">{session?.tenantId ?? session?.scopeTenantId ?? "no tenant selected"}</span>
            {" · "}
            {session?.role}
          </p>
        </div>
        <label className="text-sm md:col-span-2">
          Question
          <input
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            className="mt-1 w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2"
          />
        </label>
        <div className="flex flex-col justify-end gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={allowRemediation}
              onChange={(event) => setAllowRemediation(event.target.checked)}
            />
            Enable remediation proposal
          </label>
          <label className="flex items-center gap-2 text-sm" title="Needs the server's judge enabled with an OpenAI key">
            <input type="checkbox" checked={llmPlanner} onChange={(event) => setLlmPlanner(event.target.checked)} />
            LLM planner for unrecognized questions
          </label>
          <label
            className="flex items-center gap-2 text-sm"
            title="Sends the evidence (device and employee IDs, metrics, times) to OpenAI. Needs the server's judge enabled with a key."
          >
            <input type="checkbox" checked={llmInterpret} onChange={(event) => setLlmInterpret(event.target.checked)} />
            LLM-written answer, verified (sends evidence to OpenAI)
          </label>
          {needsTenant && (
            <p className="text-sm text-amber-300">Choose a tenant in the top bar to ask questions about its data.</p>
          )}
          <button
            onClick={runCopilot}
            disabled={busy || needsTenant}
            className="rounded-md bg-cyan-500 px-4 py-2 font-semibold text-slate-900 transition hover:bg-cyan-400 disabled:opacity-50"
          >
            {phase === "running" ? "Running..." : "Run copilot"}
          </button>
        </div>
      </section>

      <section className="flex flex-col gap-2 rounded-xl border border-slate-800 bg-panel p-4 text-sm md:flex-row md:items-end">
        <label className="flex-1">
          Reference time (blank = now)
          <input
            type="datetime-local"
            value={asOf}
            onChange={(event) => setAsOf(event.target.value)}
            className="mt-1 w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2"
          />
        </label>
        <p className="text-xs text-slate-400">
          Use a past time to query historical data. The one-year window ends at this time, and rows after it are ignored.
          The take-home dataset ends June 12, 2026 at 11:00 UTC, so any time after that on June 12 covers all of it.
        </p>
      </section>

      {refusalReason && (
        <section className="rounded-xl border border-rose-700 bg-rose-950/40 p-4">
          <h2 className="text-lg font-semibold text-rose-300">Refused</h2>
          <p className="mt-1 text-sm text-rose-100">{refusalReason}</p>
        </section>
      )}

      {answer && !refusalReason && (
        <section className="rounded-xl border border-slate-800 bg-panel p-4">
          <h2 className="mb-2 text-lg font-semibold text-cyan-300">
            Answer{" "}
            <span className="text-xs font-normal text-slate-400">
              intent: {intent}
              {company && ` · company: ${company}`}
            </span>
          </h2>
          <p className="text-slate-100">{answer}</p>
        </section>
      )}

      {proposedAction && (
        <section className="rounded-xl border border-amber-700 bg-amber-950/40 p-4">
          <h2 className="text-lg font-semibold text-amber-300">Human approval gate</h2>
          <p className="mt-1 text-sm font-medium text-amber-100">{proposedAction.title}</p>
          <p className="text-sm text-amber-100">{proposedAction.summary}</p>
          <p className="mt-2 text-sm text-amber-200">
            Tool: {proposedAction.tool_name} | Risk: {proposedAction.risk_level.toUpperCase()} | Evidence
            score: {proposedAction.evidence_score.toFixed(2)} | Status: {proposedAction.status.toUpperCase()}
          </p>
          <pre className="mt-2 overflow-x-auto text-xs text-amber-200">
            {JSON.stringify(proposedAction.tool_payload, null, 2)}
          </pre>
          {awaitingApproval && (
            <div className="mt-3 flex gap-2">
              <button
                onClick={() => decideAction("approve")}
                className="rounded-md bg-emerald-500 px-3 py-2 text-sm font-medium text-slate-950"
              >
                Approve &amp; execute
              </button>
              <button
                onClick={() => decideAction("reject")}
                className="rounded-md bg-rose-500 px-3 py-2 text-sm font-medium text-white"
              >
                Reject
              </button>
            </div>
          )}
          {decisionResult && (
            <p className="mt-2 text-xs text-amber-200">
              Decision recorded by {decisionResult.reviewer}; graph resumed: {String(decisionResult.graph_resumed)}
            </p>
          )}
        </section>
      )}

      {executionResult && (
        <section className="rounded-xl border border-emerald-700 bg-emerald-950/40 p-4">
          <h2 className="text-lg font-semibold text-emerald-300">Action executed</h2>
          <pre className="mt-2 overflow-x-auto text-xs text-emerald-100">
            {JSON.stringify(executionResult.result, null, 2)}
          </pre>
        </section>
      )}

      {error && <p className="rounded-md bg-rose-900/60 p-3 text-sm text-rose-100">{error}</p>}

      <section className="grid gap-4 lg:grid-cols-3">
        <TracePanel traces={traces} />
        <InsightPanel insights={insights} />
        <CitationPanel citations={citations} />
      </section>

      <JudgePanel question={question} canJudge={Boolean(answer) && !refusalReason && citations.length > 0} />
    </main>
  );
}

export default function CopilotPage() {
  return (
    <RequireAuth>
      <CopilotWorkspace />
    </RequireAuth>
  );
}
