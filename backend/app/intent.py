"""Deterministic intent routing.

A wrong answer is worse than a refusal, so the router refuses whenever it cannot be sure: negated questions,
superlatives, and questions that match more than one topic are refused with a reason.
"""

from __future__ import annotations

import difflib
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


# Phrases rewritten before tokenizing, so common paraphrases hit the existing rules.
_SYNONYMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bhard (drive|disk)s?\b"), "disk"),
    (re.compile(r"\bout of space\b"), "disk space"),
    (re.compile(r"\bnearly full\b"), "low disk"),
    (re.compile(r"\bbad health\b"), "battery health"),
    (re.compile(r"\bwearing out\b"), "end-of-life"),
)

_NEGATIONS = frozenset(
    {"not", "no", "never", "without", "none", "nothing", "isn't", "aren't", "don't", "doesn't", "didn't", "wasn't", "weren't"}
)
_SUPERLATIVES = frozenset(
    {
        "most", "least", "highest", "lowest", "fewest", "biggest", "largest", "smallest",
        "worst", "best", "top", "bottom", "oldest", "newest", "maximum", "minimum",
    }
)

# Vocabulary used to correct typos. Only words of five or more letters are corrected, so short words are never
# rewritten by accident (for example "dsk" stays as typed).
_VOCABULARY = (
    "disk", "storage", "memory", "battery", "batteries", "compliance", "compliant", "drift", "macos",
    "laptop", "laptops", "desktop", "desktops", "failing", "space", "health", "policy", "policies",
    "devices", "operating", "system", "version", "consistently", "trending", "approaching",
)
_CORRECTION_CUTOFF = 0.8


def _correct(token: str) -> str:
    if len(token) < 5 or token in _VOCABULARY:
        return token
    match = difflib.get_close_matches(token, _VOCABULARY, n=1, cutoff=_CORRECTION_CUTOFF)
    return match[0] if match else token


def _normalize(question: str) -> tuple[list[str], str]:
    """Return (tokens, text) after synonym rewrites and typo correction."""
    text = question.lower()
    for pattern, replacement in _SYNONYMS:
        text = pattern.sub(replacement, text)
    tokens = [_correct(token) for token in re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text)]
    return tokens, " ".join(tokens)


def _resolve(matches: list[str]) -> str:
    """Pick one intent, or "unsupported" when the question spans several topics.

    A specific rule suppresses the general rule it overlaps with: drift over failing compliance, and storage trend over
    low disk. Anything still matching more than one intent is ambiguous, so it is refused.
    """
    found = set(matches)
    if "compliance_drift" in found:
        found.discard("failing_compliance")
    if "storage_trend" in found:
        found.discard("low_disk")
    if len(found) == 1:
        return found.pop()
    return "unsupported"


def classify_with_reason(question: str, current_tenant: str, known_tenants: Iterable[tuple[str, str]]) -> tuple[str, str | None]:
    """Route a question. Returns (intent, refusal reason). `known_tenants` are (tenant_id, display name) pairs.

    A mention of another tenant's ID or name is a cross-tenant request. The caller's own ID and name never count,
    even when another tenant shares the word (for example "Acme" for tenant acme-001 and an older tenant named acme).
    Retrieval is tenant-scoped either way, so this check guards intent, not data access.
    """
    text = question.lower()
    if _GENERIC_CROSS_TENANT.search(text):
        return "cross_tenant", None
    tenants = list(known_tenants)
    own_terms = {term.lower() for tid, name in tenants if tid == current_tenant for term in (tid, name)}
    other_terms = {term.lower() for tid, name in tenants if tid != current_tenant for term in (tid, name)} - own_terms
    for term in other_terms:
        if re.search(rf"\b{re.escape(term)}\b", text):
            return "cross_tenant", None
    # Misspelled rival names ("Globx") are caught too. Tokens shorter than five letters are never fuzzy-matched.
    words = re.findall(r"[a-z0-9]+", text)
    for word in words:
        if len(word) >= 5 and word not in own_terms and difflib.get_close_matches(word, sorted(other_terms), n=1, cutoff=0.85):
            return "cross_tenant", None

    tokens, normalized = _normalize(question)
    if _NEGATIONS.intersection(tokens):
        return "unsupported", "Negated questions are refused. Ask for the affected devices directly, for example 'which devices are low on disk space'."
    if _SUPERLATIVES.intersection(tokens):
        return "unsupported", "Rankings and superlatives such as 'most' or 'lowest' are not supported. Ask for the devices that meet a condition."

    matches = [intent for intent, pattern in _RULES if pattern.search(normalized)]
    intent = _resolve(matches)
    if intent == "unsupported" and len(set(matches)) > 1:
        return "unsupported", "The question touches several topics at once. Ask about one topic at a time."
    return intent, None if intent != "unsupported" else None


def classify(question: str, current_tenant: str, known_tenants: Iterable[tuple[str, str]]) -> str:
    return classify_with_reason(question, current_tenant, known_tenants)[0]


def device_type_scope(question: str) -> str | None:
    text = question.lower()
    if re.search(r"\blaptops?\b", text):
        return "laptop"
    if re.search(r"\bdesktops?\b", text):
        return "desktop"
    return None
