"""Trust classification — decides status, confidence, and reason for each document.

A document is **current** only when there is positive evidence of trust.
Absence of negative evidence is *not* positive trust evidence.

Evidence categories (conceptual):
  - **authoritative_evidence**: signals that the document is trustworthy
    (Status: Current, Canonical, high incoming refs, latest version).
  - **supersession_evidence**: proof the document is obsolete
    (Status: Legacy, "replaced by …", older version with newer sibling).
  - **maintenance_risks**: issues suggesting the content may be unreliable
    (unresolved refs, broken links, overdue review, old last-reviewed).
  - **scan_context**: observations about scan-level relationships
    (incoming ref count, resolved outgoing refs, sibling versions).
  - **missing_evidence**: absent trust markers
    (no status field, no owner, no last-reviewed date).

Classification priority:
1. Explicit stale/supersession evidence → stale
2. Hard maintenance risks → needs_review
3. Positive authoritative evidence → current
   - Soft risks lower confidence but do not override Status: Current.
   - Contradictory evidence (stale status keyword + authoritative signals)
     forces needs_review.
4. Otherwise → unknown (soft risk alone → needs_review)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from kb_audit.models import AuditPriority, Document, Lifecycle, Severity, StalenessSignal, Status
from kb_audit.titles import normalize_title  # noqa: F401 — re-exported

# ---------------------------------------------------------------------------
# Content parsing helpers
# ---------------------------------------------------------------------------

_STATUS_FIELD_RE = re.compile(
    r"(?:^|\n)\s*Status\s*[:]\s*(.+?)(?:\n|$)",
    re.IGNORECASE,
)

_OWNER_FIELD_RE = re.compile(
    r"(?:^|\n)\s*(?:Owner|Maintained\s+by|DRI)\s*[:]\s*(\S.+?)(?:\n|$)",
    re.IGNORECASE,
)

_LAST_REVIEWED_RE = re.compile(
    r"(?:^|\n)\s*Last\s+reviewed\s*[:]\s*(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)

# Structured body metadata fields for supersession/authority
_REPLACED_BY_RE = re.compile(
    r"(?:^|\n)\s*(?:Replaced\s+by|Superseded\s+by)\s*[:]\s*(\S.+?)(?:\n|$)",
    re.IGNORECASE,
)
_DEPRECATED_AS_OF_RE = re.compile(
    r"(?:^|\n)\s*Deprecated\s+as\s+of\s*[:]\s*(\S.+?)(?:\n|$)",
    re.IGNORECASE,
)
_CANONICAL_RE = re.compile(
    r"(?:^|\n)\s*Canonical\s*[:]\s*(true|yes)\s*(?:\n|$)",
    re.IGNORECASE,
)
_REVIEW_CADENCE_RE = re.compile(
    r"(?:^|\n)\s*Review\s+cadence\s*[:]\s*(\S.+?)(?:\n|$)",
    re.IGNORECASE,
)
_APPLIES_TO_RE = re.compile(
    r"(?:^|\n)\s*Applies\s+to\s*[:]\s*(\S.+?)(?:\n|$)",
    re.IGNORECASE,
)

# Structured applicability/scope fields — used for scope-aware supersession.
# Each field captures one dimension of a document's intended applicability.
_SCOPE_PRODUCT_RE = re.compile(
    r"(?:^|\n)\s*Product\s*[:]\s*(\S.+?)(?:\n|$)", re.IGNORECASE
)
_SCOPE_VERSION_RE = re.compile(
    r"(?:^|\n)\s*Version\s*[:]\s*(\S.+?)(?:\n|$)", re.IGNORECASE
)
_SCOPE_AUDIENCE_RE = re.compile(
    r"(?:^|\n)\s*Audience\s*[:]\s*(\S.+?)(?:\n|$)", re.IGNORECASE
)
_SCOPE_ENVIRONMENT_RE = re.compile(
    r"(?:^|\n)\s*Environment\s*[:]\s*(\S.+?)(?:\n|$)", re.IGNORECASE
)
_SCOPE_REGION_RE = re.compile(
    r"(?:^|\n)\s*Region\s*[:]\s*(\S.+?)(?:\n|$)", re.IGNORECASE
)
_SCOPE_PLAN_RE = re.compile(
    r"(?:^|\n)\s*Plan\s*[:]\s*(\S.+?)(?:\n|$)", re.IGNORECASE
)
_SCOPE_FEATURE_STATE_RE = re.compile(
    r"(?:^|\n)\s*Feature\s+state\s*[:]\s*(\S.+?)(?:\n|$)", re.IGNORECASE
)
_SCOPE_FEATURE_FLAG_RE = re.compile(
    r"(?:^|\n)\s*Feature\s+flag\s*[:]\s*(\S.+?)(?:\n|$)", re.IGNORECASE
)
_SCOPE_COMPACT_RE = re.compile(
    r"(?:^|\n)\s*Scope\s*[:]\s*(\S.+?)(?:\n|$)", re.IGNORECASE
)

# Ordered list for compact iteration during parsing
_SCOPE_FIELD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("product", _SCOPE_PRODUCT_RE),
    ("version", _SCOPE_VERSION_RE),
    ("audience", _SCOPE_AUDIENCE_RE),
    ("environment", _SCOPE_ENVIRONMENT_RE),
    ("region", _SCOPE_REGION_RE),
    ("plan", _SCOPE_PLAN_RE),
    ("feature_state", _SCOPE_FEATURE_STATE_RE),
    ("feature_flag", _SCOPE_FEATURE_FLAG_RE),
]

# Cadence text → maximum days between reviews
_CADENCE_DAYS: dict[str, int] = {
    "weekly": 14,
    "monthly": 45,
    "quarterly": 120,
    "semi-annually": 210,
    "annually": 395,
    "yearly": 395,
}

# Status text → classification
_STALE_STATUS_KEYWORDS = {
    "legacy", "deprecated", "retired", "obsolete", "archived",
    "superseded", "end-of-life", "eol", "sunset",
}
_STRONG_TRUST_KEYWORDS = {"current"}
_SUPPORTING_TRUST_KEYWORDS = {"supported", "active", "approved", "live"}

# Body-text phrases that indicate the *page itself* is obsolete.
# Each entry is (compiled regex, human-readable evidence label).
_SUPERSESSION_PHRASES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:^|\n)\s*(?:this\s+(?:document|page|guide|article)\s+(?:is|has been)\s+)?superseded\s+by\b", re.IGNORECASE),
     "Body text says this document is superseded"),
    (re.compile(r"(?:^|\n)\s*(?:this\s+(?:document|page|guide|article)\s+(?:is|has been)\s+)?replaced\s+by\b", re.IGNORECASE),
     "Body text says this document has been replaced"),
    (re.compile(r"(?:^|\n)\s*(?:please\s+)?use\s+\S.{2,60}?\s+instead\b", re.IGNORECASE),
     "Body text says to use another document instead"),
    (re.compile(r"(?:^|\n)\s*(?:this\s+(?:document|page|guide|article)\s+is\s+)?no\s+longer\s+maintained\b", re.IGNORECASE),
     "Body text says this document is no longer maintained"),
    (re.compile(r"(?:^|\n)\s*deprecated\s+as\s+of\b", re.IGNORECASE),
     "Body text says this document is deprecated"),
    (re.compile(r"(?:^|\n)\s*archived\s+as\s+of\b", re.IGNORECASE),
     "Body text says this document is archived"),
    (re.compile(r"(?:^|\n)\s*do\s+not\s+use\b", re.IGNORECASE),
     "Body text says do not use this document"),
    (re.compile(r"(?:^|\n)\s*(?:this\s+(?:document|page|guide|article)\s+is\s+)?no\s+longer\s+authoritative\b", re.IGNORECASE),
     "Body text says this document is no longer authoritative"),
]

# Title normalization is in kb_audit.titles; normalize_title is re-exported above.

# ---------------------------------------------------------------------------
# Lifecycle detection patterns (only patterns NOT already covered above)
# ---------------------------------------------------------------------------

# Status keywords → lifecycle mapping (ordered by priority)
_LIFECYCLE_STATUS_MAP: list[tuple[set[str], Lifecycle]] = [
    ({"deprecated", "retired", "obsolete", "end-of-life", "eol", "sunset"}, "deprecated"),
    ({"superseded"}, "superseded"),
    ({"archived"}, "archived"),
    ({"legacy"}, "deprecated"),
    ({"current"}, "current"),
    ({"supported", "active", "approved", "live"}, "supported"),
    ({"draft", "wip", "work-in-progress", "work in progress"}, "draft"),
    ({"experimental", "beta", "preview", "alpha", "labs"}, "experimental"),
]

# Title clues for lifecycle (regex, lifecycle, evidence label)
_LIFECYCLE_TITLE_PATTERNS: list[tuple[re.Pattern[str], Lifecycle, str]] = [
    (re.compile(r"\(\s*deprecated\s*\)", re.IGNORECASE), "deprecated", "Title contains '(deprecated)'"),
    (re.compile(r"\(\s*legacy\s*\)", re.IGNORECASE), "deprecated", "Title contains '(legacy)'"),
    (re.compile(r"\(\s*old\s*\)", re.IGNORECASE), "deprecated", "Title contains '(old)'"),
    (re.compile(r"\bdraft\b", re.IGNORECASE), "draft", "Title contains 'draft'"),
    (re.compile(r"\b(?:beta|preview)\b", re.IGNORECASE), "experimental", "Title contains beta/preview"),
    (re.compile(r"\bexperimental\b", re.IGNORECASE), "experimental", "Title contains 'experimental'"),
]

# Body phrases for lifecycle states not already covered by _SUPERSESSION_PHRASES
_LIFECYCLE_BODY_PATTERNS: list[tuple[re.Pattern[str], Lifecycle, str]] = [
    (re.compile(r"\bsubject\s+to\s+change\b", re.IGNORECASE),
     "experimental", "Body text says content is subject to change"),
    (re.compile(r"\bnot\s+for\s+production\s+use\b", re.IGNORECASE),
     "experimental", "Body text says not for production use"),
    (re.compile(r"\b(?:beta|preview)\s+(?:feature|documentation|release)\b", re.IGNORECASE),
     "experimental", "Body text indicates beta/preview content"),
    (re.compile(r"\bwork[- ]in[- ]progress\b", re.IGNORECASE),
     "draft", "Body text indicates work-in-progress"),
]

# Freshness thresholds (days)
_FRESH_REVIEWED_DAYS = 180   # "Last reviewed" within 6 months = positive trust
_OLD_REVIEWED_DAYS = 365     # "Last reviewed" over 1 year ago = risk


@dataclass
class TrustMetadata:
    """Parsed document metadata from body content."""

    last_reviewed: str | None = None
    last_modified: str | None = None
    owner: str | None = None
    declared_status: str | None = None
    replaced_by: str | None = None
    deprecated_as_of: str | None = None
    canonical: bool = False
    review_cadence: str | None = None
    applies_to: str | None = None
    lifecycle: Lifecycle = "unknown"
    lifecycle_evidence: list[str] = field(default_factory=list)
    applicability_scope: dict[str, str] = field(default_factory=dict)


@dataclass
class TrustEvidence:
    """Structured explanation of why a document received its status."""

    summary: str = ""
    positive_evidence: list[str] = field(default_factory=list)
    review_risks: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    recommended_action: str = ""


@dataclass
class TrustVerdict:
    """The output of trust classification for one document."""

    status: Status
    confidence: float
    reason: str
    metadata: TrustMetadata = field(default_factory=TrustMetadata)
    evidence: TrustEvidence = field(default_factory=TrustEvidence)

    @property
    def lifecycle(self) -> Lifecycle:
        """Lifecycle state, stored in metadata."""
        return self.metadata.lifecycle

    @property
    def lifecycle_evidence(self) -> list[str]:
        """Lifecycle evidence, stored in metadata."""
        return self.metadata.lifecycle_evidence


def classify(
    doc: Document,
    signals: list[StalenessSignal],
    incoming_ref_count: int = 0,
    *,
    scan_titles: dict[str, str] | None = None,
    scan_scopes: dict[str, dict[str, str]] | None = None,
) -> TrustVerdict:
    """Classify a single document's trust status.

    Parameters
    ----------
    scan_titles
        Optional mapping of ``{doc_id: title}`` for every document in the
        current scan.  Used for scan-local supersession detection
        (e.g. "Guide 2021" is stale when "Guide 2024" exists).
    """

    parsed = _parse_body_metadata(doc)
    meta = _build_trust_metadata(doc, parsed)
    missing = _compute_missing_evidence(incoming_ref_count, parsed)

    # Collect positive trust signals regardless of outcome (used for context)
    trust_reasons = _check_trust_evidence(doc, signals, incoming_ref_count, parsed)

    # --- Step 1: Explicit stale evidence wins ---
    stale_reasons = _check_stale_evidence(signals, parsed, doc)

    # Scan-local supersession (only adds evidence, never sole stale trigger
    # unless the title has a clearly stale suffix like "(old)").
    if scan_titles is not None:
        supersession = _check_scan_supersession(
            doc, scan_titles,
            my_scope=parsed.applicability_scope,
            scan_scopes=scan_scopes,
        )
        stale_reasons.extend(supersession)

    # Detect lifecycle from existing evidence (no duplicate parsing)
    meta.lifecycle, meta.lifecycle_evidence = _detect_lifecycle(doc, parsed, stale_reasons)

    # Apply lifecycle → status policy: adjusts stale reasons and injects
    # lifecycle-derived review risks before the priority ladder runs.
    policy = _apply_lifecycle_status_policy(meta.lifecycle, stale_reasons, trust_reasons)
    stale_reasons = policy.effective_stale_reasons

    if stale_reasons:
        # Contradiction detection: if there is both stale evidence AND strong
        # authoritative evidence (e.g. Status: Legacy but referenced by 5 docs),
        # force needs_review instead of stale — a human must arbitrate.
        has_contradiction = bool(trust_reasons or policy.contradiction_reasons)
        if has_contradiction:
            all_reasons = (
                ["Contradictory evidence: stale signals conflict with trust signals"]
                + policy.contradiction_reasons
                + stale_reasons + trust_reasons
            )
            confidence = round(min(0.70, _stale_confidence(signals, parsed, stale_reasons)), 2)
            return TrustVerdict(
                status="needs_review",
                confidence=confidence,
                reason="; ".join(all_reasons),
                metadata=meta,
                evidence=_build_evidence("needs_review", trust_reasons, stale_reasons, missing),
            )

        confidence = _stale_confidence(signals, parsed, stale_reasons)
        return TrustVerdict(
            status="stale",
            confidence=confidence,
            reason="; ".join(stale_reasons),
            metadata=meta,
            evidence=_build_evidence("stale", trust_reasons, stale_reasons, missing),
        )

    # --- Step 2: Hard risk evidence → needs_review ---
    hard_risk, soft_risk = _check_risk_evidence(signals, parsed)

    # Merge lifecycle-derived review risks into hard risks
    if policy.lifecycle_review_risks:
        hard_risk = hard_risk + policy.lifecycle_review_risks

    if hard_risk:
        all_risk = hard_risk + soft_risk
        context = _review_context(signals, incoming_ref_count, parsed, all_risk)
        all_reasons = all_risk + context
        confidence = _risk_confidence(signals, all_reasons)
        return TrustVerdict(
            status="needs_review",
            confidence=confidence,
            reason="; ".join(all_reasons),
            metadata=meta,
            evidence=_build_evidence("needs_review", trust_reasons, all_risk, missing),
        )

    # --- Step 3: Positive trust evidence → current (only if no active risks) ---
    # Any active review risk (hard or soft) forces needs_review even when
    # positive trust evidence exists.  Positive evidence is preserved in the
    # structured explanation so reviewers can see what's going for the doc.
    if trust_reasons:
        if soft_risk:
            # Soft risk forces needs_review; preserve positive evidence
            context = _review_context(signals, incoming_ref_count, parsed, soft_risk)
            all_reasons = soft_risk + context
            confidence = _risk_confidence(signals, all_reasons)
            return TrustVerdict(
                status="needs_review",
                confidence=confidence,
                reason="; ".join(all_reasons),
                metadata=meta,
                evidence=_build_evidence("needs_review", trust_reasons, soft_risk, missing),
            )
        confidence = _trust_confidence(trust_reasons, incoming_ref_count)
        return TrustVerdict(
            status="current",
            confidence=confidence,
            reason="; ".join(trust_reasons),
            metadata=meta,
            evidence=_build_evidence("current", trust_reasons, [], missing),
        )

    # --- Step 4: Soft risk alone with no trust evidence → needs_review ---
    if soft_risk:
        context = _review_context(signals, incoming_ref_count, parsed, soft_risk)
        all_reasons = soft_risk + context
        confidence = _risk_confidence(signals, all_reasons)
        return TrustVerdict(
            status="needs_review",
            confidence=confidence,
            reason="; ".join(all_reasons),
            metadata=meta,
            evidence=_build_evidence("needs_review", [], soft_risk, missing),
        )

    # --- Step 5: Insufficient evidence → unknown ---
    unknown_reasons = _build_unknown_reasons(doc, signals, incoming_ref_count, parsed)
    confidence = _unknown_confidence(doc)
    return TrustVerdict(
        status="unknown",
        confidence=confidence,
        reason="; ".join(unknown_reasons) if unknown_reasons else "Insufficient positive trust evidence",
        metadata=meta,
        evidence=_build_evidence("unknown", [], [], missing),
    )


# ---------------------------------------------------------------------------
# Body metadata parsing
# ---------------------------------------------------------------------------

@dataclass
class _ParsedMeta:
    status_text: str | None = None
    owner: str | None = None
    last_reviewed: datetime | None = None
    replaced_by: str | None = None
    deprecated_as_of: str | None = None
    canonical: bool = False
    review_cadence: str | None = None
    applies_to: str | None = None
    applicability_scope: dict[str, str] = field(default_factory=dict)


def _parse_body_metadata(doc: Document) -> _ParsedMeta:
    meta = _ParsedMeta()
    m = _STATUS_FIELD_RE.search(doc.content)
    if m:
        meta.status_text = m.group(1).strip()
    m = _OWNER_FIELD_RE.search(doc.content)
    if m:
        meta.owner = m.group(1).strip()
    m = _LAST_REVIEWED_RE.search(doc.content)
    if m:
        try:
            meta.last_reviewed = datetime.strptime(m.group(1), "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    m = _REPLACED_BY_RE.search(doc.content)
    if m:
        meta.replaced_by = m.group(1).strip()
    m = _DEPRECATED_AS_OF_RE.search(doc.content)
    if m:
        meta.deprecated_as_of = m.group(1).strip()
    m = _CANONICAL_RE.search(doc.content)
    if m:
        meta.canonical = True
    m = _REVIEW_CADENCE_RE.search(doc.content)
    if m:
        meta.review_cadence = m.group(1).strip()
    m = _APPLIES_TO_RE.search(doc.content)
    if m:
        meta.applies_to = m.group(1).strip()

    # Parse structured applicability scope fields
    scope: dict[str, str] = {}
    for key, pattern in _SCOPE_FIELD_PATTERNS:
        m = pattern.search(doc.content)
        if m:
            scope[key] = m.group(1).strip()
    # Compact Scope: line — semicolon-separated key=value pairs.
    # Only whitelisted dimension keys (derived from _SCOPE_FIELD_PATTERNS) are
    # accepted; unknown keys are ignored so arbitrary prose cannot suppress
    # scan-local stale detection via _scopes_coexist().
    _SCOPE_ALLOWED_KEYS = frozenset(key for key, _ in _SCOPE_FIELD_PATTERNS)
    m = _SCOPE_COMPACT_RE.search(doc.content)
    if m:
        for pair in m.group(1).split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                norm_key = k.strip().lower().replace(" ", "_")
                if norm_key in _SCOPE_ALLOWED_KEYS and v.strip():
                    scope.setdefault(norm_key, v.strip())
    meta.applicability_scope = scope

    return meta


def _build_trust_metadata(doc: Document, parsed: _ParsedMeta) -> TrustMetadata:
    return TrustMetadata(
        last_reviewed=(
            parsed.last_reviewed.strftime("%Y-%m-%d") if parsed.last_reviewed else None
        ),
        last_modified=(
            doc.last_modified.strftime("%Y-%m-%d") if doc.last_modified else None
        ),
        owner=parsed.owner,
        declared_status=parsed.status_text,
        replaced_by=parsed.replaced_by,
        deprecated_as_of=parsed.deprecated_as_of,
        canonical=parsed.canonical,
        review_cadence=parsed.review_cadence,
        applies_to=parsed.applies_to,
        applicability_scope=dict(parsed.applicability_scope),
    )


_SUMMARY_TEMPLATES: dict[Status, str] = {
    "current": "Recommended",
    "needs_review": "Needs review",
    "stale": "Stale",
    "unknown": "Insufficient evidence to classify",
}

_ACTION_TEMPLATES: dict[Status, str] = {
    "current": "Use as trusted reference",
    "needs_review": "Review before relying on this document",
    "stale": "Do not use as authoritative guidance",
    "unknown": "Verify accuracy before use",
}


def _build_evidence(
    status: Status,
    positive: list[str],
    risks: list[str],
    missing: list[str],
) -> TrustEvidence:
    """Build structured evidence for a verdict."""
    # Build a human-readable summary sentence
    if status == "current":
        if positive:
            summary = f"Recommended because {positive[0][0].lower()}{positive[0][1:]}"
            if len(positive) > 1:
                summary += f" and {len(positive) - 1} more positive indicator{'s' if len(positive) > 2 else ''}"
            summary += "."
        else:
            summary = "Recommended."
    elif status == "stale":
        if risks:
            summary = f"Stale because {risks[0][0].lower()}{risks[0][1:]}."
        else:
            summary = "Stale."
    elif status == "needs_review":
        if risks:
            summary = f"Needs review because {risks[0][0].lower()}{risks[0][1:]}."
        else:
            summary = "Needs review."
    else:
        summary = "Insufficient evidence to classify this document."

    return TrustEvidence(
        summary=summary,
        positive_evidence=list(positive),
        review_risks=list(risks),
        missing_evidence=list(missing),
        recommended_action=_ACTION_TEMPLATES.get(status, ""),
    )


def parse_applicability_scope(doc: Document) -> dict[str, str]:
    """Extract structured applicability scope from a document's body content.

    Returns a dict mapping dimension keys to values, e.g.
    ``{"version": "v1", "environment": "prod"}``.
    Returns an empty dict when no scope fields are found.
    Used by the auditor to pre-compute ``scan_scopes`` for scope-aware supersession.
    """
    return _parse_body_metadata(doc).applicability_scope


def parse_body_metadata(doc: Document) -> dict:
    """Public helper — returns parsed metadata as a dict for storage/display."""
    meta = _parse_body_metadata(doc)
    return {
        "parsed_status": meta.status_text,
        "parsed_owner": meta.owner,
        "parsed_last_reviewed": (
            meta.last_reviewed.strftime("%Y-%m-%d") if meta.last_reviewed else None
        ),
        "parsed_replaced_by": meta.replaced_by,
        "parsed_deprecated_as_of": meta.deprecated_as_of,
        "parsed_canonical": meta.canonical,
        "parsed_review_cadence": meta.review_cadence,
        "parsed_applies_to": meta.applies_to,
        "parsed_applicability_scope": dict(meta.applicability_scope),
    }


# ---------------------------------------------------------------------------
# Stale evidence
# ---------------------------------------------------------------------------

def _check_stale_evidence(
    signals: list[StalenessSignal],
    parsed: _ParsedMeta,
    doc: Document | None = None,
) -> list[str]:
    reasons: list[str] = []
    for s in signals:
        if s.signal_type == "duplicate":
            title = s.details.get("duplicate_title", "another document")
            reasons.append(f"Exact duplicate of '{title}'")
        elif s.signal_type == "version_marker":
            found = s.details.get("found_version", "")
            current = s.details.get("current_version", "")
            current_title = s.details.get("current_doc_title", "another document")
            reasons.append(
                f"Contains {found}, but {current} exists in '{current_title}'"
            )
        elif s.signal_type == "version_ref":
            found = s.details.get("found_version", "")
            current = s.details.get("current_version", "")
            reasons.append(f"References outdated version {found} (current is {current})")
        elif s.signal_type == "replacement_link":
            target = s.details.get("target_title", "another document")
            reasons.append(
                f"Explicitly links to '{target}' as the replacement for this document"
            )

    # Body status = Legacy / Deprecated / Archived / Superseded etc. → stale
    if parsed.status_text:
        for kw in _STALE_STATUS_KEYWORDS:
            if kw in parsed.status_text.lower():
                reasons.append(f"Status field indicates '{parsed.status_text}'")
                break

    # Body-text supersession phrases
    if doc is not None:
        for pat, label in _SUPERSESSION_PHRASES:
            if pat.search(doc.content):
                reasons.append(label)
                break  # one phrase is enough

    # Structured metadata: "Replaced by" / "Superseded by" field
    if parsed.replaced_by:
        reasons.append(f"Replaced by '{parsed.replaced_by}'")

    # Structured metadata: "Deprecated as of" field
    if parsed.deprecated_as_of:
        reasons.append(f"Deprecated as of {parsed.deprecated_as_of}")

    # Notion archived metadata
    if doc is not None and doc.metadata.get("archived"):
        reasons.append("Source metadata indicates page is archived")

    return reasons


def _stale_confidence(
    signals: list[StalenessSignal],
    parsed: _ParsedMeta,
    stale_reasons: list[str] | None = None,
) -> float:
    score = 0.0
    has_age = False
    for s in signals:
        if s.signal_type == "duplicate":
            score += 0.45
        elif s.signal_type == "version_marker":
            score += 0.50
        elif s.signal_type == "version_ref":
            score += 0.30
        elif s.signal_type == "replacement_link":
            score += 0.45
        elif s.signal_type == "age":
            has_age = True
            score += 0.15 if s.severity == Severity.CRITICAL else 0.10
    if parsed.status_text:
        for kw in _STALE_STATUS_KEYWORDS:
            if kw in parsed.status_text.lower():
                score += 0.30
                break
    # Body-text supersession phrases and structured metadata
    if stale_reasons:
        if any("Body text says" in r for r in stale_reasons):
            score += 0.30
        if any("archived" in r.lower() and "metadata" in r.lower() for r in stale_reasons):
            score += 0.25
        if any("superseded by a related page" in r.lower() for r in stale_reasons):
            score += 0.25
        if any("replaced by" in r.lower() for r in stale_reasons):
            score += 0.35
        if any("deprecated as of" in r.lower() for r in stale_reasons):
            score += 0.30
    if has_age and score > 0.10:
        score += 0.10
    return round(min(0.95, max(0.3, score)), 2)


# ---------------------------------------------------------------------------
# Risk evidence
# ---------------------------------------------------------------------------

def _check_risk_evidence(
    signals: list[StalenessSignal], parsed: _ParsedMeta,
) -> tuple[list[str], list[str]]:
    """Return (hard_risk, soft_risk) reason lists.

    Hard risk (unresolved refs, broken links, near-duplicates, critical age)
    always triggers needs_review.

    Soft risk (old last-reviewed date) only triggers needs_review when there
    is no strong trust evidence to outweigh it.
    """
    hard: list[str] = []
    soft: list[str] = []
    broken_count = 0
    for s in signals:
        if s.signal_type == "unresolved_reference":
            ref_title = s.details.get("referenced_title", "unknown")
            hard.append(f"Contains unresolved reference: {ref_title}")
        elif s.signal_type == "ambiguous_reference":
            ref_title = s.details.get("referenced_title", "unknown")
            n = len(s.details.get("matching_doc_ids", []))
            hard.append(f"Ambiguous reference '{ref_title}' matches {n} documents")
        elif s.signal_type == "broken_link":
            broken_count += 1
        elif s.signal_type == "near_duplicate":
            title = s.details.get("similar_title", "another document")
            sim = s.details.get("similarity", 0)
            hard.append(f"{sim:.0f}% similar to '{title}'")
        elif s.signal_type == "broken_internal_link":
            url = s.details.get("url", "")
            hard.append(f"Internal link cannot be resolved: {url}")
        elif s.signal_type == "ambiguous_internal_link":
            url = s.details.get("url", "")
            n = len(s.details.get("matching_doc_ids", []))
            hard.append(f"Ambiguous internal link '{url}' matches {n} documents")
        elif s.signal_type == "age" and s.severity == Severity.CRITICAL:
            age_days = s.details.get("age_days", 0)
            hard.append(f"Not modified in {age_days} days")
    if broken_count == 1:
        hard.append("Contains 1 broken link")
    elif broken_count > 1:
        hard.append(f"Contains {broken_count} broken links")

    # Old last-reviewed date = soft risk (doesn't override strong trust)
    if parsed.last_reviewed:
        days = (datetime.now(timezone.utc) - parsed.last_reviewed).days
        if days > _OLD_REVIEWED_DAYS:
            soft.append(
                f"Last reviewed {parsed.last_reviewed.strftime('%Y-%m-%d')} ({days} days ago)"
            )

        # Review cadence overdue = soft risk
        if parsed.review_cadence:
            cadence_key = parsed.review_cadence.lower().strip()
            max_days = _CADENCE_DAYS.get(cadence_key)
            if max_days and days > max_days:
                soft.append(
                    f"Review cadence is '{parsed.review_cadence}' "
                    f"but last reviewed {days} days ago (max {max_days})"
                )

    return hard, soft


def _risk_confidence(
    signals: list[StalenessSignal], reasons: list[str],
) -> float:
    score = 0.35
    for s in signals:
        if s.signal_type in ("unresolved_reference", "ambiguous_reference"):
            score += 0.10
        elif s.signal_type == "broken_link":
            score += 0.08
        elif s.signal_type in ("broken_internal_link", "ambiguous_internal_link"):
            score += 0.10
        elif s.signal_type == "near_duplicate":
            score += 0.15
        elif s.signal_type == "age" and s.severity == Severity.CRITICAL:
            score += 0.10
    if any("Status field" in r for r in reasons):
        score += 0.15
    if any("Last reviewed" in r for r in reasons):
        score += 0.10
    return round(min(0.90, score), 2)


# ---------------------------------------------------------------------------
# Positive trust evidence
# ---------------------------------------------------------------------------

def _check_trust_evidence(
    doc: Document,
    signals: list[StalenessSignal],
    incoming_ref_count: int,
    parsed: _ParsedMeta,
) -> list[str]:
    """Return reasons if there is positive trust evidence.

    Strong signals (can trigger current on their own):
      - incoming references ≥ 2
      - body status field = "Current"

    Supporting signals (boost confidence but cannot trigger current alone):
      - body status = "Supported" / "Active" / etc.
      - outgoing resolved references (all resolve, none unresolved)
      - recent last-reviewed date
      - owner/DRI field
    """
    strong: list[str] = []
    supporting: list[str] = []

    # --- Strong ---
    if incoming_ref_count >= 2:
        strong.append(f"Referenced by {incoming_ref_count} other documents")

    if parsed.status_text:
        for kw in _STRONG_TRUST_KEYWORDS:
            if kw in parsed.status_text.lower():
                strong.append(f"Status field indicates '{parsed.status_text}'")
                break

    # Canonical: true/yes is a strong trust signal
    if parsed.canonical:
        strong.append("Marked as canonical document")

    # --- Supporting ---

    # "Supported" etc. are weaker than "Current"
    if parsed.status_text and not any("Status field" in s for s in strong):
        for kw in _SUPPORTING_TRUST_KEYWORDS:
            if kw in parsed.status_text.lower():
                supporting.append(f"Status field indicates '{parsed.status_text}'")
                break

    # Outgoing resolved with none unresolved (context, not proof of trust)
    resolved_out = [s for s in signals if s.signal_type == "resolved_reference"]
    unresolved_out = [
        s for s in signals
        if s.signal_type in ("unresolved_reference", "ambiguous_reference")
    ]
    if resolved_out and not unresolved_out:
        supporting.append(
            f"All {len(resolved_out)} outgoing references resolve correctly"
        )

    # Internal links (structured, from source): resolved with no broken ones
    resolved_internal = [s for s in signals if s.signal_type == "resolved_internal_link"]
    broken_internal = [
        s for s in signals
        if s.signal_type in ("broken_internal_link", "ambiguous_internal_link")
    ]
    if resolved_internal and not broken_internal:
        supporting.append(
            f"All {len(resolved_internal)} internal links resolve correctly"
        )

    # Recent last-reviewed date
    if parsed.last_reviewed:
        days = (datetime.now(timezone.utc) - parsed.last_reviewed).days
        if days <= _FRESH_REVIEWED_DAYS:
            supporting.append(
                f"Last reviewed {parsed.last_reviewed.strftime('%Y-%m-%d')}"
            )

    # Owner
    if parsed.owner:
        supporting.append(f"Has designated owner: {parsed.owner}")

    if not strong:
        return []

    return strong + supporting


def _trust_confidence(reasons: list[str], incoming_ref_count: int) -> float:
    score = 0.50
    for r in reasons:
        if r.startswith("Referenced by"):
            score += 0.10 + min(incoming_ref_count, 5) * 0.04
        elif "Status field indicates" in r:
            if "Current" in r:
                score += 0.12
            else:
                score += 0.05  # Supported etc.
        elif r == "Marked as canonical document":
            score += 0.15
        elif "outgoing references resolve" in r:
            score += 0.05
        elif "Last reviewed" in r:
            score += 0.10
        elif "designated owner" in r:
            score += 0.06
    return round(min(1.0, score), 2)


# ---------------------------------------------------------------------------
# Review context (enriches needs_review reasons)
# ---------------------------------------------------------------------------

def _review_context(
    signals: list[StalenessSignal],
    incoming_ref_count: int,
    parsed: _ParsedMeta,
    primary_reasons: list[str],
) -> list[str]:
    """Build additional context notes for needs_review documents.

    Returns up to 3 notes that are not already covered by *primary_reasons*.
    """
    notes: list[str] = []
    joined = " ".join(primary_reasons).lower()

    # Incoming references
    if "referenced by" not in joined and "incoming" not in joined:
        if incoming_ref_count >= 2:
            notes.append(f"Referenced by {incoming_ref_count} documents")
        elif incoming_ref_count == 1:
            notes.append("Referenced by 1 document")
        else:
            notes.append("No incoming references")

    # Status field (only supporting statuses — stale/current handled elsewhere)
    if parsed.status_text and "status" not in joined:
        lc = parsed.status_text.lower()
        is_stale = any(kw in lc for kw in _STALE_STATUS_KEYWORDS)
        is_current = any(kw in lc for kw in _STRONG_TRUST_KEYWORDS)
        if not is_stale and not is_current:
            notes.append(f"Status: {parsed.status_text}")

    # Outgoing references
    if "resolve" not in joined and "unresolved" not in joined:
        resolved = [s for s in signals if s.signal_type == "resolved_reference"]
        unresolved = [
            s for s in signals
            if s.signal_type in ("unresolved_reference", "ambiguous_reference")
        ]
        if resolved and not unresolved:
            notes.append(f"All {len(resolved)} outgoing references resolve")

    # Owner
    if parsed.owner and "owner" not in joined:
        notes.append(f"Owner: {parsed.owner}")

    return notes[:3]


# ---------------------------------------------------------------------------
# Unknown
# ---------------------------------------------------------------------------

def _build_unknown_reasons(
    doc: Document,
    signals: list[StalenessSignal],
    incoming_ref_count: int,
    parsed: _ParsedMeta,
) -> list[str]:
    reasons: list[str] = []
    if incoming_ref_count == 0:
        reasons.append("No incoming references from other documents")
    elif incoming_ref_count == 1:
        reasons.append("Only 1 incoming reference (minimum 2 needed for trust)")

    # Outgoing resolved but no incoming → explain why not current
    resolved_out = [s for s in signals if s.signal_type == "resolved_reference"]
    unresolved_out = [
        s for s in signals
        if s.signal_type in ("unresolved_reference", "ambiguous_reference")
    ]
    if resolved_out and not unresolved_out and incoming_ref_count < 2:
        reasons.append(
            "Outgoing references resolve, but no documents reference this page"
            if incoming_ref_count == 0
            else "Outgoing references resolve, but only 1 document references this page"
        )

    if parsed.last_reviewed is None:
        reasons.append("No 'Last reviewed' date in document body")

    # Note supporting status if present (not stale/current — those are handled elsewhere)
    if parsed.status_text:
        lc = parsed.status_text.lower()
        is_stale = any(kw in lc for kw in _STALE_STATUS_KEYWORDS)
        is_current = any(kw in lc for kw in _STRONG_TRUST_KEYWORDS)
        if not is_stale and not is_current:
            reasons.append(f"Status: {parsed.status_text}")

    if not reasons:
        reasons.append("Insufficient positive trust evidence")
    return reasons


def _compute_missing_evidence(
    incoming_ref_count: int, parsed: _ParsedMeta,
) -> list[str]:
    """Return list of absent trust markers."""
    missing: list[str] = []
    if incoming_ref_count == 0:
        missing.append("No incoming references from other documents")
    if parsed.last_reviewed is None:
        missing.append("No 'Last reviewed' date in document body")
    if parsed.owner is None:
        missing.append("No owner or DRI specified")
    if parsed.status_text is None:
        missing.append("No status field in document body")
    if parsed.review_cadence is None:
        missing.append("No review cadence specified")
    return missing


def _unknown_confidence(doc: Document) -> float:
    if doc.last_modified is None:
        return 0.15
    return 0.25


# ---------------------------------------------------------------------------
# Title normalization & scan-local supersession
# ---------------------------------------------------------------------------

def _scopes_coexist(scope_a: dict[str, str], scope_b: dict[str, str]) -> bool:
    """Return True if scopes explicitly indicate documents serve different audiences.

    Requires both scopes to share at least one dimension key with differing
    values — e.g. both have ``version`` but one is ``v1`` and the other ``v2``.
    Returns False when either scope is empty, or they share no keys, so that
    missing scope defaults to the existing (conservative) supersession behavior.
    """
    for key in scope_a:
        if key in scope_b:
            if scope_a[key].strip().lower() != scope_b[key].strip().lower():
                return True
    return False


def _check_scan_supersession(
    doc: Document,
    scan_titles: dict[str, str],
    my_scope: dict[str, str] | None = None,
    scan_scopes: dict[str, dict[str, str]] | None = None,
) -> list[str]:
    """Return stale reasons if this doc is superseded by a sibling in the scan.

    Rules:
    - A doc with a stale suffix like "(old)" is stale if a base sibling exists.
      Hard title suffixes are never suppressed by scope.
    - A doc with an older trailing year is stale if a newer year sibling exists,
      unless explicit scope shows the two docs validly coexist.
    - A doc with an older trailing version is stale if a newer version sibling
      exists, unless explicit scope shows they validly coexist.
    """
    reasons: list[str] = []
    my_base, my_ver, my_stale = normalize_title(doc.title)
    _my_scope = my_scope or {}
    _scan_scopes = scan_scopes or {}

    # Build index of scan siblings with the same base title
    siblings: list[tuple[str, str, str | None, str | None]] = []  # (id, title, ver, stale)
    for other_id, other_title in scan_titles.items():
        if other_id == doc.id:
            continue
        other_base, other_ver, other_stale = normalize_title(other_title)
        if other_base == my_base:
            siblings.append((other_id, other_title, other_ver, other_stale))

    if not siblings:
        return reasons

    # Rule 1: stale suffix like "(old)" + base or newer sibling exists → always stale.
    # Hard title suffixes are explicit negative evidence; scope cannot suppress them.
    if my_stale:
        reasons.append(
            f"Title has stale suffix '{my_stale}' and a newer page '{siblings[0][1]}' exists in this scan"
        )
        return reasons

    # Rule 2: trailing year — older year is stale when newer year exists,
    # unless explicit scope shows the two docs validly coexist.
    if my_ver and my_ver.isdigit() and len(my_ver) == 4:
        my_year = int(my_ver)
        for sib_id, sib_title, sib_ver, _ in siblings:
            if sib_ver and sib_ver.isdigit() and len(sib_ver) == 4:
                sib_year = int(sib_ver)
                if sib_year > my_year:
                    sib_scope = _scan_scopes.get(sib_id, {})
                    if not _scopes_coexist(_my_scope, sib_scope):
                        reasons.append(
                            f"Superseded by a related page in this scan: '{sib_title}' (year {sib_ver} > {my_ver})"
                        )
                        return reasons
            elif sib_ver is None:
                # Base title (no year) exists — might be the canonical version
                # Only flag as stale if the base sibling is not itself stale-suffixed
                pass

    # Rule 3: trailing version — older version is stale when newer exists,
    # unless explicit scope shows the two docs validly coexist.
    if my_ver and not (my_ver.isdigit() and len(my_ver) == 4):
        # Extract numeric version for comparison
        my_nums = re.findall(r"\d+", my_ver)
        if my_nums:
            my_num = tuple(int(n) for n in my_nums)
            for sib_id, sib_title, sib_ver, _ in siblings:
                if sib_ver and not (sib_ver.isdigit() and len(sib_ver) == 4):
                    sib_nums = re.findall(r"\d+", sib_ver)
                    if sib_nums:
                        sib_num = tuple(int(n) for n in sib_nums)
                        if sib_num > my_num:
                            sib_scope = _scan_scopes.get(sib_id, {})
                            if not _scopes_coexist(_my_scope, sib_scope):
                                reasons.append(
                                    f"Superseded by a related page in this scan: '{sib_title}' (version {sib_ver} > {my_ver})"
                                )
                                return reasons

    return reasons


# ---------------------------------------------------------------------------
# Lifecycle / stale-reason predicates
# ---------------------------------------------------------------------------


def _is_scan_local_version_inference(reason: str) -> bool:
    """True if *reason* is a weak year/version sibling inference.

    These are inferred from title similarity (e.g. "API Guide v1" vs
    "API Guide v2") and are weak evidence that can be suppressed by
    lifecycle ``supported`` or ``current``.
    """
    return "related page in this scan" in reason.lower()


def _is_title_stale_suffix(reason: str) -> bool:
    """True if *reason* comes from a parenthetical title suffix like '(old)'.

    Title stale suffixes are explicit negative evidence and must NOT be
    suppressed by lifecycle ``supported``.
    """
    return "stale suffix" in reason.lower()


# Suffixes that are cautionary (draft) vs hard-negative (old, deprecated, etc.)
_CAUTIONARY_TITLE_SUFFIXES = {"draft"}


def _is_cautionary_title_suffix(reason: str) -> bool:
    """True if the title stale suffix is cautionary rather than hard-negative.

    ``(draft)`` is cautionary — it aligns with lifecycle ``draft`` and should
    follow the draft policy (needs_review), not be treated as hard stale evidence.
    """
    if not _is_title_stale_suffix(reason):
        return False
    r = reason.lower()
    return any(f"'{s}'" in r or f"({s})" in r for s in _CAUTIONARY_TITLE_SUFFIXES)


def _is_scan_local_inferred_supersession(reason: str) -> bool:
    """True for any scan-local supersession evidence (version OR title suffix).

    Used by lifecycle detection to defer scan-local evidence to a lower
    priority step.  For policy filtering, prefer the more specific
    ``_is_scan_local_version_inference`` and ``_is_title_stale_suffix``.
    """
    return _is_scan_local_version_inference(reason) or _is_title_stale_suffix(reason)


# ---------------------------------------------------------------------------
# Lifecycle detection
# ---------------------------------------------------------------------------

def _detect_lifecycle(
    doc: Document,
    parsed: _ParsedMeta,
    stale_reasons: list[str],
) -> tuple[Lifecycle, list[str]]:
    """Derive a lifecycle label from already-parsed metadata and stale evidence.

    Returns (lifecycle, evidence_list).  Does not duplicate stale detection —
    reuses *parsed* and *stale_reasons* produced by the existing classifier.
    """
    evidence: list[str] = []

    # 1. Source metadata: archived flag (strongest — comes from the platform)
    if doc.metadata.get("archived"):
        evidence.append("Source metadata indicates page is archived")
        return "archived", evidence

    # 2. Explicit replacement/supersession metadata
    if parsed.replaced_by:
        evidence.append(f"Replaced by '{parsed.replaced_by}'")
        return "superseded", evidence

    # 3. Explicit deprecation metadata
    if parsed.deprecated_as_of:
        evidence.append(f"Deprecated as of {parsed.deprecated_as_of}")
        return "deprecated", evidence

    # 4. Stale/negative status keywords from Status field
    #    (deprecated, legacy, archived, superseded, etc. — checked before
    #    positive keywords so that negative evidence outranks positive)
    if parsed.status_text:
        status_lower = parsed.status_text.lower()
        for keywords, lifecycle in _LIFECYCLE_STATUS_MAP:
            if lifecycle in ("deprecated", "superseded", "archived"):
                if any(kw in status_lower for kw in keywords):
                    evidence.append(f"Status field indicates '{parsed.status_text}'")
                    return lifecycle, evidence

    # 5. Stale reasons from body-text supersession phrases and explicit evidence
    #    (skip scan-local inferred supersession — that's checked at step 10)
    for reason in stale_reasons:
        reason_lower = reason.lower()
        # Skip inferred scan-local supersession (handled at lower priority)
        if _is_scan_local_inferred_supersession(reason):
            continue
        if "superseded" in reason_lower or "replaced" in reason_lower:
            evidence.append(reason)
            return "superseded", evidence
        if "deprecated" in reason_lower:
            evidence.append(reason)
            return "deprecated", evidence
        if "archived" in reason_lower:
            evidence.append(reason)
            return "archived", evidence
        if "no longer maintained" in reason_lower or "no longer authoritative" in reason_lower:
            evidence.append(reason)
            return "deprecated", evidence
        if "do not use" in reason_lower:
            evidence.append(reason)
            return "deprecated", evidence

    # 6. Title lifecycle clues (outrank positive status keywords)
    for pat, lifecycle, label in _LIFECYCLE_TITLE_PATTERNS:
        if pat.search(doc.title):
            evidence.append(label)
            return lifecycle, evidence

    # 7. Body-text lifecycle phrases (draft, experimental — not covered above)
    for pat, lifecycle, label in _LIFECYCLE_BODY_PATTERNS:
        if pat.search(doc.content):
            evidence.append(label)
            return lifecycle, evidence

    # 8. Positive/neutral status keywords (current, supported, active, etc.)
    if parsed.status_text:
        status_lower = parsed.status_text.lower()
        for keywords, lifecycle in _LIFECYCLE_STATUS_MAP:
            if lifecycle not in ("deprecated", "superseded", "archived"):
                if any(kw in status_lower for kw in keywords):
                    evidence.append(f"Status field indicates '{parsed.status_text}'")
                    return lifecycle, evidence

    # 9. Canonical marker
    if parsed.canonical:
        evidence.append("Marked as canonical document")
        return "current", evidence

    # 10. Explicit applicability evidence (outranks inferred supersession)
    if parsed.applies_to:
        evidence.append(f"Applies to: {parsed.applies_to}")
        return "supported", evidence

    # 11. Inferred scan-local supersession from title similarity
    for reason in stale_reasons:
        if _is_scan_local_inferred_supersession(reason):
            suffix = " (inferred from title suffix)" if _is_title_stale_suffix(reason) else " (inferred from title similarity)"
            evidence.append(f"{reason}{suffix}")
            return "superseded", evidence

    return "unknown", evidence


# ---------------------------------------------------------------------------
# Lifecycle → status policy
# ---------------------------------------------------------------------------


@dataclass
class _LifecyclePolicy:
    """Result of applying lifecycle semantics to status classification inputs."""

    effective_stale_reasons: list[str]
    lifecycle_review_risks: list[str]
    contradiction_reasons: list[str]


def _apply_lifecycle_status_policy(
    lifecycle: Lifecycle,
    stale_reasons: list[str],
    trust_reasons: list[str],
) -> _LifecyclePolicy:
    """Adjust stale reasons and inject review risks based on lifecycle meaning.

    This is the single place where lifecycle semantics influence audit status.
    It does NOT decide the final status — it adjusts the *inputs* that
    ``classify()`` feeds into its existing priority ladder.
    """
    effective = list(stale_reasons)
    risks: list[str] = []
    contradictions: list[str] = []

    if lifecycle in ("deprecated", "superseded", "archived"):
        # Negative lifecycle: stale reasons stay.  But if there is strong
        # trust evidence contradicting the lifecycle, inject a review risk
        # so that classify() pushes to needs_review instead of stale.
        if trust_reasons:
            contradictions.append(
                f"Lifecycle is '{lifecycle}' but trust evidence exists — human review needed"
            )
        return _LifecyclePolicy(effective, risks, contradictions)

    if lifecycle in ("experimental", "draft"):
        # Do not classify stale solely for being experimental/draft.
        # Remove weak version/year inference and cautionary title suffixes
        # like (draft).  Hard-negative suffixes like (old), (deprecated) stay.
        effective = [
            r for r in effective
            if not _is_scan_local_version_inference(r) and not _is_cautionary_title_suffix(r)
        ]
        # If there's no remaining explicit stale evidence, inject a review
        # risk to ensure needs_review (not unknown or current).
        if not effective:
            risks.append(
                f"Document lifecycle is '{lifecycle}' — needs review before relying on content"
            )
        return _LifecyclePolicy(effective, risks, contradictions)

    if lifecycle == "supported":
        # Supported older versions may coexist with newer siblings.
        # Remove weak version/year inference only — explicit negative title
        # suffixes like (archived), (obsolete), (copy), (backup) must not
        # be suppressed by 'supported'.
        filtered = [r for r in effective if not _is_scan_local_version_inference(r)]
        if len(filtered) < len(effective):
            contradictions.append(
                "Lifecycle is 'supported' — scan-local version supersession suppressed"
            )
        effective = filtered
        return _LifecyclePolicy(effective, risks, contradictions)

    if lifecycle == "current":
        # Declared-current should not be stale solely from weak version
        # inference.  Title stale suffixes remain as explicit evidence.
        non_inferred = [r for r in effective if not _is_scan_local_version_inference(r)]
        inferred = [r for r in effective if _is_scan_local_version_inference(r)]
        if inferred and not non_inferred:
            risks.append(
                "Lifecycle is 'current' but inferred version supersession exists — needs review"
            )
            effective = []
        else:
            effective = non_inferred
        return _LifecyclePolicy(effective, risks, contradictions)

    # lifecycle == "unknown" — no adjustments, fall through to existing logic
    return _LifecyclePolicy(effective, risks, contradictions)


# ---------------------------------------------------------------------------
# Audit actionability
# ---------------------------------------------------------------------------

#: Importance score threshold above which a conditionally-actionable document
#: is promoted to a workflow finding.
IMPORTANCE_THRESHOLD: int = 2

#: Lifecycles that always require human audit regardless of importance.
_ALWAYS_ACTIONABLE_LIFECYCLES: frozenset[str] = frozenset(
    {"deprecated", "superseded", "archived", "experimental", "draft"}
)

#: Signal types that always make a document actionable (hard risks).
_HARD_RISK_SIGNAL_TYPES: frozenset[str] = frozenset(
    {
        "unresolved_reference", "ambiguous_reference", "broken_link",
        "near_duplicate", "duplicate",
        "broken_internal_link", "ambiguous_internal_link",
    }
)


def _has_hard_review_risk(signals: list[StalenessSignal]) -> bool:
    """True if any signal represents a hard review risk (always triggers audit)."""
    return any(
        s.signal_type in _HARD_RISK_SIGNAL_TYPES
        or (s.signal_type == "age" and s.severity == Severity.CRITICAL)
        for s in signals
    )


def _has_cadence_overdue(review_risks: list[str]) -> bool:
    """True if the document's review cadence is overdue (always triggers audit)."""
    return any("review cadence" in r.lower() for r in review_risks)


