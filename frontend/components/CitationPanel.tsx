import { Citation } from "@/lib/types";

type Props = {
  citations: Citation[];
};

export function CitationPanel({ citations }: Props) {
  return (
    <section className="rounded-xl border border-slate-800 bg-panel p-4">
      <h2 className="mb-3 text-lg font-semibold text-cyan-300">Evidence citations</h2>
      {!citations.length ? (
        <p className="text-sm text-slate-400">No citations in the current result.</p>
      ) : (
        <ul className="space-y-2 text-sm text-slate-200">
          {citations.map((citation, index) => (
            <li key={`${citation.snapshot_id}-${index}`} className="rounded-md bg-panelSoft p-3">
              <div className="font-medium">{citation.snapshot_id}</div>
              {citation.device_id && (
                <div className="text-slate-300">
                  device {citation.device_id}
                  {citation.employee_id && ` · employee ${citation.employee_id}`}
                  {citation.metric && ` · ${citation.metric}`}
                  {citation.value !== null && ` = ${citation.value}`}
                </div>
              )}
              <div className="text-slate-400">{citation.reason}</div>
              <div className="text-slate-500">{new Date(citation.observed_at).toLocaleString()}</div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
