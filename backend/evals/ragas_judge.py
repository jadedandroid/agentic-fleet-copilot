"""Test-suite entry point for the grounding judge. The implementation lives in app/grounding_judge.py so the
running service can use the same judge with a caller-supplied key."""

from __future__ import annotations

from app.grounding_judge import (  # noqa: F401  (re-exported for tests)
    ENABLE_FLAG,
    REPORT_DIR,
    SAMPLE_COLUMNS,
    build_samples,
    citation_text,
    judge_status,
    run_judge,
)
