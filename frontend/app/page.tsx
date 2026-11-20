import Link from "next/link";

const PAGES = [
  {
    href: "/agent-flow",
    title: "Agent-flow",
    body: "Step through each pipeline stage and its guardrails, then run a live question and see every check."
  },
  {
    href: "/copilot",
    title: "Agent co-pilot",
    body: "Ask grounded questions, review cited evidence, and approve or reject proposed remediations."
  },
  {
    href: "/source",
    title: "Source",
    body: "Stream simulated telemetry snapshots into the tenant fleet and watch them land in real time."
  },
  {
    href: "/about",
    title: "About",
    body: "What the system does, how it is built, and what it deliberately does not do."
  }
];

export default function HomePage() {
  return (
    <main className="mx-auto flex w-full max-w-7xl flex-col gap-8 p-6">
      <header className="space-y-3 pt-6">
        <p className="text-sm uppercase tracking-wide text-cyan-300">Deterministic operations copilot</p>
        <h1 className="text-4xl font-bold">Evidence-grounded answers for your device fleet</h1>
        <p className="max-w-2xl text-slate-300">
          Turn static IT telemetry into cited answers, trend insights, and auditable actions. Nothing changes state
          without a human approving it.
        </p>
      </header>

      <section className="grid gap-4 md:grid-cols-2">
        {PAGES.map((page) => (
          <Link
            key={page.href}
            href={page.href}
            className="rounded-xl border border-slate-800 bg-panel p-5 transition hover:border-cyan-600"
          >
            <h2 className="text-lg font-semibold text-cyan-300">{page.title}</h2>
            <p className="mt-2 text-sm text-slate-300">{page.body}</p>
          </Link>
        ))}
      </section>
    </main>
  );
}
