"""Deterministic intent routing. Order matters: cross-tenant and specific trends are checked first."""

from __future__ import annotations

import re
from collections.abc import Iterable

INTENTS = (
    "cross_tenant",
    "compliance_drift",
    "battery_eol",
    "ram_constrained",
    "storage_trend",
    "failing_compliance",
    "os_age",
    "low_disk",
    "unsupported",
)

_GENERIC_CROSS_TENANT = re.compile(
    r"\b(all tenants|every (company|tenant|customer)|other (companies|customers|tenants|clients))\b"
)
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("compliance_drift", re.compile(r"\bdrift\b|(over time|trend).*complian|complian.*(over time|trend)")),
    ("battery_eol", re.compile(r"\bbatter(y|ies)\b|end[- ]of[- ]life|\beol\b")),
    ("ram_constrained", re.compile(r"\b(ram|memory)\b")),
    (
        "storage_trend",
        re.compile(
            r"\b(disk|storage)\b.*\b(consistent\w*|persistent\w*|over time|trend\w*|always|repeatedly)\b"
            r"|\b(consistent\w*|persistent\w*|over time|trend\w*|always|repeatedly)\b.*\b(disk|storage)\b"
        ),
    ),
    ("failing_compliance", re.compile(r"complian|\bcheck\w*|\bpolic\w*|\bfail\w*")),
    ("os_age", re.compile(r"\b(os|macos|operating system|version)\b")),
    ("low_disk", re.compile(r"\b(disk|storage|space)\b")),
)


def classify(question: str, current_tenant: str, known_tenants: Iterable[tuple[str, str]]) -> str:
    """Route a question. `known_tenants` are (tenant_id, display name) pairs.

    A mention of another tenant's ID or name is a cross-tenant request. The caller's own ID and name never count,
    even when another tenant shares the word (for example "Acme" for tenant acme-001 and an older tenant named acme).
    Retrieval is tenant-scoped either way, so this check guards intent, not data access.
    """
    text = question.lower()
    if _GENERIC_CROSS_TENANT.search(text):
        return "cross_tenant"
    tenants = list(known_tenants)
    own_terms = {term.lower() for tid, name in tenants if tid == current_tenant for term in (tid, name)}
    for tid, name in tenants:
        if tid == current_tenant:
            continue
        for term in {tid.lower(), name.lower()} - own_terms:
            if re.search(rf"\b{re.escape(term)}\b", text):
                return "cross_tenant"
    for intent, pattern in _RULES:
        if pattern.search(text):
            return intent
    return "unsupported"


def device_type_scope(question: str) -> str | None:
    text = question.lower()
    if re.search(r"\blaptops?\b", text):
        return "laptop"
    if re.search(r"\bdesktops?\b", text):
        return "desktop"
    return None
