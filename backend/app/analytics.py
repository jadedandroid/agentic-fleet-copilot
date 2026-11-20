"""Deterministic analytics over fleet telemetry.

Every function is pure: it reads a FleetData index and returns a Finding with the
citations that support it. No LLM is involved, so results are reproducible.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from statistics import mean
from typing import Any

from .store import ComplianceRecord, DeviceRecord, MetricRecord

DISK_THRESHOLD_PCT = 15.0
RAM_THRESHOLD_PCT = 90.0
SUSTAINED_SHARE = 0.75
MIN_TREND_SAMPLES = 3
BATTERY_EOL_PCT = 80.0
BATTERY_HORIZON_DAYS = 180
COMPLIANCE_DRIFT_DELTA = 0.2
MIN_DRIFT_SAMPLES = 4
MACOS_MIN_MAJOR = 15
MAX_EMPTY_CITATIONS = 5


@dataclass(frozen=True)
class Finding:
    kind: str
    finding: str
    evidence: list[str]
    explanation: str
    severity: str
    device_ids: list[str]
    citations: list[dict[str, Any]]
    worst_device_id: str | None
    sample_count: int
    details: dict[str, Any] = field(default_factory=dict)


class FleetData:
    """Index over one tenant's device inventory, metric series, and compliance series."""

    def __init__(
        self,
        devices: Iterable[DeviceRecord],
        metrics: Iterable[MetricRecord],
        compliance: Iterable[ComplianceRecord],
    ) -> None:
        self.devices: dict[str, DeviceRecord] = {d.device_id: d for d in devices}
        self._series: dict[tuple[str, str], list[MetricRecord]] = defaultdict(list)
        for record in metrics:
            self._series[(record.device_id, record.metric)].append(record)
        self._checks: dict[tuple[str, str], list[ComplianceRecord]] = defaultdict(list)
        for record in compliance:
            self._checks[(record.device_id, record.check_id)].append(record)
        for bucket in (*self._series.values(), *self._checks.values()):
            bucket.sort(key=lambda r: r.observed_at)

    def in_scope(self, device_type: str | None) -> list[DeviceRecord]:
        return [
            device
            for device_id, device in sorted(self.devices.items())
            if device_type is None or device.device_type == device_type
        ]

    def series(self, device_id: str, metric: str) -> list[MetricRecord]:
        return self._series.get((device_id, metric), [])

    def check_ids(self, device_id: str) -> list[str]:
        return sorted({check for (device, check) in self._checks if device == device_id})

    def check_series(self, device_id: str, check_id: str) -> list[ComplianceRecord]:
        return self._checks.get((device_id, check_id), [])

    def sample_count(self, device_id: str) -> int:
        return len(self.series(device_id, "disk_free_pct"))


def _metric_citation(record: MetricRecord, reason: str) -> dict[str, Any]:
    return {
        "snapshot_id": record.snapshot_id,
        "observed_at": record.observed_at.isoformat(),
        "reason": reason,
        "device_id": record.device_id,
        "metric": record.metric,
        "value": record.value,
    }


def _check_citation(record: ComplianceRecord, reason: str) -> dict[str, Any]:
    return {
        "snapshot_id": record.snapshot_id,
        "observed_at": record.observed_at.isoformat(),
        "reason": reason,
        "device_id": record.device_id,
        "metric": record.check_id,
        "value": None,
    }


def _device_citation(device: DeviceRecord, reason: str) -> dict[str, Any]:
    return {
        "snapshot_id": device.snapshot_id,
        "observed_at": device.updated_at.isoformat(),
        "reason": reason,
        "device_id": device.device_id,
        "metric": None,
        "value": None,
    }


def _empty(kind: str, label: str, data: FleetData, device_type: str | None) -> Finding:
    """No device matched. The finding still cites the scope it checked, so the 'no' answer is grounded."""
    scope = data.in_scope(device_type)
    citations = []
    for device in scope[:MAX_EMPTY_CITATIONS]:
        latest = data.series(device.device_id, "disk_free_pct")
        if latest:
            citations.append(_metric_citation(latest[-1], "scope checked"))
        else:
            citations.append(_device_citation(device, "scope checked"))
    return Finding(
        kind=kind,
        finding=f"No {label} across {len(scope)} in-scope device(s).",
        evidence=[],
        explanation="No device met the threshold in the telemetry window, so no action is proposed.",
        severity="low",
        device_ids=[],
        citations=citations,
        worst_device_id=None,
        sample_count=0,
    )