def _compute_importance_score(
    trust_metadata: dict,
    incoming_ref_count: int,
    has_version_siblings: bool,
    is_suggested_replacement: bool,
    n_stale_siblings: int,
) -> tuple[int, list[str]]:
    """Return (score, reasons) representing how important a document is.

    Higher scores mean the document is referenced, maintained, and part of
    the active knowledge graph — making weak-evidence findings more worthy
    of human attention.
    """
    score = 0
    reasons: list[str] = []

    # Incoming reference count
    if incoming_ref_count >= 5:
        score += 3
        reasons.append(f"Referenced by {incoming_ref_count} documents (+3)")
    elif incoming_ref_count >= 2:
        score += 2
        reasons.append(f"Referenced by {incoming_ref_count} documents (+2)")
    elif incoming_ref_count == 1:
        score += 1
        reasons.append("Referenced by 1 document (+1)")

    # Document metadata signals
    if trust_metadata.get("owner"):
        score += 1
        reasons.append("Has designated owner (+1)")
    if trust_metadata.get("declared_status"):
        score += 1
        reasons.append("Has declared status field (+1)")
    lifecycle = trust_metadata.get("lifecycle", "unknown")
    if lifecycle and lifecycle != "unknown":
        score += 1
        reasons.append(f"Lifecycle is '{lifecycle}' (+1)")
    if trust_metadata.get("canonical"):
        score += 2
        reasons.append("Marked as canonical (+2)")
    if trust_metadata.get("applies_to"):
        score += 1
        reasons.append("Has applicability scope (+1)")

    # Graph context
    if is_suggested_replacement and n_stale_siblings > 0:
        score += 2
        label = "documents" if n_stale_siblings != 1 else "document"
        reasons.append(f"Suggested replacement for {n_stale_siblings} stale {label} (+2)")
    elif has_version_siblings:
        score += 1
        reasons.append("Part of a version family (+1)")

    return score, reasons


