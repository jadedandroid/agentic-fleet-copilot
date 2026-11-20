import { TraceStep } from "@/lib/types";

type Props = {
  traces: TraceStep[];
};

export function TracePanel({ traces }: Props) {
  return (
    <section className="rounded-xl border border-slate-800 bg-panel p-4">
      <h2 className="mb-3 text-lg font-semibold text-cyan-300">Live reasoning trace</h2>
      <div className="space-y-2">
        {traces.map((trace, index) => (
          <div key={`${trace.stage}-${index}`} className="rounded-md bg-panelSoft p-3 text-sm">
            <div className="font-medium text-slate-100">
              {trace.stage}: {trace.summary}
            </div>
            {Object.keys(trace.details).length > 0 && (
              <pre className="mt-2 overflow-x-auto text-xs text-slate-300">
                {JSON.stringify(trace.details, null, 2)}
              </pre>
            )}
          </div>
        ))}
        {!traces.length && <p className="text-sm text-slate-400">No trace events yet.</p>}
      </div>
    </section>
  );
}
