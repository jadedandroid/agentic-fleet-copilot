"""Optional LLM judge for grounding (RAGAS 0.4.x), run with either the server's key or a caller's own key.

Off unless a caller asks for it. It sends the question, the answer, and each citation's text to OpenAI. Raw telemetry
rows are never sent. A caller's key is used for that one call only: it is never stored, logged, audited, or written
to disk.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ENABLE_FLAG = "AFC_RAGAS_ENABLED"
REPORT_DIR = Path(__file__).resolve().parents[1] / "evals" / "reports"
SAMPLE_COLUMNS = {"user_input", "response", "retrieved_contexts", "reference"}
JUDGE_MODEL = "gpt-4o-mini"


class JudgeUnavailable(Exception):
    """The judge cannot run. The message is safe to show the user; it never contains a key."""


def judge_status() -> tuple[bool, str]:
    """Server-key gate for the test suite: needs the flag and an environment key."""
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


def run_judge(
    samples: list[dict[str, Any]],
    api_key: str | None = None,
    write_report: bool = True,
) -> dict[str, Any]:
    """Score faithfulness and context precision. Uses `api_key` when given, otherwise OPENAI_API_KEY from the environment.

    Raises JudgeUnavailable with a key-free message when the judge cannot run.
    """
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        raise JudgeUnavailable("No OpenAI key was provided.")
    try:
        from openai import OpenAI
        from ragas import EvaluationDataset, evaluate
        from ragas.dataset_schema import SingleTurnSample
        from ragas.llms import llm_factory
        from ragas.metrics import context_precision, faithfulness
    except ImportError as error:
        raise JudgeUnavailable("RAGAS is not installed on the server. Run: pip install -r requirements-evals.txt") from error

    try:
        llm = llm_factory(JUDGE_MODEL, client=OpenAI(api_key=key))
        dataset = EvaluationDataset(samples=[SingleTurnSample(**sample) for sample in samples])
        # raise_exceptions surfaces metric failures. By default RAGAS records them as NaN and reports nothing useful.
        frame = evaluate(
            dataset=dataset, metrics=[faithfulness, context_precision], llm=llm, raise_exceptions=True
        ).to_pandas()
    except Exception as error:  # the provider error may echo request details, so only its type is reported
        raise JudgeUnavailable(f"The judge call failed ({type(error).__name__}). Check the key and try again.") from None

    scores = {
        column: float(frame[column].mean())
        for column in frame.columns
        if column not in SAMPLE_COLUMNS and frame[column].dtype.kind == "f"
    }
    missing = [name for name, value in scores.items() if value != value]  # NaN never equals itself
    if not scores or missing:
        raise JudgeUnavailable(
            f"The judge returned no usable score for: {', '.join(missing) or 'any metric'}. No report was written."
        )
    report = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "judge_model": JUDGE_MODEL,
        "samples": len(samples),
        "scores": scores,
        "note": "Model-based scores. They vary between runs and are not a pass or fail gate by themselves.",
    }
    if write_report:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        (REPORT_DIR / f"ragas-{stamp}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
