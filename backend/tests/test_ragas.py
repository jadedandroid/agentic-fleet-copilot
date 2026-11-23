"""RAGAS judge: the gate and sample construction are tested by default. The judge itself is opt-in (-m ragas)."""

from __future__ import annotations

import pytest

from evals.ragas_judge import build_samples, citation_text, judge_status


def test_judge_is_off_by_default(monkeypatch):
    monkeypatch.delenv("AFC_RAGAS_ENABLED", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    enabled, reason = judge_status()
    assert enabled is False
    assert "AFC_RAGAS_ENABLED" in reason


def test_judge_needs_both_the_flag_and_a_key(monkeypatch):
    monkeypatch.setenv("AFC_RAGAS_ENABLED", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    enabled, reason = judge_status()
    assert enabled is False
    assert "OPENAI_API_KEY" in reason


def test_metric_citations_render_the_cited_value_and_time():
    text = citation_text(
        {"device_id": "acme-lap-001", "metric": "disk_free_pct", "value": 9.5, "reason": "latest", "observed_at": "t1"}
    )
    assert text == "acme-lap-001 disk_free_pct = 9.5 (latest, observed t1)"


def test_compliance_style_citations_render_without_a_metric():
    text = citation_text({"device_id": "acme-lap-005", "metric": None, "value": None, "reason": "drift", "observed_at": "t2"})
    assert text == "acme-lap-005: drift (observed t2)"


def test_samples_carry_only_question_answer_and_citation_text():
    samples = build_samples(
        [
            {
                "question": "Which devices at Acme are low on disk space?",
                "answer": "2 device(s) below 15% free disk.",
                "citations": [{"device_id": "acme-lap-001", "metric": "disk_free_pct", "value": 9.5, "reason": "r", "observed_at": "t"}],
                "raw_rows": "must not be sent",
            }
        ]
    )
    assert set(samples[0]) == {"user_input", "response", "retrieved_contexts", "reference"}
    assert samples[0]["retrieved_contexts"] == ["acme-lap-001 disk_free_pct = 9.5 (r, observed t)"]


@pytest.mark.ragas
def test_ragas_judge_scores_grounded_answers(tmp_path):
    """Runs only with `pytest -m ragas`, AFC_RAGAS_ENABLED=1, and OPENAI_API_KEY. Calls an external model."""
    enabled, reason = judge_status()
    if not enabled:
        pytest.skip(reason)
    from evals.ragas_judge import run_judge

    samples = build_samples(
        [
            {
                "question": "Which devices at Acme are low on disk space?",
                "answer": "2 device(s) below 15% free disk: acme-lap-001 (9.5%), acme-lap-003 (12.0%).",
                "citations": [
                    {"device_id": "acme-lap-001", "metric": "disk_free_pct", "value": 9.5, "reason": "latest disk reading below threshold", "observed_at": "2026-06-12T11:00:00Z"},
                    {"device_id": "acme-lap-003", "metric": "disk_free_pct", "value": 12.0, "reason": "latest disk reading below threshold", "observed_at": "2026-06-12T11:00:00Z"},
                ],
            }
        ]
    )
    report = run_judge(samples)
    assert report["samples"] == 1
    assert report["scores"], "the judge returned no scores"
    assert all(0.0 <= value <= 1.0 for value in report["scores"].values())