def _sustained_threshold(
    data: FleetData,
    device_type: str | None,
    *,
    kind: str,
    metric: str,
    label: str,
    predicate: Callable[[float], bool],
    worst_is_highest: bool,
) -> Finding:
    hits: list[tuple[float, DeviceRecord, list[MetricRecord]]] = []
    for device in data.in_scope(device_type):
        series = data.series(device.device_id, metric)
        if len(series) < MIN_TREND_SAMPLES:
            continue
        over = sum(1 for record in series if predicate(record.value))
        if over / len(series) >= SUSTAINED_SHARE:
            hits.append((mean(r.value for r in series), device, series))
    if not hits:
        return _empty(kind, f"device {label}", data, device_type)

    hits.sort(key=lambda h: (-h[0] if worst_is_highest else h[0], h[1].device_id))
    evidence: list[str] = []
    citations: list[dict[str, Any]] = []
    device_ids: list[str] = []
    for average, device, series in hits:
        over = sum(1 for record in series if predicate(record.value))
        latest = series[-1]
        device_ids.append(device.device_id)
        evidence.append(
            f"{device.device_id}: {label} in {over}/{len(series)} snapshots "
            f"(latest {latest.value:.1f}%, average {average:.1f}%)"
        )
        citations.append(_metric_citation(latest, f"{label} in sustained window"))
    worst_device = hits[0][1]
    worst_series = hits[0][2]
    over_ratio = sum(1 for r in worst_series if predicate(r.value)) / len(worst_series)
    return Finding(
        kind=kind,
        finding=f"{len(hits)} device(s) consistently {label}: {', '.join(device_ids)}.",
        evidence=evidence,
        explanation=(
            f"{metric} crossed its threshold in at least {SUSTAINED_SHARE:.0%} of snapshots "
            "over the window, so the condition is persistent rather than a one-off spike."
        ),
        severity="high" if over_ratio >= 0.9 else "medium",
        device_ids=device_ids,
        citations=citations,
        worst_device_id=worst_device.device_id,
        sample_count=data.sample_count(worst_device.device_id),
        details={"metric": metric, "average_pct": round(hits[0][0], 2)},
    )


def low_disk(data: FleetData, device_type: str | None = None) -> Finding:
    hits: list[tuple[MetricRecord, DeviceRecord]] = []
    for device in data.in_scope(device_type):
        series = data.series(device.device_id, "disk_free_pct")
        if series and series[-1].value < DISK_THRESHOLD_PCT:
            hits.append((series[-1], device))
    if not hits:
        return _empty("low_disk", f"devices below {DISK_THRESHOLD_PCT:.0f}% free disk", data, device_type)

    hits.sort(key=lambda h: (h[0].value, h[1].device_id))
    worst_latest, worst_device = hits[0]
    return Finding(
        kind="low_disk",
        finding=(
            f"{len(hits)} device(s) below {DISK_THRESHOLD_PCT:.0f}% free disk: "
            + ", ".join(f"{d.device_id} ({r.value:.1f}%)" for r, d in hits)
        ),
        evidence=[f"{d.device_id}: disk_free_pct={r.value:.1f}% (latest snapshot)" for r, d in hits],
        explanation=(
            "Latest disk_free_pct reading is under the 15% threshold. "
            "Devices under 10% free are rated high severity."
        ),
        severity="high" if worst_latest.value < 10 else "medium",
        device_ids=[d.device_id for _, d in hits],
        citations=[_metric_citation(r, "latest disk reading below threshold") for r, _ in hits],
        worst_device_id=worst_device.device_id,
        sample_count=data.sample_count(worst_device.device_id),
        details={"lowest_free_pct": worst_latest.value},
    )


def storage_constrained(data: FleetData, device_type: str | None = None) -> Finding:
    return _sustained_threshold(
        data,
        device_type,
        kind="storage_trend",
        metric="disk_free_pct",
        label=f"low on storage (<{DISK_THRESHOLD_PCT:.0f}% free)",
        predicate=lambda v: v < DISK_THRESHOLD_PCT,
        worst_is_highest=False,
    )


