"""Deterministic NAV (net asset value) reconciliation kernel.

This module is intentionally pure: no I/O, no wall-clock time, no hidden
global state.  Given the same candidate evidence it always produces the same
decision, so duplicate delivery, out-of-order arrival, source outages or an
interrupted/resumed run can never make the conclusion for one valuation day
change randomly.

Conclusion lifecycle (one state per fund / valuation day):

* ``published``  -- every eligible source has published the same NAV
* ``pending``    -- evidence exists but publication is still tentative, or an
                    expected source has not reported yet
* ``conflict``   -- eligible sources report different NAV values
* ``review``     -- a source itself flagged its figure as conflicting and a
                    human must review it

Every decision carries a ``basis`` snapshot: the rule version that was
applied, which candidates were eligible / superseded / suspended, per-value
vote tallies, missing sources and a machine-readable rationale, so operators
can reconstruct afterwards why a conclusion looked the way it did.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json

RULE_VERSION = "nav-rules-v1"

PENDING = "pending"
PUBLISHED = "published"
CONFLICT = "conflict"
REVIEW = "review"

STATUSES = (PENDING, PUBLISHED, CONFLICT, REVIEW)

# Raw source status -> candidate condition (the "source condition").
_CONDITION_MAP = {
    "published": "published",
    "active": "published",
    "sold": "published",
    "tentative": "tentative",
    "pending": "tentative",
    "new": "tentative",
    "withdrawn": "withdrawn",
    "suspended": "withdrawn",
    "paused": "withdrawn",
    "conflict": "conflicting",
    "conflicting": "conflicting",
}
_USABLE_CONDITIONS = ("published", "tentative")


# --------------------------------------------------------------------------- #
# Field extraction helpers.
# --------------------------------------------------------------------------- #
def _ident(rec):
    nm = str(rec.get("name", ""))
    return nm.split()[0].lower() if nm else str(rec.get("id"))


def valuation_date_of(ts):
    """Return the valuation day (``YYYY-MM-DD``) behind a raw timestamp."""
    ts = str(ts or "")
    return ts.split("T", 1)[0].split(" ", 1)[0] or "unknown"


def candidate_condition(status):
    """Normalise a raw record status into a candidate evidence condition."""
    if not isinstance(status, str):
        return "tentative"
    return _CONDITION_MAP.get(status.strip().lower(), "tentative")


def source_of(record):
    """Extract the reporting source; fall back to the id prefix."""
    src = record.get("source")
    if src is not None and str(src).strip():
        return str(src).strip().lower()
    rid = str(record.get("id", ""))
    prefix = rid.split("-", 1)[0]
    return prefix.strip().lower() or "default"


def adjust_for_dividend(value, factor=1.0):
    """Dividend rebase (分红折算): scale a NAV by its dividend factor.

    Kept as a small standalone responsibility so the conversion rule is
    explicit, testable and reused by every candidate.
    """
    if factor in (None, ""):
        factor = 1.0
    return round(float(value) * float(factor), 6)


def _as_number(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _hash(blob):
    return hashlib.sha256(
        json.dumps(blob, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24]


# --------------------------------------------------------------------------- #
# Candidate construction.
# --------------------------------------------------------------------------- #
def process(record):
    """Per-record projection (back-compatible entry point, enriched)."""
    value = _as_number(record.get("value"))
    factor = record.get("dividend_factor", 1.0)
    adjusted = adjust_for_dividend(value, factor) if value is not None else None
    return {
        "merge_key": _ident(record),
        "src_value": record.get("value"),
        "valuation_date": valuation_date_of(record.get("ts")),
        "source": source_of(record),
        "condition": candidate_condition(record.get("status")),
        "adjusted_value": adjusted,
    }


def build_candidate(record):
    """Turn one raw record into a deterministic candidate evidence row."""
    value = _as_number(record.get("value"))
    factor = record.get("dividend_factor", 1.0)
    adjusted = adjust_for_dividend(value, factor) if value is not None else None
    rid = str(record.get("id", ""))
    cand = {
        "fund_key": _ident(record),
        "valuation_date": valuation_date_of(record.get("ts")),
        "source": source_of(record),
        "record_id": rid,
        "record_ts": str(record.get("ts", "")),
        "raw_value": record.get("value"),
        "value": adjusted,
        "numeric": adjusted is not None,
        "condition": candidate_condition(record.get("status")),
        "superseded": False,
    }
    cand["candidate_hash"] = _hash({
        "fund_key": cand["fund_key"],
        "valuation_date": cand["valuation_date"],
        "source": cand["source"],
        "record_id": cand["record_id"],
        "record_ts": cand["record_ts"],
        "value": cand["value"],
        "condition": cand["condition"],
    })
    return cand


def build_candidates(records):
    return [build_candidate(r) for r in records]


# --------------------------------------------------------------------------- #
# Group evaluation.
# --------------------------------------------------------------------------- #
def _sort_key(c):
    # Fully deterministic ordering: source, then delivery timestamp, then id.
    return (c["source"], c["record_ts"], c["record_id"],
            "" if c["value"] is None else repr(c["value"]), c["condition"])


def _select_latest_per_source(candidates):
    """Mark older evidence of the same source as superseded.

    A source may deliver more than once for the same day.  Only its latest
    row (by record timestamp, ties broken by id) is eligible; the rest stay
    visible in the basis snapshot but cannot influence the vote.
    """
    ordered = sorted(candidates, key=_sort_key)
    latest: dict[str, dict] = {}
    for c in ordered:
        latest[c["source"]] = c
    for c in ordered:
        c["superseded"] = latest[c["source"]] is not c
    return ordered


def evaluate_group(fund_key, valuation_date, candidates, expected_sources=()):
    """Evaluate every candidate for one fund / valuation day.

    Returns a JSON-serialisable decision.  Pure and order-independent:
    shuffling ``candidates`` yields an identical result.
    """
    rows = _select_latest_per_source([dict(c) for c in candidates])
    active = [c for c in rows if not c["superseded"]]
    expected = tuple(sorted(str(s).lower() for s in expected_sources))
    seen_sources = {c["source"] for c in active}
    missing_sources = tuple(s for s in expected if s not in seen_sources)
    suspended_sources = tuple(sorted(
        c["source"] for c in active if c["condition"] == "withdrawn"))

    eligible = [c for c in active
                if c["numeric"] and c["condition"] in _USABLE_CONDITIONS]
    eligible.sort(key=_sort_key)

    votes = Counter(c["value"] for c in eligible)
    value_votes = {}
    for val in sorted(votes, key=lambda v: repr(v)):
        backers = sorted(c["source"] for c in eligible if c["value"] == val)
        value_votes[_num_key(val)] = {"value": val, "count": votes[val],
                                      "sources": backers}

    conflicting = [c for c in active if c["condition"] == "conflicting"]
    non_numeric = [c for c in active if not c["numeric"]]

    # ---- Rule chain (rule version pinned in the basis snapshot) ---------- #
    status, nav, rationale = PENDING, None, "no_eligible_evidence"
    if conflicting:
        status, rationale = REVIEW, "source_reported_conflict"
    elif len(votes) > 1:
        status, rationale = CONFLICT, "distinct_values"
    elif not eligible:
        if missing_sources:
            rationale = "awaiting_expected_sources"
        elif suspended_sources:
            rationale = "all_sources_suspended"
        elif non_numeric:
            rationale = "no_numeric_value"
    else:
        (only_value,) = tuple(votes)
        if all(c["condition"] == "published" for c in eligible):
            status, nav, rationale = PUBLISHED, only_value, "unanimous_published"
        else:
            rationale = "awaiting_publication"
        if missing_sources:
            # An expected source has not reported at all: stay tentative.
            status, nav, rationale = PENDING, None, "awaiting_expected_sources"

    # Deterministic leading figure for transparency (never used to pick a
    # winner silently: conflicts stay conflicts).
    leading = None
    if votes:
        leading = sorted(votes.items(), key=lambda kv: (-kv[1], repr(kv[0])))[0][0]

    basis = {
        "rule_version": RULE_VERSION,
        "rationale": rationale,
        "expected_sources": list(expected),
        "seen_sources": sorted(seen_sources),
        "missing_sources": list(missing_sources),
        "suspended_sources": list(suspended_sources),
        "eligible": [c["candidate_hash"] for c in eligible],
        "superseded": [c["candidate_hash"] for c in rows if c["superseded"]],
        "withdrawn": [c["candidate_hash"] for c in active
                      if c["condition"] == "withdrawn"],
        "conflicting": [c["candidate_hash"] for c in conflicting],
        "non_numeric": [c["candidate_hash"] for c in non_numeric],
        "value_votes": value_votes,
        "leading_value": leading,
        "tie_break": "source_ts_id_then_value_lexicographic",
    }
    return {
        "fund_key": fund_key,
        "valuation_date": valuation_date,
        "status": status,
        "nav": nav,
        "basis": basis,
        "candidates": rows,
    }


def _num_key(val):
    # JSON object keys must be strings; keep a stable, sortable rendering.
    return repr(float(val))


def evaluate(records, expected_sources=()):
    """Group raw records/candidates by fund + valuation day and decide."""
    if records and isinstance(records[0], dict) and "candidate_hash" in records[0]:
        candidates = [dict(c) for c in records]
    else:
        candidates = build_candidates(records)
    grouped: dict[tuple, list] = defaultdict(list)
    for c in candidates:
        grouped[(c["fund_key"], c["valuation_date"])].append(c)
    decisions = []
    for key in sorted(grouped):
        decisions.append(
            evaluate_group(key[0], key[1], grouped[key], expected_sources))
    return decisions


def reduce(records):
    """Back-compatible reducer: annotate records with the group decision.

    Legacy fields (``reconciled_value`` / ``consensus_hits`` / ``conflict``)
    are still emitted so existing payloads keep their shape; the new
    ``nav_status`` / ``nav_basis`` fields carry the transparent decision.
    """
    decisions = evaluate(records)
    index = {(d["fund_key"], d["valuation_date"]): d for d in decisions}
    for r in records:
        key = (_ident(r), valuation_date_of(r.get("ts")))
        d = index.get(key)
        if d is None:
            continue
        votes = d["basis"]["value_votes"]
        hits = max((v["count"] for v in votes.values()), default=0)
        r["reconciled_value"] = d["nav"]
        r["consensus_hits"] = hits
        r["conflict"] = int(d["status"] == CONFLICT)
        r["nav_status"] = d["status"]
        r["nav_basis"] = d["basis"]
    return records
