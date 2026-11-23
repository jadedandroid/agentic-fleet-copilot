"""Optional LLM judge for grounding (RAGAS 0.4.x).

Off by default. It runs only when AFC_RAGAS_ENABLED=1 and OPENAI_API_KEY are both set, because it sends each
answer and its cited evidence to an external model. Raw telemetry rows never leave the machine: the judge sees
only the question, the answer, and the citation text.

Scores are model-based and vary between runs. The deterministic checks in tests/ remain the gate.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ENABLE_FLAG = "AFC_RAGAS_ENABLED"
REPORT_DIR = Path(__file__).resolve().parent / "reports"
SAMPLE_COLUMNS = {"user_input", "response", "retrieved_contexts", "reference"}


def judge_status() -> tuple[bool, str]:
    """Return (enabled, reason). The reason explains why the judge is off, for skip messages."""
    if os.getenv(ENABLE_FLAG) != "1":
        return False, f"set {ENABLE_FLAG}=1 to run the RAGAS judge (it sends evidence to an external LLM)"
    if not os.getenv("OPENAI_API_KEY"):
        return False, "OPENAI_API_KEY is not set"
    return True, ""


def citation_text(citation: dict[str, Any]) -> str:
    """Render one citation as the evidence sentence the judge sees. Only the cited fields are included."""
    subject = citation.get("device_id") or citation.get("snapshot_id", "unknown")
    if citation.get("metric"):
        return (
            f"{subject} {citation['metric']} = {citation.get('value')} "
            f"({citation.get('reason')}, observed {citation.get('observed_at')})"
        )
    return f"{subject}: {citation.get('reason')} (observed {citation.get('observed_at')})"


def build_samples(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each case: {question, answer, citations}. Samples keep only what grounding needs."""
    samples = []
    for case in cases:
        contexts = [citation_text(c) for c in case.get("citations", [])]
        # context_precision needs a reference. The deterministic answer is the reference, so the judge asks whether each
        # cited context supports that answer. This checks the citations, not an independent ground truth.
        samples.append(
            {
                "user_input": case["question"],
                "response": case["answer"],
                "retrieved_contexts": contexts,
                "reference": case["answer"],
            }
        )
    return samples


def run_judge(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Score faithfulness and context precision with an OpenAI model. Writes a JSON report and returns the means."""
    enabled, reason = judge_status()
    if not enabled:
        raise RuntimeError(reason)
    try:
        from openai import OpenAI
        from ragas import EvaluationDataset, evaluate
        from ragas.dataset_schema import SingleTurnSample
        from ragas.llms import llm_factory
        from ragas.metrics import context_precision, faithfulness
    except ImportError as error:
        raise RuntimeError("RAGAS is not installed. Run: pip install -r requirements-evals.txt") from error

    llm = llm_factory("gpt-4o-mini", client=OpenAI(api_key=os.environ["OPENAI_API_KEY"]))
    dataset = EvaluationDataset(samples=[SingleTurnSample(**sample) for sample in samples])
    frame = evaluate(dataset=dataset, metrics=[faithfulness, context_precision], llm=llm).to_pandas()

    scores = {
        column: float(frame[column].mean())
        for column in frame.columns
        if column not in SAMPLE_COLUMNS and frame[column].dtype.kind == "f"
    }
    report = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "judge_model": "gpt-4o-mini",
        "samples": len(samples),
        "scores": scores,
        "note": "Model-based scores. They vary between runs and are not a pass or fail gate by themselves.",
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    (REPORT_DIR / f"ragas-{stamp}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