def compute_audit_actionability(
    status: str,
    trust_metadata: dict,
    trust_evidence: dict,
    signals: list[StalenessSignal],
    incoming_ref_count: int,
    has_version_siblings: bool = False,
    is_suggested_replacement: bool = False,
    n_stale_siblings: int = 0,
) -> dict:
    """Decide whether this audit result requires a human-audit workflow finding.

    Returns a dict of actionability fields suitable for merging into
    ``AuditResult.trust_metadata``.

    Key product rule: classification status != human-audit actionability.
    Low-importance ``unknown`` / soft-evidence ``needs_review`` documents should
    remain visible in scan results but should not generate actionable findings.
    """
    # Current documents never require audit.
    if status == "current":
        return {
            "requires_human_audit": False,
            "audit_priority": "none",
            "importance_score": 0,
            "importance_reasons": [],
            "actionability_reason": "Document is current — no audit required",
        }

    # --- Always-actionable conditions ---
    review_risks: list[str] = trust_evidence.get("review_risks", [])

    if status == "stale":
        return {
            "requires_human_audit": True,
            "audit_priority": "high",
            "importance_score": 0,
            "importance_reasons": [],
            "actionability_reason": "Document is stale",
        }

    lifecycle = trust_metadata.get("lifecycle", "unknown")
    if lifecycle in _ALWAYS_ACTIONABLE_LIFECYCLES:
        return {
            "requires_human_audit": True,
            "audit_priority": "high",
            "importance_score": 0,
            "importance_reasons": [],
            "actionability_reason": f"Lifecycle is '{lifecycle}' — always requires review",
        }

    if _has_hard_review_risk(signals):
        return {
            "requires_human_audit": True,
            "audit_priority": "high",
            "importance_score": 0,
            "importance_reasons": [],
            "actionability_reason": "Has hard review risk (broken links, unresolved references, etc.)",
        }

    if _has_cadence_overdue(review_risks):
        return {
            "requires_human_audit": True,
            "audit_priority": "high",
            "importance_score": 0,
            "importance_reasons": [],
            "actionability_reason": "Review cadence is overdue",
        }

    # --- Conditionally actionable: importance-based ---
    score, reasons = _compute_importance_score(
        trust_metadata, incoming_ref_count,
        has_version_siblings, is_suggested_replacement, n_stale_siblings,
    )

    if score >= IMPORTANCE_THRESHOLD:
        return {
            "requires_human_audit": True,
            "audit_priority": "medium",
            "importance_score": score,
            "importance_reasons": reasons,
            "actionability_reason": f"Important document requiring review (score {score})",
        }

    priority: AuditPriority = "low" if score > 0 else "none"
    return {
        "requires_human_audit": False,
        "audit_priority": priority,
        "importance_score": score,
        "importance_reasons": reasons,
        "actionability_reason": f"Insufficient importance signals to require audit (score {score})",
    }


# ---------------------------------------------------------------------------
# Cross-document helper
# ---------------------------------------------------------------------------

def compute_incoming_ref_counts(
    all_signals: dict[str, list[StalenessSignal]],
) -> dict[str, int]:
    """Count how many documents have a resolved_reference to each target doc."""
    incoming: dict[str, set[str]] = {}
    for source_id, signals in all_signals.items():
        for s in signals:
            if s.signal_type in ("resolved_reference", "resolved_internal_link"):
                target_id = s.details.get("resolved_doc_id") or s.details.get("target_id")
                if target_id and target_id != source_id:
                    incoming.setdefault(target_id, set()).add(source_id)
    return {doc_id: len(sources) for doc_id, sources in incoming.items()}
