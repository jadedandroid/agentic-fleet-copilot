"""LLM-assisted planning for questions the rule router cannot place.

Gated twice: the server must have the judge enabled (AFC_RAGAS_ENABLED=1) with an OpenAI key, and the request must ask
for it (llm_planner=true). It runs only when the rules find no supported topic at all. Negation, superlative, and
multi-topic refusals stay deterministic, so the model cannot override them.

The model's answer must match a strict schema and pass semantic checks before it is used. Anything else falls back
to the rules. Only the question text and the list of supported topics are sent to OpenAI, never telemetry.
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

from .intent import RANK_METRICS, QuerySpec

PLANNER_MODEL = "gpt-4o-mini"
TOPICS = (
    "ram_now",
    "ram_constrained",
    "low_disk",
    "storage_trend",
    "failing_compliance",
    "os_age",
    "battery_eol",
    "compliance_drift",
)
OPERATIONS = ("single", "negated", "ranking", "conjunction", "unsupported")

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "enum": list(OPERATIONS)},
        "topics": {"type": "array", "items": {"type": "string", "enum": list(TOPICS)}},
        "descending": {"type": "boolean"},
        "limit": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["operation", "topics", "descending", "limit", "reason"],
    "additionalProperties": False,
}
_PROMPT = (
    "You map a fleet-telemetry question to one supported operation. Operations: single (one topic), negated (the "
    "complement of one topic), ranking (top devices by one metric), conjunction (devices matching two or more topics), "
    "or unsupported (none of the topics fits). Topics: ram_now = current RAM use at or above 90%; ram_constrained = "
    "sustained RAM pressure; low_disk = current free disk below 15%; storage_trend = sustained low storage; "
    "failing_compliance = failing high-severity checks; os_age = macOS older than 15; battery_eol = battery nearing "
    "end-of-life; compliance_drift = compliance getting worse over time. Answer only from these. If unsure, answer "
    "unsupported."
)


class LlmPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["single", "negated", "ranking", "conjunction", "unsupported"]
    topics: list[Literal["ram_now", "ram_constrained", "low_disk", "storage_trend", "failing_compliance", "os_age", "battery_eol", "compliance_drift"]] = Field(max_length=3)
    descending: StrictBool
    limit: StrictInt = Field(ge=1, le=20)
    reason: StrictStr = Field(max_length=200)


def planner_enabled() -> bool:
    """The same gate as the judge: the flag and a server key must both be set."""
    return os.getenv("AFC_RAGAS_ENABLED") == "1" and bool(os.getenv("OPENAI_API_KEY"))


def validate_plan(raw: dict[str, Any]) -> QuerySpec | None:
    """Return a QuerySpec only when the model's answer is well-formed and semantically consistent."""
    try:
        plan = LlmPlan.model_validate(raw)
    except ValidationError:
        return None
    if plan.operation == "unsupported" or not plan.topics:
        return None
    topics = tuple(dict.fromkeys(plan.topics))  # de-duplicate, keep order
    if plan.operation in ("single", "negated") and len(topics) != 1:
        return None
    if plan.operation == "conjunction" and len(topics) < 2:
        return None
    if plan.operation == "ranking":
        if len(topics) != 1 or topics[0] not in RANK_METRICS:
            return None
        return QuerySpec(kind="ranking", topics=topics, descending=plan.descending, limit=plan.limit, metric=RANK_METRICS[topics[0]])
    return QuerySpec(kind=plan.operation, topics=topics)


def plan_with_llm(question: str) -> QuerySpec | None:
    """Ask the model for a plan. Returns a validated QuerySpec, or None when the plan is unusable or the call fails."""
    if not planner_enabled():
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=15.0)
        response = client.chat.completions.create(
            model=PLANNER_MODEL,
            temperature=0,
            messages=[{"role": "system", "content": _PROMPT}, {"role": "user", "content": question}],
            response_format={"type": "json_schema", "json_schema": {"name": "query_plan", "strict": True, "schema": _SCHEMA}},
        )
        raw = json.loads(response.choices[0].message.content or "{}")
    except Exception:  # network, auth, or malformed output: the rules answer instead
        return None
    return validate_plan(raw)
