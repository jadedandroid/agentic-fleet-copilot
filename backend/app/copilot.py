"""Deterministic LangGraph copilot.

Graph: route_intent -> retrieve_and_analyze -> compose_answer -> propose_action -> approval_gate.
Every node is a pure function of state plus the store, so the same question over the same
telemetry always yields the same trace, answer, and proposal. The LLM is not in this path.
"""

from __future__ import annotations

import operator
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from .analytics import (
    FleetData,
    Finding,
    battery_eol,
    compliance_drift,
    failing_compliance,
    os_age,
    low_disk,
    ram_constrained,
    ram_now,
    storage_constrained,
)
from .audit import AuditLogger
from . import compound
from .intent import OUT_OF_SCOPE_REFUSAL, QuerySpec, device_type_scope, parse_query
from .models import (
    MAX_LOOKBACK_MINUTES,
    Citation,
    CopilotQueryRequest,
    CopilotQueryResponse,
    Insight,
    ProposedAction,
    TraceStep,
)
from .store import TelemetryStore

EVIDENCE_THRESHOLD = 0.7
SAMPLE_SATURATION = 3

ANALYSES: dict[str, Callable[[FleetData, str | None], Finding]] = {
    "low_disk": low_disk,
    "storage_trend": storage_constrained,
    "ram_constrained": ram_constrained,
    "ram_now": ram_now,
    "failing_compliance": failing_compliance,
    "os_age": os_age,
    "battery_eol": battery_eol,
    "compliance_drift": compliance_drift,
}


class CopilotState(TypedDict, total=False):
    thread_id: str
    tenant_id: str
    question: str
    window_minutes: int
    as_of: str
    allow_remediation: bool
    intent: str
    device_scope: str | None
    query: dict[str, Any]
    refusal_reason: str | None
    finding: dict[str, Any] | None
    answer: str
    proposed_action: dict[str, Any] | None
    # Set when a proposal is refused for weak evidence and the lookback is widened once for a second pass.
    replan: bool
    widened: bool
    traces: Annotated[list[dict[str, Any]], operator.add]


def _trace(stage: str, summary: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"stage": stage, "summary": summary, "details": details or {}}


def evidence_score(finding: dict[str, Any]) -> float:
    """0.5 for a device that exists in the tenant inventory, plus up to 0.5 for repeated samples."""
    inventory = 0.5 if finding.get("worst_device_id") else 0.0
    return round(inventory + 0.5 * min(1.0, finding["sample_count"] / SAMPLE_SATURATION), 3)


def _build_proposal(finding: dict[str, Any]) -> tuple[str, dict[str, Any], str, str] | None:
    """Map a finding to one state-changing tool call. Returns (tool, payload, title, summary)."""
    device = finding.get("worst_device_id")
    details = finding.get("details", {})
    kind = finding["kind"]
    if not device:
        return None
    if kind in ("low_disk", "storage_trend"):
        return (
            "create_upgrade_order",
            {"device_id": device, "component": "storage", "spec": "512GB SSD minimum"},
            f"Upgrade storage on {device}",
            "Free disk space is persistently low; propose a 512GB SSD minimum.",
        )
    if kind == "ram_constrained":
        return (
            "create_upgrade_order",
            {"device_id": device, "component": "memory", "spec": "32GB"},
            f"Upgrade memory on {device}",
            "Memory use is persistently at or above 90%; propose a 32GB upgrade.",
        )
    if kind == "failing_compliance":
        return (
            "open_remediation_ticket",
            {"device_id": device, "check_id": details["check_id"], "note": finding["finding"]},
            f"Open remediation ticket for {device}",
            f"Failing high-severity check {details['check_id']}; open a ticket for remediation.",
        )
    if kind == "battery_eol":
        return (
            "flag_device_for_replacement",
            {
                "device_id": device,
                "reason": (
                    f"Battery health projected to reach {details['eol_threshold_pct']:.0f}% "
                    f"in about {details['projected_days']:.0f} days"
                ),
            },
            f"Flag {device} for replacement",
            "Battery end-of-life is projected within the planning horizon.",
        )
    if kind == "os_age":
        return (
            "notify_employee",
            {
                "employee_id": details["employee_id"],
                "message": "Your Mac runs an OS older than macOS 15. Please schedule an upgrade with IT.",
            },
            f"Notify employee {details['employee_id']}",
            "Device OS is older than the supported baseline; notify the owner.",
        )
    return None


def _insight(finding: dict[str, Any]) -> Insight:
    return Insight(
        kind=finding["kind"],
        finding=finding["finding"],
        evidence=finding["evidence"],
        explanation=finding["explanation"],
        severity=finding["severity"],
        device_ids=finding["device_ids"],
    )


