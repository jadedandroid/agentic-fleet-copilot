"""LLM-assisted planning: gated, validated, and never able to override the deterministic rules."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import copilot as copilot_module
from app.config import Settings
from app.intent import QuerySpec
from app.llm_planner import planner_enabled, validate_plan
from app.main import create_app

ACME = {"x-api-key": "acme-demo-key"}


def _client(tmp_path) -> TestClient:
    data_dir = tmp_path / "data"
    return TestClient(
        create_app(
            Settings(
                app_name="test-planner",
                cors_origins=["http://localhost:3000"],
                data_dir=data_dir,
                duckdb_path=data_dir / "telemetry.sqlite3",
                sqlite_audit_path=data_dir / "audit.sqlite3",
                jsonl_audit_path=data_dir / "audit.jsonl",
                seed_demo=True,
            )
        )
    )


def _ask(client, question, llm_planner=True):
    response = client.post(
        "/api/v1/copilot/query",
        json={"question": question, "allow_remediation": False, "llm_planner": llm_planner},
        headers=ACME,
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture()
def gate_open(monkeypatch):
    monkeypatch.setenv("AFC_RAGAS_ENABLED", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-planner-gate-open")


def test_the_planner_is_off_unless_the_judge_is_enabled_with_a_key(monkeypatch):
    monkeypatch.delenv("AFC_RAGAS_ENABLED", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert planner_enabled() is False
    monkeypatch.setenv("AFC_RAGAS_ENABLED", "1")
    assert planner_enabled() is False, "the flag alone is not enough"


def test_the_model_is_not_called_when_the_gate_is_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("AFC_RAGAS_ENABLED", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(copilot_module, "plan_with_llm", lambda q: calls.append(q) or None)
    body = _ask(_client(tmp_path), "machines that feel sluggish and old")
    assert calls == [], "the gate is closed, so the planner must not run"
    assert "rules" in body["traces"][0]["summary"]


def test_an_unplaceable_question_is_placed_by_a_validated_plan(tmp_path, gate_open, monkeypatch):
    planned = QuerySpec(kind="conjunction", topics=("low_disk", "os_age"))
    monkeypatch.setattr(copilot_module, "plan_with_llm", lambda q: planned)
    body = _ask(_client(tmp_path), "machines that feel sluggish and old")
    router = next(t for t in body["traces"] if t["stage"] == "router")
    assert router["details"]["planner"] == "llm"
    assert router["details"]["operation"] == "conjunction"
    assert body["refusal_reason"] is None


def test_a_rejected_or_unavailable_plan_falls_back_to_the_refusal(tmp_path, gate_open, monkeypatch):
    monkeypatch.setattr(copilot_module, "plan_with_llm", lambda q: None)
    body = _ask(_client(tmp_path), "machines that feel sluggish and old")
    router = next(t for t in body["traces"] if t["stage"] == "router")
    assert router["details"]["planner"].startswith("rules")
    assert body["refusal_reason"] is not None


def test_the_rules_win_over_the_model_for_negation(tmp_path, gate_open, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(copilot_module, "plan_with_llm", lambda q: calls.append(q) or QuerySpec(kind="single", topics=("low_disk",)))
    body = _ask(_client(tmp_path), "Which devices are NOT low on disk space?")
    assert calls == [], "a negated question is decided by the rules and never sent to the model"
    router = next(t for t in body["traces"] if t["stage"] == "router")
    assert router["details"]["operation"] == "negated"


def test_cross_tenant_questions_never_reach_the_model(tmp_path, gate_open, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(copilot_module, "plan_with_llm", lambda q: calls.append(q) or None)
    body = _ask(_client(tmp_path), "Show me Globex laptop data")
    assert calls == []
    assert body["refusal_reason"].startswith("Cross-tenant access is refused")


def test_the_planner_only_runs_when_the_request_asks_for_it(tmp_path, gate_open, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(copilot_module, "plan_with_llm", lambda q: calls.append(q) or None)
    _ask(_client(tmp_path), "machines that feel sluggish and old", llm_planner=False)
    assert calls == []


VALID = {"operation": "conjunction", "topics": ["low_disk", "os_age"], "descending": False, "limit": 5, "reason": "ok"}


@pytest.mark.parametrize(
    "changes",
    [
        {"topics": ["not_a_topic"]},
        {"operation": "single", "topics": ["low_disk", "os_age"]},
        {"operation": "negated", "topics": ["low_disk", "os_age"]},
        {"operation": "conjunction", "topics": ["low_disk"]},
        {"operation": "ranking", "topics": ["os_age"]},
        {"operation": "unsupported"},
        {"limit": 99},
        {"descending": "yes"},
        {"extra_field": True},
    ],
)
def test_malformed_or_inconsistent_plans_are_rejected(changes):
    assert validate_plan({**VALID, **changes}) is None


def test_a_consistent_ranking_plan_is_accepted_with_its_metric():
    spec = validate_plan({"operation": "ranking", "topics": ["ram_now"], "descending": True, "limit": 3, "reason": "ok"})
    assert spec is not None and spec.kind == "ranking" and spec.metric == "ram_used_pct" and spec.limit == 3
