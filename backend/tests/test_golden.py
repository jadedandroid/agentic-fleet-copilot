"""Golden-answer regression: stored expectations for a seeded demo fleet.

Compares routing, refusal, proposed tool, cited devices, and answer prefix.
Exact answer text is not compared, so wording changes that keep the
grounded facts do not break the suite. Run twice to confirm determinism.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ACME = {"x-api-key": "acme-demo-key"}
GOLDEN = json.loads((Path(__file__).parent / "golden" / "copilot_golden.json").read_text())


def _client(tmp_path) -> TestClient:
    data_dir = tmp_path / "data"
    settings = Settings(
        app_name="test-golden",
        cors_origins=["http://localhost:3000"],
        data_dir=data_dir,
        duckdb_path=data_dir / "telemetry.sqlite3",
        sqlite_audit_path=data_dir / "audit.sqlite3",
        jsonl_audit_path=data_dir / "audit.jsonl",
        seed_demo=True,
    )
    return TestClient(create_app(settings))


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> TestClient:
    return _client(tmp_path_factory.mktemp("golden"))


def _ask(client, question: str) -> dict:
    response = client.post(
        "/api/v1/copilot/query",
        json={"question": question, "allow_remediation": True},
        headers=ACME,
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("case", GOLDEN, ids=[c["question"][:40] for c in GOLDEN])
def test_golden_case_matches_expectations(client, case):
    body = _ask(client, case["question"])
    assert body["intent"] == case["intent"]
    assert bool(body.get("refusal_reason")) is case["refusal"]
    proposed = body.get("proposed_action")
    assert (proposed["tool_name"] if proposed else None) == case["tool"]
    assert sorted({c["device_id"] for c in body.get("citations", [])}) == sorted(case["devices"])
    assert body["answer"].startswith(case["answer_startswith"])


@pytest.mark.parametrize("case", GOLDEN, ids=[c["question"][:40] for c in GOLDEN])
def test_golden_case_is_deterministic(client, case):
    first = _ask(client, case["question"])
    second = _ask(client, case["question"])
    assert first["answer"] == second["answer"]
    assert first["citations"] == second["citations"]
