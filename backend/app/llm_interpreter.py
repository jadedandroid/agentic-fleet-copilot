"""Verified LLM interpretation: the model rewrites the deterministic answer, and the rewrite is used only if it passes checks.

The deterministic answer is always computed first. The model sees that draft and the evidence it came from, and returns
a short rewrite plus the device IDs it names. The rewrite is accepted only when every number and every device or
employee ID in it appears in the evidence, and it names at least one device from the evidence. Otherwise the
deterministic answer is kept. Citations, insights, and proposed actions never come from the model.

Unlike the planner, this does send evidence (device and employee IDs, metrics, timestamps) to OpenAI, so it is opt-in per
request and gated by the same server key.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from .llm_planner import planner_enabled

INTERPRET_MODEL = "gpt-4o-mini"
MAX_ANSWER_CHARS = 800

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "maxLength": MAX_ANSWER_CHARS},
        "device_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
    },
    "required": ["answer", "device_ids"],
    "additionalProperties": False,
}
_PROMPT = (
    "You rewrite a fleet-telemetry answer for an IT administrator. You receive the question, a draft answer, and the "
    "EVIDENCE the draft was built from. Rewrite the draft in two to four plain sentences. Use only facts that appear "
    "in the EVIDENCE: do not add numbers, dates, device or employee IDs, causes, or recommendations. List in device_ids "
    "every device ID your answer names. Reply with JSON only."
)

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
# Identifiers such as acme-desk-001 or emp-acme-006. Hyphenated words without a digit (low-disk) are not identifiers.
_IDENTIFIER = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\b")


class InterpretedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: StrictStr = Field(min_length=1, max_length=MAX_ANSWER_CHARS)
    device_ids: list[StrictStr] = Field(max_length=20)


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    answer: str
    reason: str


def interpreter_enabled() -> bool:
    """The same gate as the planner: the judge flag and a server key must both be set."""
    return planner_enabled()


def evidence_packet(finding: dict[str, Any]) -> dict[str, Any]:
    """The only facts the model may use. It is also the reference the verifier checks against."""
    return {
        "finding": finding["finding"],
        "explanation": finding["explanation"],
        "evidence": finding["evidence"],
        "device_ids": finding["device_ids"],
        "citations": [
            {
                "device_id": citation.get("device_id"),
                "employee_id": citation.get("employee_id"),
                "metric": citation.get("metric"),
                "value": citation.get("value"),
                "observed_at": citation.get("observed_at"),
            }
            for citation in finding["citations"]
        ],
    }


def _numbers(text: str) -> set[float]:
    return {float(match) for match in _NUMBER.findall(text)}


def _identifiers(text: str) -> set[str]:
    return {token for token in _IDENTIFIER.findall(text.lower()) if any(char.isdigit() for char in token)}


def verify(raw: dict[str, Any], draft: str, packet: dict[str, Any]) -> Verdict:
    """Accept the model's rewrite only if it is well-formed and uses nothing outside the evidence."""
    try:
        parsed = InterpretedAnswer.model_validate(raw)
    except ValidationError:
        return Verdict(False, draft, "the model's reply did not match the schema")

    evidence_text = json.dumps(packet, default=str)
    allowed_numbers = _numbers(evidence_text)
    allowed_ids = _identifiers(evidence_text)

    named_ids = _identifiers(parsed.answer) | {identifier.lower() for identifier in parsed.device_ids}
    unknown_ids = sorted(named_ids - allowed_ids)
    if unknown_ids:
        return Verdict(False, draft, f"an identifier is not in the evidence: {unknown_ids[0]}")

    unknown_numbers = sorted(_numbers(parsed.answer) - allowed_numbers)
    if unknown_numbers:
        return Verdict(False, draft, f"a number is not in the evidence: {unknown_numbers[0]:g}")

    evidence_devices = {device.lower() for device in packet["device_ids"]}
    if evidence_devices and not evidence_devices & named_ids:
        return Verdict(False, draft, "the answer names none of the devices in the evidence")

    return Verdict(True, parsed.answer, "verified against the evidence")


def call_model(question: str, draft: str, packet: dict[str, Any]) -> dict[str, Any] | None:
    """One chat call with a strict JSON schema. Any failure returns None and the draft is kept."""
    try:
        from openai import OpenAI

        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=15.0)
        response = client.chat.completions.create(
            model=INTERPRET_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": json.dumps({"question": question, "draft": draft, "evidence": packet})},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "interpreted_answer", "strict": True, "schema": _SCHEMA},
            },
        )
        return json.loads(response.choices[0].message.content or "{}")
    except Exception:  # network, auth, or malformed output: the deterministic answer stands
        return None


def interpret_answer(question: str, draft: str, finding: dict[str, Any]) -> Verdict:
    if not interpreter_enabled():
        return Verdict(False, draft, "LLM interpretation is not enabled on this server")
    packet = evidence_packet(finding)
    raw = call_model(question, draft, packet)
    if raw is None:
        return Verdict(False, draft, "the model call failed")
    return verify(raw, draft, packet)
