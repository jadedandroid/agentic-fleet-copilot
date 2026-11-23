"""Answer negated, ranked, and multi-topic questions from the same deterministic analyses.

Each answer is built from the device records and citations of the existing analyses, so every device listed is
traceable to telemetry. None of these results proposes an action: a ranking or a "not" list is informational.
"""

from __future__ import annotations

from collections.abc import Callable

from .analytics import Finding, FleetData, _check_citation, _device_citation, _empty, _metric_citation
from .intent import RANK_METRICS, QuerySpec

Analysis = Callable[[FleetData, str | None], Finding]

TOPIC_LABELS = {
    "low_disk": "low on disk space",
    "storage_trend": "consistently low on storage",
    "ram_constrained": "consistently RAM-constrained",
    "failing_compliance": "failing high-severity compliance",
    "os_age": "running an OS older than macOS 15",
    "battery_eol": "approaching battery end-of-life",
    "compliance_drift": "showing compliance drift",
}
_DISK_TOPICS = ("low_disk", "storage_trend")


class Unanswerable(Exception):
    """The telemetry cannot support this question. The message is the refusal shown to the user."""


def _population(data: FleetData, topic: str, device_type: str | None):
    """Devices that report the data a topic depends on. A device without that data is not a 'not' answer."""
    devices = data.in_scope(device_type)
    if topic in _DISK_TOPICS:
        return [d for d in devices if data.series(d.device_id, "disk_free_pct")]
    if topic == "ram_constrained":
        return [d for d in devices if data.series(d.device_id, "ram_used_pct")]
    if topic == "battery_eol":
        return [d for d in devices if data.series(d.device_id, "battery_health_pct")]
    if topic in ("failing_compliance", "compliance_drift"):
        return [d for d in devices if data.check_ids(d.device_id)]
    return devices


def _latest_value(data: FleetData, device_id: str, metric: str) -> float | None:
    series = data.series(device_id, metric)
    return series[-1].value if series else None


def run_negated(spec: QuerySpec, data: FleetData, device_type: str | None, analyses: dict[str, Analysis]) -> Finding:
    topic = spec.topics[0]
    label = TOPIC_LABELS[topic]
    population = _population(data, topic, device_type)
    if topic == "battery_eol" and not population:
        raise Unanswerable("Battery health is not reported for this tenant, so end-of-life cannot be assessed from the telemetry.")
    if not population:
        raise Unanswerable("No telemetry evidence exists for this tenant in the window.")

    hits = set(analyses[topic](data, device_type).device_ids)
    kept = [d for d in population if d.device_id not in hits]
    kind = f"negated_{topic}"
    if not kept:
        return _empty(kind, f"devices NOT {label}", data, device_type)

    citations = []
    for device in kept:
        metric = RANK_METRICS.get(topic) or ("disk_free_pct" if topic in _DISK_TOPICS else None)
        if topic in ("failing_compliance", "compliance_drift"):
            checks = [data.check_series(device.device_id, c)[-1] for c in data.check_ids(device.device_id)]
            citations.append(_check_citation(checks[-1], f"not {label}: latest checks pass"))
        elif metric and data.series(device.device_id, metric):
            citations.append(_metric_citation(data.series(device.device_id, metric)[-1], f"not {label}: within limit"))
        else:
            citations.append(_device_citation(device, f"not {label}"))

    ids = [d.device_id for d in kept]
    return Finding(
        kind=kind,
        finding=f"{len(ids)} device(s) are NOT {label}: {', '.join(ids)}",
        evidence=[f"{d.device_id}: does not meet the condition in the latest telemetry" for d in kept],
        explanation=f"These devices do not meet the '{label}' condition, so they are the complement of that answer.",
        severity="low",
        device_ids=ids,
        citations=citations,
        worst_device_id=None,
        sample_count=0,
        details={"base_kind": topic},
    )


def run_ranking(spec: QuerySpec, data: FleetData, device_type: str | None) -> Finding:
    topic = spec.topics[0]
    metric = spec.metric or RANK_METRICS[topic]
    ranked = []
    for device in data.in_scope(device_type):
        value = _latest_value(data, device.device_id, metric)
        if value is not None:
            ranked.append((value, device.device_id))
    if not ranked:
        return _empty(f"ranking_{topic}", f"devices with {metric}", data, device_type)
    ranked.sort(key=lambda item: (-item[0], item[1]) if spec.descending else (item[0], item[1]))
    top = ranked[: spec.limit]
    direction = "highest" if spec.descending else "lowest"
    ids = [device_id for _, device_id in top]
    citations = [
        _metric_citation(data.series(device_id, metric)[-1], f"ranked {direction} by latest {metric}")
        for _, device_id in top
    ]
    return Finding(
        kind=f"ranking_{topic}",
        finding=(
            f"Top {len(top)} device(s) by {direction} latest {metric}: "
            + ", ".join(f"{device_id} ({value:.1f})" for value, device_id in top)
        ),
        evidence=[f"{device_id}: {metric} = {value:.1f}" for value, device_id in top],
        explanation=f"Ranked by the latest {metric} reading in the window, {direction} first.",
        severity="low",
        device_ids=ids,
        citations=citations,
        worst_device_id=None,
        sample_count=0,
        details={"metric": metric, "descending": spec.descending},
    )


def run_conjunction(spec: QuerySpec, data: FleetData, device_type: str | None, analyses: dict[str, Analysis]) -> Finding:
    results = {topic: analyses[topic](data, device_type) for topic in spec.topics}
    common = set.intersection(*(set(result.device_ids) for result in results.values()))
    labels = " and ".join(TOPIC_LABELS[topic] for topic in spec.topics)
    if not common:
        return _empty("conjunction", f"devices that are {labels}", data, device_type)

    ids = sorted(common)
    citations: list[dict] = []
    seen: set[tuple] = set()
    for result in results.values():
        for citation in result.citations:
            if citation.get("device_id") in common:
                key = (citation.get("device_id"), citation.get("metric"), citation.get("reason"), citation.get("snapshot_id"))
                if key not in seen:
                    seen.add(key)
                    citations.append(citation)
    return Finding(
        kind="conjunction",
        finding=f"{len(ids)} device(s) are {labels}: {', '.join(ids)}",
        evidence=[f"{device_id}: matches all {len(spec.topics)} conditions" for device_id in ids],
        explanation="Only devices that satisfy every named condition are listed. Each condition's evidence is cited.",
        severity="medium",
        device_ids=ids,
        citations=citations,
        worst_device_id=None,
        sample_count=0,
        details={"topics": list(spec.topics)},
    )


def run(spec: QuerySpec, data: FleetData, device_type: str | None, analyses: dict[str, Analysis]) -> Finding:
    if spec.kind == "negated":
        return run_negated(spec, data, device_type, analyses)
    if spec.kind == "ranking":
        return run_ranking(spec, data, device_type)
    if spec.kind == "conjunction":
        return run_conjunction(spec, data, device_type, analyses)
    raise ValueError(f"not a compound query: {spec.kind}")
