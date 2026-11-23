"""Routing: paraphrases, misspellings, negation, rankings, multi-topic questions, and cross-tenant mentions.

Each case asserts the operation and topics. A wrong operation is a failure. Refusal is expected only where the
telemetry cannot answer the question.
"""

from __future__ import annotations

import pytest

from app.intent import classify, parse_query

TENANTS = [("acme-001", "Acme"), ("globex-002", "Globex")]
CALLER = "acme-001"
REFUSE = "unsupported"

CASES = [
    # Paraphrases of single analyses
    ("Which devices at Acme are low on disk space?", "low_disk"),
    ("which laptops are running out of storage", "low_disk"),
    ("Devices nearly full on hard drive", "low_disk"),
    ("Show me machines with little free storage", "low_disk"),
    ("Devices consistently constrained by RAM", "ram_constrained"),
    ("which machines always run out of memory", "ram_constrained"),
    ("Show me laptops failing high-severity compliance checks.", "failing_compliance"),
    ("which laptops fail security policies", "failing_compliance"),
    ("which laptops are non-compliant", "failing_compliance"),
    ("How many devices are running an OS older than macOS 15?", "os_age"),
    ("how many macs are on an old version of macos", "os_age"),
    ("Batteries approaching end-of-life", "battery_eol"),
    ("which batteries are dying soon", "battery_eol"),
    ("which battery is wearing out", "battery_eol"),
    ("Compliance drift over time", "compliance_drift"),
    ("are compliance checks getting worse over time", "compliance_drift"),
    ("Devices consistently low on storage", "storage_trend"),
    ("which devices are trending downward on storage over time", "storage_trend"),
    # Misspellings within the correction window
    ("wich devices at acme have low disk spce", "low_disk"),
    ("which devices have low dsk space", "low_disk"),
    ("devices with memroy pressure", "ram_constrained"),
    ("devices with bad batery health", "battery_eol"),
    ("Compliance drfit over time", "compliance_drift"),
    ("which laptops fial security policies", "failing_compliance"),
    ("Which laptops have very little space left on the drive?", "low_disk"),
    ("which computers are short on memory", "ram_constrained"),
    ("which laptops keep failing their security checks", "failing_compliance"),
    ("how many macs still run an old macos", "os_age"),
    ("are our compliance results drifting over time", "compliance_drift"),
    # Negation: the complement of one topic
    ("Which devices are NOT low on disk space?", "negated:low_disk"),
    ("Which devices have no failing compliance checks?", "negated:failing_compliance"),
    ("which laptops aren't running out of storage", "negated:low_disk"),
    ("devices without memory pressure", "negated:ram_constrained"),
    ("which batteries are never end-of-life", "negated:battery_eol"),
    ("which laptops don't fail security policies", "negated:failing_compliance"),
    ("none of the devices are low on disk space", "negated:low_disk"),
    # Rankings: a superlative on one metric
    ("Which devices have the most RAM?", "ranking:ram_constrained"),
    ("which laptop has the least free storage", "ranking:low_disk"),
    ("top 5 devices by memory usage", "ranking:ram_constrained"),
    ("which battery has the lowest health", "ranking:battery_eol"),
    # Multi-topic conjunctions
    ("devices low on disk and high ram", "conjunction:ram_constrained+low_disk"),
    ("laptops failing compliance on macos 14", "conjunction:failing_compliance+os_age"),
    ("batteries dying and disk space low", "conjunction:battery_eol+low_disk"),
    # Refused: the telemetry cannot support the meaning
    ("none of the devices low on disk and high ram", REFUSE),
    ("which devices don't have the most ram", REFUSE),
    ("show the worst compliance failures", REFUSE),
    ("which macbook is the oldest", REFUSE),
    ("What is the weather in Paris?", REFUSE),
    ("Write me a poem about laptops", REFUSE),
    ("Who is the CEO of Acme?", REFUSE),
    ("which devices are ok", REFUSE),
    ("which devices are fne", REFUSE),
    # Cross-tenant, including misspelled rival names
    ("Show me Globex laptop data", "cross_tenant"),
    ("disk space at Globx", "cross_tenant"),
    ("Show me laptops failing compliance at globex", "cross_tenant"),
    ("every company's devices with low disk", "cross_tenant"),
    ("list devices in all tenants", "cross_tenant"),
]


@pytest.mark.parametrize("question, expected", CASES, ids=[q[:50] for q, _ in CASES])
def test_router_gives_the_expected_operation(question, expected):
    got = parse_query(question, CALLER, TENANTS).label
    assert got == expected, f"{question!r} routed to {got!r}, expected {expected!r}"


def test_a_negated_question_is_never_answered_as_the_plain_topic():
    for question, expected in CASES:
        if "negated" in expected:
            assert not classify(question, CALLER, TENANTS) in ("low_disk", "ram_constrained", "battery_eol", "failing_compliance"), question


def test_the_caller_own_company_name_never_counts_as_cross_tenant():
    assert classify("Which devices at Acme are low on disk space?", CALLER, TENANTS) == "low_disk"
    assert classify("Which devices at Acme are low on disk space?", "globex-002", TENANTS) == "cross_tenant"
