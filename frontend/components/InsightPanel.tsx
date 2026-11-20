import { Insight } from "@/lib/types";

type Props = {
  insights: Insight[];
};

const severityStyle: Record<Insight["severity"], string> = {
  low: "text-emerald-300",
  medium: "text-amber-300",
  high: "text-rose-300"
};

export function InsightPanel({ insights }: Props) {
  return (
    <section className="rounded-xl border border-slate-800 bg-panel p-4">
      <h2 className="mb-3 text-lg font-semibold text-cyan-300">Insights &amp; trends</h2>
      {!insights.length ? (
        <p className="text-sm text-slate-400">No insights for this query.</p>
      ) : (
        <ul className="space-y-2 text-sm">
          {insights.map((item, index) => (
            <li key={`${item.kind}-${index}`} className="rounded-md bg-panelSoft p-3">
              <div className={`font-medium ${severityStyle[item.severity]}`}>
                {item.finding} ({item.severity.toUpperCase()})
              </div>
              <p className="mt-1 text-slate-300">{item.explanation}</p>
              <ul className="mt-2 list-disc space-y-0.5 pl-5 text-xs text-slate-400">
                {item.evidence.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
