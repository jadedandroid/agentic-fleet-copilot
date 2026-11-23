"""LLM interpretation: the model may rewrite an answer only when every number and identifier in it is in the evidence."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import llm_interpreter
from app.config import Settings
from app.llm_interpreter import evidence_packet, interpreter_enabled, verify
from app.main import create_app

ACME = {"x-api-key": "acme-demo-key"}
QUESTION = "Which devices at Acme are low on disk space?"


def _finding() -> dict:
    return {
        "finding": "2 devices are low on disk space.",
        "explanation": "Free disk is below 15% on acme-desk-001 (9.5%) and acme-laptop-004 (12%).",
        "evidence": ["acme-desk-001 free disk 9.5% at 2026-06-12T10:00:00+00:00"],
        "device_ids": ["acme-desk-001", "acme-laptop-004"],
        "citations": [
            {
                "snapshot_id": "snap-1",
                "observed_at": "2026-06-12T10:00:00+00:00",
                "reason": "free disk below 15%",
                "device_id": "acme-desk-001",
                "employee_id": "emp-acme-006",
                "metric": "disk_free_pct",
                "value": 9.5,
            }
        ],
    }


PACKET = evidence_packet(_finding())
DRAFT = "2 devices are low on disk space."


def test_a_rewrite_that_uses_only_evidence_is_accepted():
    verdict = verify(
        {"answer": "acme-desk-001 has 9.5% free disk, and acme-laptop-004 is at 12%.", "device_ids": ["acme-desk-001"]},
        DRAFT,
        PACKET,
    )
    assert verdict.accepted is True
    assert "acme-desk-001" in verdict.answer


def test_an_invented_number_is_rejected():
    verdict = verify({"answer": "acme-desk-001 has 3% free disk.", "device_ids": ["acme-desk-001"]}, DRAFT, PACKET)
    assert verdict.accepted is False
    assert verdict.answer == DRAFT
    assert "3" in verdict.reason


def test_an_invented_device_in_the_text_is_rejected():
    verdict = verify(
        {"answer": "acme-desk-001 and zz-fake-999 are low on disk.", "device_ids": ["acme-desk-001"]}, DRAFT, PACKET
    )
    assert verdict.accepted is False
    assert "zz-fake-999" in verdict.reason


def test_an_invented_device_in_the_list_is_rejected():
    verdict = verify({"answer": "acme-desk-001 is low on disk.", "device_ids": ["zz-fake-999"]}, DRAFT, PACKET)
    assert verdict.accepted is False


def test_a_rewrite_must_name_an_evidence_device():
    verdict = verify({"answer": "Two laptops need attention.", "device_ids": []}, DRAFT, PACKET)
    assert verdict.accepted is False
    assert "none of the devices" in verdict.reason


def test_hyphenated_words_without_digits_are_not_identifiers():
    verdict = verify(
        {"answer": "acme-desk-001 is running low on disk, a low-disk case.", "device_ids": ["acme-desk-001"]},
        DRAFT,
        PACKET,
    )
    assert verdict.accepted is True


def test_extra_fields_and_long_answers_are_rejected():
    extra = verify({"answer": "acme-desk-001 is low.", "device_ids": ["acme-desk-001"], "score": 1}, DRAFT, PACKET)
    assert extra.accepted is False

    long = verify({"answer": "acme-desk-001 " * 200, "device_ids": ["acme-desk-001"]}, DRAFT, PACKET)
    assert long.accepted is False


def test_the_gate_is_closed_without_the_flag_and_the_key(monkeypatch):
    monkeypatch.delenv("AFC_RAGAS_ENABLED", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert interpreter_enabled() is False


@pytest.fixture()
def client(tmp_path) -> TestClient:
    data_dir = tmp_path / "data"
    return TestClient(
        create_app(
            Settings(
                app_name="test-interpreter",
                cors_origins=["http://localhost:3000"],
                data_dir=data_dir,
                duckdb_path=data_dir / "telemetry.sqlite3",
                sqlite_audit_path=data_dir / "audit.sqlite3",
                jsonl_audit_path=data_dir / "audit.jsonl",
                seed_demo=True,
            )
        )
    )


@pytest.fixture()
def gate_open(monkeypatch):
    monkeypatch.setenv("AFC_RAGAS_ENABLED", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-interpreter-gate-open")


def _ask(client, question=QUESTION, llm_interpret=True):
    response = client.post(
        "/api/v1/copilot/query",
        json={"question": question, "allow_remediation": False, "llm_interpret": llm_interpret},
        headers=ACME,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _interpret_trace(body) -> dict:
    matches = [trace for trace in body["traces"] if trace["stage"] == "interpret"]
    assert len(matches) == 1
    return matches[0]


def test_the_model_is_not_called_unless_requested(client, gate_open, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(llm_interpreter, "call_model", lambda *args: calls.append("called") or None)
    body = _ask(client, llm_interpret=False)
    assert calls == []
    assert not [trace for trace in body["traces"] if trace["stage"] == "interpret"]


def test_a_closed_gate_keeps_the_deterministic_answer(client, monkeypatch):
    monkeypatch.delenv("AFC_RAGAS_ENABLED", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(llm_interpreter, "call_model", lambda *args: calls.append("called") or None)
    baseline = _ask(client, llm_interpret=False)
    body = _ask(client, llm_interpret=True)
    assert calls == []
    assert body["answer"] == baseline["answer"]
    assert _interpret_trace(body)["details"]["accepted"] is False


def test_a_fabricated_rewrite_never_reaches_the_user(client, gate_open, monkeypatch):
    baseline = _ask(client, llm_interpret=False)
    device = baseline["citations"][0]["device_id"]
    monkeypatch.setattr(
        llm_interpreter,
        "call_model",
        lambda *args: {"answer": f"{device} and zz-fake-999 need action.", "device_ids": [device, "zz-fake-999"]},
    )
    body = _ask(client, llm_interpret=True)
    assert body["answer"] == baseline["answer"]
    assert _interpret_trace(body)["details"]["accepted"] is False
    assert "zz-fake-999" not in body["answer"]


def test_a_verified_rewrite_is_used_and_citations_are_unchanged(client, gate_open, monkeypatch):
    baseline = _ask(client, llm_interpret=False)
    device = baseline["citations"][0]["device_id"]
    rewrite = f"{device} is the device to check first."
    monkeypatch.setattr(llm_interpreter, "call_model", lambda *args: {"answer": rewrite, "device_ids": [device]})
    body = _ask(client, llm_interpret=True)
    assert body["answer"] == rewrite
    assert _interpret_trace(body)["details"]["accepted"] is True
    assert body["citations"] == baseline["citations"]
    assert body["insights"] == baseline["insights"]


def test_refusals_are_never_sent_to_the_model(client, gate_open, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(llm_interpreter, "call_model", lambda *args: calls.append("called") or None)
    body = _ask(client, question="Which devices at Globex are low on disk space?", llm_interpret=True)
    assert body["refusal_reason"]
    assert calls == []
