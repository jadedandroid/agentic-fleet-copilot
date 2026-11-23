"""Deterministic intent routing.

A wrong answer is worse than a refusal, so the router refuses whenever it cannot be sure: negated questions,
superlatives, and questions that match more than one topic are refused with a reason.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass

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


def _topics(matches: list[str]) -> list[str]:
    """Matched topics after suppression: drift wins over failing compliance, storage trend over low disk."""
    found = set(matches)
    if "compliance_drift" in found:
        found.discard("failing_compliance")
    if "storage_trend" in found:
        found.discard("low_disk")
    return [intent for intent, _ in _RULES if intent in found]


_CROSS_TENANT_REFUSAL = "Cross-tenant access is refused. Queries only read the caller's own tenant."
_ASC_WORDS = frozenset({"least", "lowest", "fewest", "smallest", "minimum", "min", "bottom", "worst"})
_DESC_WORDS = frozenset({"most", "highest", "biggest", "largest", "maximum", "max", "top", "best"})
# Metric and default direction per topic. Disk ranks by free space, so the most constrained device ranks first.
RANK_METRICS: dict[str, str] = {
    "ram_now": "ram_used_pct",
    "ram_constrained": "ram_used_pct",
    "low_disk": "disk_free_pct",
    "storage_trend": "disk_free_pct",
    "battery_eol": "battery_health_pct",
}


@dataclass(frozen=True)
class QuerySpec:
    """What the question asks for. `kind` is one of: single, negated, ranking, conjunction, cross_tenant, unsupported."""

    kind: str
    topics: tuple[str, ...] = ()
    refusal: str | None = None
    descending: bool = False
    limit: int = 5
    metric: str | None = None

    @property
    def label(self) -> str:
        if self.kind in ("cross_tenant", "unsupported"):
            return self.kind
        if self.kind == "single":
            return self.topics[0]
        return f"{self.kind}:{'+'.join(self.topics)}"


def _refuse(kind: str, reason: str) -> QuerySpec:
    return QuerySpec(kind="unsupported" if kind != "cross_tenant" else kind, refusal=reason)


# Words that make a RAM question about a sustained pattern. Without them, RAM means the current reading.
_SUSTAINED_WORDS = re.compile(r"\bconsisten\w*|\bpersisten\w*|\balways\b|over time|\btrend\w*|\brepeatedly\b")


def parse_query(question: str, current_tenant: str, known_tenants: Iterable[tuple[str, str]]) -> QuerySpec:
    """Interpret a question as one of the supported operations, or refuse with a reason.

    - single: one topic, as asked.
    - negated: one topic, inverted. "not low on disk" lists devices that are not low on disk.
    - ranking: a superlative on one topic's metric. "most RAM" lists the top devices by RAM use.
    - conjunction: several topics joined with "and". Lists devices that match all of them.
    Negation combined with a ranking or with several topics is refused, because the meaning is ambiguous.
    """
    if mentions_other_tenant(question, current_tenant, known_tenants):
        return QuerySpec(kind="cross_tenant", refusal=_CROSS_TENANT_REFUSAL)

    tokens, normalized = _normalize(question)
    negated = bool(_NEGATIONS.intersection(tokens))
    superlative = bool(_SUPERLATIVES.intersection(tokens))
    topics = _topics([intent for intent, pattern in _RULES if pattern.search(normalized)])
    if "ram_constrained" in topics and not _SUSTAINED_WORDS.search(normalized):
        topics = ["ram_now" if topic == "ram_constrained" else topic for topic in topics]

    if not topics:
        return QuerySpec(kind="unsupported", refusal=OUT_OF_SCOPE_REFUSAL)
    if negated and superlative:
        return _refuse("unsupported", "A question cannot both negate a condition and rank devices. Ask one of those.")
    if negated and len(topics) > 1:
        return _refuse("unsupported", "A negated question must name one topic. Ask about one condition at a time.")
    if negated:
        return QuerySpec(kind="negated", topics=(topics[0],))
    if superlative:
        if len(topics) > 1:
            return _refuse("unsupported", "Ranking applies to one topic at a time. Name the metric you want ranked.")
        topic = topics[0]
        if topic not in RANK_METRICS:
            return _refuse("unsupported", f"Devices cannot be ranked by {topic.replace('_', ' ')}.")
        words = set(tokens)
        if topic == "low_disk" or topic == "storage_trend":
            # "most free space" ranks by free space; any other disk superlative means the most constrained devices.
            descending = bool(words & _DESC_WORDS) and "free" in words
        elif topic == "battery_eol":
            descending = bool(words & _DESC_WORDS)
        else:
            descending = not bool(words & _ASC_WORDS)
        limit_match = re.search(r"\btop (\d{1,2})\b", normalized)
        limit = int(limit_match.group(1)) if limit_match else 5
        return QuerySpec(
            kind="ranking",
            topics=(topic,),
            descending=descending,
            limit=max(1, min(limit, 20)),
            metric=RANK_METRICS[topic],
        )
    if len(topics) > 1:
        return QuerySpec(kind="conjunction", topics=tuple(topics))
    return QuerySpec(kind="single", topics=(topics[0],))


OUT_OF_SCOPE_REFUSAL = (
    "The question is outside the supported telemetry analyses: low disk, storage trends, RAM pressure, "
    "failing compliance, compliance drift, OS age, and battery end-of-life."
)


def mentions_other_tenant(question: str, current_tenant: str, known_tenants: Iterable[tuple]) -> bool:
    """True when the question names data that belongs to another tenant, including a near-miss of its name.

    Each entry is (owner_tenant, term_id, display_name). A two-item (tenant_id, name) pair means the tenant itself.
    Companies uploaded into the caller's tenant are the caller's own, so questions about them are answered. The
    caller's terms never count as cross-tenant, even when another tenant shares the word (for example "Acme" for
    tenant acme-001 and an older tenant named acme). Retrieval is tenant-scoped either way; this guards intent.
    """
    text = question.lower()
    if _GENERIC_CROSS_TENANT.search(text):
        return True
    entries = [(item[0], item[0], item[1]) if len(item) == 2 else tuple(item) for item in known_tenants]
    own_terms = {term.lower() for owner, *names in entries if owner == current_tenant for term in names}
    other_terms = {term.lower() for owner, *names in entries if owner != current_tenant for term in names} - own_terms
    for term in other_terms:
        if re.search(rf"\b{re.escape(term)}\b", text):
            return True
    # Misspelled rival names ("Globx") are caught too. Tokens shorter than five letters are never fuzzy-matched.
    for word in re.findall(r"[a-z0-9]+", text):
        if len(word) >= 5 and word not in own_terms and difflib.get_close_matches(word, sorted(other_terms), n=1, cutoff=0.85):
            return True
    return False


def classify(question: str, current_tenant: str, known_tenants: Iterable[tuple[str, str]]) -> str:
    """Intent label for a question: a topic, cross_tenant, or unsupported."""
    spec = parse_query(question, current_tenant, known_tenants)
    if spec.kind == "single":
        return spec.topics[0]
    return spec.label if spec.kind in ("cross_tenant", "unsupported") else spec.kind


def device_type_scope(question: str) -> str | None:
    text = question.lower()
    if re.search(r"\blaptops?\b", text):
        return "laptop"
    if re.search(r"\bdesktops?\b", text):
        return "desktop"
    return None
