import { PIPELINE_STAGES, PipelineStage } from "@/lib/pipeline";

type Props = {
  stages?: PipelineStage[];
};

export function PipelineDiagram({ stages = PIPELINE_STAGES }: Props) {
  return (
    <ol className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
      {stages.map((stage, index) => (
        <li key={stage.key} className="rounded-xl border border-slate-800 bg-panel p-4">
          <div className="flex items-center gap-2">
            <span className="flex h-7 w-7 items-center justify-center rounded-full bg-cyan-500 text-sm font-bold text-slate-900">
              {index + 1}
            </span>
            <h3 className="font-semibold text-slate-100">{stage.title}</h3>
          </div>
          <p className="mt-2 text-sm text-slate-300">{stage.description}</p>
          <p className="mt-3 rounded-md bg-panelSoft p-2 text-xs text-emerald-200">
            Guardrail: {stage.guardrail}
          </p>
        </li>
      ))}
    </ol>
  );
}