def ram_constrained(data: FleetData, device_type: str | None = None) -> Finding:
    return _sustained_threshold(
        data,
        device_type,
        kind="ram_constrained",
        metric="ram_used_pct",
        label=f"RAM-constrained (>={RAM_THRESHOLD_PCT:.0f}% used)",
        predicate=lambda v: v >= RAM_THRESHOLD_PCT,
        worst_is_highest=True,
    )


def failing_compliance(data: FleetData, device_type: str | None = None, severity: str = "high") -> Finding:
    hits: dict[str, list[tuple[str, ComplianceRecord]]] = defaultdict(list)
    for device in data.in_scope(device_type):
        for check_id in data.check_ids(device.device_id):
            latest = data.check_series(device.device_id, check_id)[-1]
            if latest.severity == severity and not latest.passed:
                hits[device.device_id].append((check_id, latest))
    if not hits:
        return _empty("failing_compliance", f"devices failing {severity}-severity checks", data, device_type)

    ordered = sorted(hits.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    worst_device_id, worst_checks = ordered[0]
    evidence: list[str] = []
    citations: list[dict[str, Any]] = []
    for device_id, checks in ordered:
        for check_id, record in checks:
            evidence.append(f"{device_id}: {check_id} failed ({severity} severity, latest snapshot)")
            citations.append(_check_citation(record, f"failed {severity}-severity check"))
    return Finding(
        kind="failing_compliance",
        finding=(
            f"{len(ordered)} device(s) fail {severity}-severity compliance checks: "
            + ", ".join(f"{device_id} ({', '.join(c for c, _ in checks)})" for device_id, checks in ordered)
        ),
        evidence=evidence,
        explanation=(
            f"The latest result for each check is a {severity}-severity failure. "
            "Checks are read per device across the window."
        ),
        severity=severity,
        device_ids=[device_id for device_id, _ in ordered],
        citations=citations,
        worst_device_id=worst_device_id,
        sample_count=data.sample_count(worst_device_id),
        details={"check_id": worst_checks[0][0], "failing_checks": len(worst_checks)},
    )


def _major_version(version: str) -> int | None:
    head = version.split(".")[0].strip()
    return int(head) if head.isdigit() else None


def os_age(data: FleetData, device_type: str | None = None, min_major: int = MACOS_MIN_MAJOR) -> Finding:
    macos_devices = [d for d in data.in_scope(device_type) if d.os_name.lower() == "macos"]
    hits: list[tuple[tuple[int, ...], DeviceRecord]] = []
    for device in macos_devices:
        major = _major_version(device.os_version)
        if major is not None and major < min_major:
            parts = tuple(int(p) for p in device.os_version.split(".") if p.isdigit())
            hits.append((parts, device))
    if not hits:
        return _empty("os_age", f"macOS devices older than macOS {min_major}", data, device_type)

    hits.sort(key=lambda h: (h[0], h[1].device_id))
    worst = hits[0][1]
    return Finding(
        kind="os_age",
        finding=(
            f"{len(hits)} of {len(macos_devices)} macOS device(s) run an OS older than macOS {min_major}: "
            + ", ".join(f"{d.device_id} (macOS {d.os_version})" for _, d in hits)
        ),
        evidence=[f"{d.device_id}: macOS {d.os_version} (employee {d.employee_id})" for _, d in hits],
        explanation=f"macOS major versions below {min_major} no longer receive the current security patch cadence.",
        severity="medium",
        device_ids=[d.device_id for _, d in hits],
        citations=[_device_citation(d, f"macOS older than {min_major}") for _, d in hits],
        worst_device_id=worst.device_id,
        sample_count=data.sample_count(worst.device_id),
        details={"employee_id": worst.employee_id, "oldest_os_version": worst.os_version},
    )


def battery_eol(
    data: FleetData,
    device_type: str | None = None,
    eol: float = BATTERY_EOL_PCT,
    horizon_days: int = BATTERY_HORIZON_DAYS,
) -> Finding:
    hits: list[tuple[float, float, DeviceRecord, list[MetricRecord]]] = []
    for device in data.in_scope(device_type):
        series = data.series(device.device_id, "battery_health_pct")
        if len(series) < MIN_TREND_SAMPLES:
            continue
        slope = _slope_per_day(series)
        if slope is None or slope >= 0:
            continue
        days_to_eol = max(0.0, (series[-1].value - eol) / -slope)
        if days_to_eol <= horizon_days:
            hits.append((days_to_eol, slope, device, series))
    if not hits:
        return _empty("battery_eol", f"batteries projected to reach {eol:.0f}% within {horizon_days} days", data, device_type)

    hits.sort(key=lambda h: (h[0], h[2].device_id))
    days_to_eol, slope, worst, _ = hits[0]
    evidence = []
    citations = []
    for days, device_slope, device, dev_series in hits:
        first, last = dev_series[0], dev_series[-1]
        span = (last.observed_at - first.observed_at).total_seconds() / 86400
        evidence.append(
            f"{device.device_id}: battery health {first.value:.1f}% -> {last.value:.1f}% over {span:.0f} days "
            f"({device_slope:+.2f}%/day, {len(dev_series)} samples); projected to reach {eol:.0f}% in {days:.0f} days"
        )
        citations.append(_metric_citation(first, "battery health start of trend"))
        citations.append(_metric_citation(last, "battery health latest reading"))
    return Finding(
        kind="battery_eol",
        finding=(
            f"{len(hits)} device(s) projected to reach {eol:.0f}% battery health within {horizon_days} days: "
            + ", ".join(f"{d.device_id} (~{days:.0f} days)" for days, _, d, _ in hits)
        ),
        evidence=evidence,
        explanation="A least-squares fit of battery_health_pct over time gives a declining slope, extrapolated to the end-of-life threshold.",
        severity="high" if days_to_eol <= 90 else "medium",
        device_ids=[d.device_id for _, _, d, _ in hits],
        citations=citations,
        worst_device_id=worst.device_id,
        sample_count=data.sample_count(worst.device_id),
        details={
            "projected_days": round(days_to_eol, 1),
            "slope_per_day": round(slope, 3),
            "eol_threshold_pct": eol,
        },
    )


def _slope_per_day(series: list[MetricRecord]) -> float | None:
    origin = series[0].observed_at
    xs = [(r.observed_at - origin).total_seconds() / 86400 for r in series]
    ys = [r.value for r in series]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denom


def compliance_drift(
    data: FleetData,
    device_type: str | None = None,
    delta: float = COMPLIANCE_DRIFT_DELTA,
    min_samples: int = MIN_DRIFT_SAMPLES,
) -> Finding:
    hits: list[tuple[float, float, float, str, str, list[ComplianceRecord]]] = []
    for device in data.in_scope(device_type):
        for check_id in data.check_ids(device.device_id):
            series = data.check_series(device.device_id, check_id)
            if len(series) < min_samples:
                continue
            half = len(series) // 2
            old_rate = sum(1 for r in series[:half] if not r.passed) / half
            new_rate = sum(1 for r in series[half:] if not r.passed) / (len(series) - half)
            if new_rate - old_rate >= delta:
                hits.append((new_rate - old_rate, old_rate, new_rate, device.device_id, check_id, series))
    if not hits:
        return _empty("compliance_drift", "device checks showing compliance drift", data, device_type)

    hits.sort(key=lambda h: (-h[0], h[3], h[4]))
    _, old_rate, new_rate, worst_device_id, worst_check, _ = hits[0]
    evidence = []
    citations = []
    for _, old, new, device_id, check_id, series in hits:
        evidence.append(
            f"{device_id}: {check_id} failure rate {old:.0%} in older half -> {new:.0%} in newer half "
            f"({len(series)} snapshots)"
        )
        citations.append(_check_citation(series[0], "drift: oldest result"))
        citations.append(_check_citation(series[-1], "drift: latest result"))
    device_ids = sorted({h[3] for h in hits})
    return Finding(
        kind="compliance_drift",
        finding=(
            f"{len(hits)} device check(s) show compliance drift: "
            + ", ".join(f"{h[3]} {h[4]} ({h[1]:.0%} -> {h[2]:.0%} failing)" for h in hits)
        ),
        evidence=evidence,
        explanation="Failure rate in the newer half of the window exceeds the older half by at least 20 points.",
        severity="high" if new_rate >= 0.5 else "medium",
        device_ids=device_ids,
        citations=citations,
        worst_device_id=worst_device_id,
        sample_count=data.sample_count(worst_device_id),
        details={"check_id": worst_check, "old_failure_rate": round(old_rate, 3), "new_failure_rate": round(new_rate, 3)},
    )
