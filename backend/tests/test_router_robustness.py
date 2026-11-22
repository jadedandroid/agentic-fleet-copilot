"""Routing robustness: paraphrases, misspellings, negations, superlatives, and multi-topic questions.

The rule is that a wrong intent is a failure. An explicit refusal is acceptable where the router cannot be sure.
"""

from __future__ import annotations

import pytest

from app.intent import classify

TENANTS = [("acme-001", "Acme"), ("globex-002", "Globex")]
CALLER = "acme-001"

REFUSE = "unsupported"

CASES = [
    # Paraphrases of the supported analyses
    ("Which devices at Acme are low on disk space?", "low_disk"),
    ("which laptops are running out of storage", "low_disk"),
    ("Devices nearly full on hard drive", "low_disk"),
    ("Show me machines with little free storage", "low_disk"),
    ("Devices consistently constrained by RAM", "ram_constrained"),
    ("which machines always run out of memory", "ram_constrained"),
    ("Which devices have high ram usage?", "ram_constrained"),
    ("Show me laptops failing high-severity compliance checks.", "failing_compliance"),
    ("which laptops fail security policies", "failing_compliance"),
    ("which laptops are non-compliant", "failing_compliance"),
    ("How many devices are running an OS older than macOS 15?", "os_age"),
    ("how many macs are on an old version of macos", "os_age"),
    ("which devices run macos 14 or below", "os_age"),
    ("how many macbooks use an outdated operating system", "os_age"),
    ("Batteries approaching end-of-life", "battery_eol"),
    ("which batteries are dying soon", "battery_eol"),
    ("which battery is wearing out", "battery_eol"),
    ("Compliance drift over time", "compliance_drift"),
    ("are compliance checks getting worse over time", "compliance_drift"),
    ("Devices consistently low on storage", "storage_trend"),
    ("which devices are trending downward on storage over time", "storage_trend"),
    # Misspellings within the one-edit correction window
    ("wich devices at acme have low disk spce", "low_disk"),
    ("which devices have low dsk space", "low_disk"),
    ("devices with memroy pressure", "ram_constrained"),
    ("devices with bad batery health", "battery_eol"),
    ("which batery is approaching end-of-life", "battery_eol"),
    ("how many macos devices are on an ancient versoin", "os_age"),
    ("Compliance drfit over time", "compliance_drift"),
    ("which laptops fial security policies", "failing_compliance"),
    # Negations: refused, never answered with the opposite
    ("Which devices are NOT low on disk space?", REFUSE),
    ("Which devices have no failing compliance checks?", REFUSE),
    ("which laptops aren't running out of storage", REFUSE),
    ("devices without memory pressure", REFUSE),
    ("which batteries are never end-of-life", REFUSE),
    ("which laptops don't fail security policies", REFUSE),
    ("none of the devices are low on disk space", REFUSE),
    # Superlatives and rankings: refused rather than matched on the topic word
    ("Which devices have the most RAM?", REFUSE),
    ("which laptop has the least free storage", REFUSE),
    ("top 5 devices by memory usage", REFUSE),
    ("show the worst compliance failures", REFUSE),
    ("which macbook is the oldest", REFUSE),
    ("which battery has the lowest health", REFUSE),
    # Multi-topic questions: refused, because the answer would cover more than one analysis
    ("devices low on disk and high ram", REFUSE),
    ("laptops failing compliance on macos 14", REFUSE),
    ("batteries dying and disk space low", REFUSE),
    # Out of scope: refused
    ("What is the weather in Paris?", REFUSE),
    ("Write me a poem about laptops", REFUSE),
    ("Who is the CEO of Acme?", REFUSE),
    # Cross-tenant, including misspelled rival names
    ("Show me Globex laptop data", "cross_tenant"),
    ("disk space at Globx", "cross_tenant"),
    ("Show me laptops failing compliance at globex", "cross_tenant"),
    ("every company's devices with low disk", "cross_tenant"),
    ("list devices in all tenants", "cross_tenant"),
    # Typos near short words must not be guessed: refuse rather than force a match
    ("which devices are ok", REFUSE),
    ("which devices are fne", REFUSE),
    # More paraphrases
    ("Which laptops have very little space left on the drive?", "low_disk"),
    ("which computers are short on memory", "ram_constrained"),
    ("which laptops keep failing their security checks", "failing_compliance"),
    ("how many macs still run an old macos", "os_age"),
    ("which battery health is getting bad", "battery_eol"),
    ("are our compliance results drifting over time", "compliance_drift"),
]


@pytest.mark.parametrize("question, expected", CASES, ids=[q[:50] for q, _ in CASES])
def test_router_gives_the_expected_intent_or_refuses(question, expected):
    got = classify(question, CALLER, TENANTS)
    assert got == expected, f"{question!r} routed to {got!r}, expected {expected!r}"


def test_no_negated_question_is_ever_answered():
    negated = [q for q, expected in CASES if expected == REFUSE and "not" in q.lower() or "n't" in q.lower()]
    for question in negated:
        assert classify(question, CALLER, TENANTS) == REFUSE, question


def test_the_caller_own_company_name_never_counts_as_cross_tenant():
    assert classify("Which devices at Acme are low on disk space?", CALLER, TENANTS) == "low_disk"
    assert classify("Which devices at Acme are low on disk space?", "globex-002", TENANTS) == "cross_tenant"