def _citations(finding: dict[str, Any] | None) -> list[Citation]:
    return [Citation(**item) for item in (finding or {}).get("citations", [])]


def _after_route(state: CopilotState) -> str:
    return "compose_answer" if state.get("refusal_reason") else "plan_steps"


def _after_propose(state: CopilotState) -> str:
    if state.get("replan"):
        return "retrieve_and_analyze"
    return "approval_gate" if state.get("proposed_action") else END


class FleetCopilotService:
    def __init__(
        self,
        store: TelemetryStore,
        audit: AuditLogger,
        known_tenants: Callable[[], Iterable[tuple[str, str]]],
        checkpoint_path: Path,
    ) -> None:
        self._store = store
        self._audit = audit
        # Called per query so tenants created at runtime are recognized by the cross-tenant check.
        self._known_tenants = known_tenants
        # Pending approvals are checkpointed to disk, so they survive a backend restart.
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self._checkpoint_conn = sqlite3.connect(checkpoint_path, check_same_thread=False)
        self._checkpointer = SqliteSaver(self._checkpoint_conn)
        self._checkpointer.setup()
        # One connection is shared by request threads, so graph operations run one at a time.
        self._lock = threading.RLock()
        self._graph = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(CopilotState)
        builder.add_node("route_intent", self._route_intent)
        builder.add_node("plan_steps", self._plan_steps)
        builder.add_node("retrieve_and_analyze", self._retrieve_and_analyze)
        builder.add_node("compose_answer", self._compose_answer)
        builder.add_node("propose_action", self._propose_action)
        builder.add_node("approval_gate", self._approval_gate)
        builder.set_entry_point("route_intent")
        builder.add_conditional_edges(
            "route_intent",
            _after_route,
            {"compose_answer": "compose_answer", "plan_steps": "plan_steps"},
        )
        builder.add_edge("plan_steps", "retrieve_and_analyze")
        builder.add_edge("retrieve_and_analyze", "compose_answer")
        builder.add_edge("compose_answer", "propose_action")
        # The one cycle in the graph: weak evidence sends control back to retrieval with a wider window.
        builder.add_conditional_edges(
            "propose_action",
            _after_propose,
            {"approval_gate": "approval_gate", "retrieve_and_analyze": "retrieve_and_analyze", END: END},
        )
        builder.add_edge("approval_gate", END)
        return builder.compile(checkpointer=self._checkpointer)

    def _route_intent(self, state: CopilotState) -> dict[str, Any]:
        spec: QuerySpec = parse_query(state["question"], state["tenant_id"], tuple(self._known_tenants()))
        scope = device_type_scope(state["question"])
        refusal = None
        if spec.kind in ("cross_tenant", "unsupported"):
            refusal = spec.refusal or OUT_OF_SCOPE_REFUSAL
        intent = spec.topics[0] if spec.kind == "single" else spec.kind
        query = {
            "kind": spec.kind,
            "topics": list(spec.topics),
            "descending": spec.descending,
            "limit": spec.limit,
            "metric": spec.metric,
        }
        return {
            "intent": intent,
            "device_scope": scope,
            "query": query,
            "refusal_reason": refusal,
            "traces": [
                _trace(
                    "router",
                    f"Classified question as {spec.label}",
                    {"intent": intent, "operation": spec.kind, "topics": list(spec.topics), "device_scope": scope},
                )
            ],
        }

    def _retrieve_and_analyze(self, state: CopilotState) -> dict[str, Any]:
        tenant = state["tenant_id"]
        window = state["window_minutes"]
        as_of = datetime.fromisoformat(state["as_of"])
        devices = self._store.list_devices(tenant)
        metrics = self._store.get_device_metrics(tenant, window, as_of=as_of)
        compliance = self._store.get_compliance_results(tenant, window, as_of=as_of)
        data = FleetData(devices, metrics, compliance)
        traces = [
            _trace(
                "retrieval",
                f"Loaded {len(devices)} devices, {len(metrics)} metric points, "
                f"{len(compliance)} compliance results from the last {window} minutes",
                {
                    "devices": len(devices),
                    "metric_points": len(metrics),
                    "compliance_results": len(compliance),
                    "as_of": as_of.isoformat(),
                },
            )
        ]
        query = state.get("query") or {"kind": "single"}
        if query["kind"] in ("negated", "ranking", "conjunction"):
            spec = QuerySpec(
                kind=query["kind"],
                topics=tuple(query["topics"]),
                descending=query["descending"],
                limit=query["limit"],
                metric=query["metric"],
            )
            try:
                finding = compound.run(spec, data, state.get("device_scope"), ANALYSES)
            except compound.Unanswerable as reason:
                return {"finding": None, "refusal_reason": str(reason), "replan": False, "traces": [
                    _trace("guardrail", "Refused: telemetry cannot answer this question", {"reason": str(reason)})
                ]}
        else:
            finding = ANALYSES[state["intent"]](data, state.get("device_scope"))
        finding_dict = asdict(finding)
        traces.append(
            _trace(
                "analysis",
                finding.finding,
                {"kind": finding.kind, "device_ids": finding.device_ids, "sample_count": finding.sample_count},
            )
        )
        # Inventory is not time-filtered, so citations alone do not prove telemetry exists in the window.
        # With no in-window readings, a "none found" answer would be an unsupported conclusion, so refuse instead.
        has_telemetry = bool(metrics or compliance)
        refusal = None if (finding.citations and has_telemetry) else "No telemetry evidence exists for this tenant in the window."
        employees = {device.device_id: device.employee_id for device in devices}
        for citation in finding_dict["citations"]:
            citation["employee_id"] = employees.get(citation.get("device_id") or "")
        # Without battery readings, "no battery is projected to fail" would be a false clean result. Refuse instead.
        if state["intent"] == "battery_eol" and not any(m.metric == "battery_health_pct" for m in metrics):
            refusal = "Battery health is not reported for this tenant, so end-of-life cannot be assessed from the telemetry."
        return {"finding": finding_dict, "refusal_reason": refusal, "replan": False, "traces": traces}

    def _plan_steps(self, state: CopilotState) -> dict[str, Any]:
        """Record the plan before any data is read, so the trace shows what the run intends to do and why."""
        steps = [
            f"retrieve telemetry for the last {state['window_minutes']} minutes up to {state['as_of']}",
            f"run the {state['intent']} analysis",
            "compose an answer cited to the evidence",
        ]
        if state.get("allow_remediation"):
            steps.append(
                f"propose an action only if the evidence score reaches {EVIDENCE_THRESHOLD:.2f}; "
                "otherwise widen the lookback once and re-analyze"
            )
        else:
            steps.append("no action: remediation was not requested")
        return {"traces": [_trace("plan", f"Planned {len(steps)} steps for {state['intent']}", {"steps": steps})]}

    def _compose_answer(self, state: CopilotState) -> dict[str, Any]:
        if state.get("refusal_reason"):
            return {
                "answer": f"Refused: {state['refusal_reason']}",
                "traces": [_trace("guardrail", "Refused to answer", {"reason": state["refusal_reason"]})],
            }
        finding = state["finding"]
        return {
            "answer": f"{finding['finding']} {finding['explanation']}",
            "traces": [
                _trace(
                    "compose",
                    "Composed a grounded answer from cited telemetry",
                    {"citations": len(finding["citations"])},
                )
            ],
        }

    def _propose_action(self, state: CopilotState) -> dict[str, Any]:
        finding = state.get("finding")
        if state.get("refusal_reason") or not finding:
            return {"traces": []}
        if not state.get("allow_remediation"):
            return {"traces": [_trace("policy", "Remediation not requested; no action proposed")]}
        spec = _build_proposal(finding)
        if spec is None:
            return {"traces": [_trace("policy", f"No state-changing action applies to {finding['kind']}")]}
        tool_name, tool_payload, title, summary = spec
        score = evidence_score(finding)
        if score < EVIDENCE_THRESHOLD and not state.get("widened") and state["window_minutes"] < MAX_LOOKBACK_MINUTES:
            # One bounded re-plan: widen the lookback to the maximum and analyze again before giving up.
            return {
                "replan": True,
                "widened": True,
                "window_minutes": MAX_LOOKBACK_MINUTES,
                "traces": [
                    _trace(
                        "replan",
                        f"Evidence score {score:.2f} is below {EVIDENCE_THRESHOLD:.2f}; widening the lookback from "
                        f"{state['window_minutes']} to {MAX_LOOKBACK_MINUTES} minutes and re-analyzing once",
                        {"evidence_score": score, "from_minutes": state["window_minutes"], "to_minutes": MAX_LOOKBACK_MINUTES},
                    )
                ],
            }
        if score < EVIDENCE_THRESHOLD:
            return {
                "replan": False,
                "traces": [
                    _trace(
                        "guardrail",
                        f"Refused to propose {tool_name}: evidence score {score:.2f} is below "
                        f"{EVIDENCE_THRESHOLD:.2f} ({finding['sample_count']} sample(s))",
                        {"evidence_score": score, "sample_count": finding["sample_count"]},
                    )
                ]
            }

        tenant = state["tenant_id"]
        action_id = f"act-{uuid4().hex[:12]}"
        proposal = ProposedAction(
            action_id=action_id,
            title=title,
            summary=summary,
            risk_level=finding["severity"],
            tool_name=tool_name,
            tool_payload=tool_payload,
            evidence_score=score,
            requires_approval=True,
            status="pending",
        ).model_dump(mode="json")
        self._audit.record_action(
            action_id,
            tenant,
            {
                "thread_id": state["thread_id"],
                "question": state["question"],
                "title": title,
                "summary": summary,
                "risk_level": finding["severity"],
                "tool_name": tool_name,
                "tool_payload": tool_payload,
                "evidence_score": score,
                "citations": finding["citations"],
            },
        )
        return {
            "proposed_action": proposal,
            "traces": [
                _trace(
                    "proposal",
                    f"Proposed {tool_name}; awaiting human approval",
                    {"action_id": action_id, "evidence_score": score},
                )
            ],
        }

    def _approval_gate(self, state: CopilotState) -> dict[str, Any]:
        # Nodes re-run from the start on resume, so nothing with side effects may precede interrupt().
        proposal = state["proposed_action"]
        decision = interrupt(
            {
                "action_id": proposal["action_id"],
                "tool_name": proposal["tool_name"],
                "title": proposal["title"],
                "evidence_score": proposal["evidence_score"],
            }
        )
        return {
            "traces": [
                _trace(
                    "approval",
                    f"Reviewer {decision['reviewer']} chose {decision['decision']}",
                    {"action_id": proposal["action_id"], "decision": decision["decision"]},
                )
            ]
        }

    def stream_query(self, tenant_id: str, request: CopilotQueryRequest) -> Iterator[tuple[str, Any]]:
        """Yield (event, payload) pairs as each graph node completes. The last event is always `final`."""
        with self._lock:
            yield from self._stream(tenant_id, request)

    def _stream(self, tenant_id: str, request: CopilotQueryRequest) -> Iterator[tuple[str, Any]]:
        thread_id = str(uuid4())
        config = {"configurable": {"thread_id": thread_id}}
        inputs: CopilotState = {
            "thread_id": thread_id,
            "tenant_id": tenant_id,
            "question": request.question,
            "window_minutes": request.context_window_minutes,
            "as_of": (request.as_of or datetime.now(tz=UTC)).isoformat(),
            "allow_remediation": request.allow_remediation,
            "traces": [],
        }
        for update in self._graph.stream(inputs, config, stream_mode="updates"):
            for node, payload in update.items():
                if node == "__interrupt__":
                    for item in payload:
                        yield "interrupt", {"thread_id": thread_id, "value": item.value}
                    continue
                payload = payload or {}
                for trace in payload.get("traces", []):
                    yield "trace", trace
                if node == "retrieve_and_analyze" and payload.get("finding"):
                    yield "insights", {"items": [_insight(payload["finding"]).model_dump(mode="json")]}
                if node == "propose_action" and payload.get("proposed_action"):
                    yield "proposed_action", payload["proposed_action"]
        yield "final", self._final_response(config)

    def _final_response(self, config: dict[str, Any]) -> CopilotQueryResponse:
        snapshot = self._graph.get_state(config)
        values = snapshot.values
        finding = values.get("finding")
        companies = dict(self._store.tenant_identities())
        return CopilotQueryResponse(
            company=companies.get(values.get("tenant_id", "")),
            answer=values.get("answer", ""),
            intent=values.get("intent", "unsupported"),
            refusal_reason=values.get("refusal_reason"),
            traces=[TraceStep(**t) for t in values.get("traces", [])],
            citations=_citations(finding),
            insights=[] if values.get("refusal_reason") or not finding else [_insight(finding)],
            proposed_action=ProposedAction(**values["proposed_action"]) if values.get("proposed_action") else None,
            thread_id=values["thread_id"],
            awaiting_approval=bool(snapshot.next),
        )

    def run_query(self, tenant_id: str, request: CopilotQueryRequest) -> CopilotQueryResponse:
        final: CopilotQueryResponse | None = None
        for event, payload in self.stream_query(tenant_id, request):
            if event == "final":
                final = payload
        assert final is not None
        return final

    def resume(self, thread_id: str, decision: dict[str, Any]) -> bool:
        """Resume a paused graph with the reviewer decision. Returns False if nothing is pending."""
        config = {"configurable": {"thread_id": thread_id}}
        with self._lock:
            if not self._graph.get_state(config).next:
                return False
            self._graph.invoke(Command(resume=decision), config)
        return True
