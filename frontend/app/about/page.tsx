import Link from "next/link";

const CAPABILITIES = [
  {
    title: "Grounded question answering",
    body: "Answers come only from telemetry. Each device in an answer is cited back to its snapshot, metric, and value. Questions the data cannot support are refused."
  },
  {
    title: "Insight and trend detection",
    body: "Deterministic analyses find devices that stay below a storage or RAM threshold across snapshots, batteries projected to reach end-of-life, OS versions older than macOS 15, and compliance drift."
  },
  {
    title: "Approval-gated action proposals",
    body: "The copilot proposes catalog tools such as upgrade orders, remediation tickets, replacement flags, and employee notices. Each proposal waits for a human decision, and every decision is audited."
  }
];

const GUARANTEES = [
  "Tenant isolation: each account or service key maps server-side to one tenant. Cross-tenant reads and writes are refused.",
  "Determinism: the same telemetry and reference time always produce the same answer, trace, and proposal. The LLM is not in the runtime path.",
  "Evidence threshold: proposals need an evidence score of at least 0.70. Weak evidence triggers one wider re-analysis, then a refusal with a visible trace.",
  "Human approval: no state change runs until an admin approves it. Pending approvals survive a backend restart.",
  "Abuse limits: five failed sign-ins for an email lock it for 15 minutes.",
  "Audit: proposals, decisions, executions, logins, tenant changes, and uploads are written to an append-only log."
];

const LIMITS = [
  "Tool actions are mock stubs. They do not call real ticketing, procurement, or notification systems.",
  "Sessions use opaque tokens, not JWTs. There is no SSO and no per-user device or IP throttling.",
  "Battery end-of-life cannot be assessed when battery health is not reported. The copilot refuses rather than guessing.",
  "The RAGAS judge is optional and has not been run in this environment. The default evaluation is deterministic."
];

export default function AboutPage() {
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-col gap-8 p-6">
      <header className="space-y-2 pt-4">
        <p className="text-sm uppercase tracking-wide text-cyan-300">About</p>
        <h1 className="text-3xl font-bold">What this copilot is for</h1>
        <p className="text-slate-300">
          Agentic Fleet Copilot helps IT administrators act on device telemetry. It answers questions with evidence,
          surfaces patterns over time, and proposes fixes that a person must approve.
        </p>
      </header>

      <section className="space-y-3">
        <h2 className="text-xl font-semibold text-cyan-300">Capabilities</h2>
        <div className="grid gap-4 md:grid-cols-3">
          {CAPABILITIES.map((item) => (
            <div key={item.title} className="rounded-xl border border-slate-800 bg-panel p-4">
              <h3 className="font-semibold">{item.title}</h3>
              <p className="mt-2 text-sm text-slate-300">{item.body}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-xl font-semibold text-cyan-300">Guarantees</h2>
        <ul className="list-disc space-y-2 pl-6 text-sm text-slate-300">
          {GUARANTEES.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </section>

      <section className="space-y-3">
        <h2 className="text-xl font-semibold text-cyan-300">Stack</h2>
        <p className="text-sm text-slate-300">
          Backend: Python, FastAPI with Server-Sent Events, LangGraph for the stateful graph and human interrupts, and
          Pydantic for strict schemas. Storage: DuckDB when installed, otherwise SQLite, with append-only SQLite and
          JSONL audit logs. Frontend: Next.js, TypeScript, and Tailwind CSS. Tests: pytest, including adversarial and
          golden-answer regression suites.
        </p>
      </section>

      <section className="space-y-3">
        <h2 className="text-xl font-semibold text-amber-300">Known limits</h2>
        <ul className="list-disc space-y-2 pl-6 text-sm text-slate-300">
          {LIMITS.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </section>

      <p className="text-sm text-slate-400">
        Want to see it work? Start at <Link className="text-cyan-300 underline" href="/agent-flow">Agent-flow</Link>.
      </p>
    </main>
  );
}
