import copy
import json
import random

import pytest

from stocknav import kernel
from stocknav.kernel import (
    PENDING, PUBLISHED, CONFLICT, REVIEW,
    build_candidate, build_candidates, evaluate, evaluate_group, reduce,
    adjust_for_dividend, valuation_date_of,
)


def rec(rid, name="Alpha Fund", value=1.25, status="published",
        ts="2030-01-05T08:00:00Z", source="sto", **extra):
    d = {"id": rid, "name": name, "value": value, "status": status,
         "ts": ts, "source": source}
    d.update(extra)
    return d


# --------------------------------------------------------------------------- #
# Four lifecycle states.
# --------------------------------------------------------------------------- #
def test_unanimous_published_evidence_concludes_published():
    rows = [rec("a-1", source="sto", value=1.25),
            rec("b-1", source="xtx", value=1.25)]
    (d,) = evaluate(rows, expected_sources=("sto", "xtx"))
    assert d["status"] == PUBLISHED
    assert d["nav"] == 1.25
    assert d["basis"]["rule_version"] == kernel.RULE_VERSION
    assert d["basis"]["rationale"] == "unanimous_published"


def test_tentative_evidence_stays_pending():
    rows = [rec("a-1", source="sto", status="pending")]
    (d,) = evaluate(rows)
    assert d["status"] == PENDING
    assert d["nav"] is None
    assert d["basis"]["rationale"] == "awaiting_publication"


def test_distinct_values_conclude_conflict_deterministically():
    rows = [rec("a-1", source="sto", value=1.25),
            rec("b-1", source="xtx", value=1.30)]
    decisions = [evaluate(copy.deepcopy(rows))[0] for _ in range(5)]
    for d in decisions:
        assert d["status"] == CONFLICT
        assert d["nav"] is None
        assert d["basis"]["rationale"] == "distinct_values"
    # Tie (1:1) is broken by a fixed rule, never by arrival order.
    assert len({d["basis"]["leading_value"] for d in decisions}) == 1


def test_source_flagged_conflict_requires_review():
    rows = [rec("a-1", source="sto", value=1.25),
            rec("b-1", source="xtx", value=1.25, status="conflict")]
    (d,) = evaluate(rows)
    assert d["status"] == REVIEW
    assert d["basis"]["rationale"] == "source_reported_conflict"
    assert len(d["basis"]["conflicting"]) == 1


def test_missing_expected_source_keeps_day_pending():
    rows = [rec("a-1", source="sto", value=1.25)]
    (d,) = evaluate(rows, expected_sources=("sto", "xtx"))
    assert d["status"] == PENDING
    assert d["basis"]["missing_sources"] == ["xtx"]
    assert d["basis"]["rationale"] == "awaiting_expected_sources"


def test_suspended_source_is_recorded_not_silently_dropped():
    rows = [rec("a-1", source="sto", value=1.25),
            rec("b-1", source="xtx", status="withdrawn")]
    (d,) = evaluate(rows, expected_sources=("sto", "xtx"))
    assert d["status"] == PUBLISHED  # sto alone is unanimous
    assert d["basis"]["suspended_sources"] == ["xtx"]
    assert d["basis"]["withdrawn"]


# --------------------------------------------------------------------------- #
# Order independence and duplicate delivery.
# --------------------------------------------------------------------------- #
def _full_blob(rows, expected=()):
    return json.dumps(evaluate(copy.deepcopy(rows), expected),
                      sort_keys=True, ensure_ascii=False)


def test_conclusion_is_independent_of_arrival_order():
    rows = [
        rec("a-1", source="sto", value=1.25, ts="2030-01-05T08:00:00Z"),
        rec("b-1", source="xtx", value=1.30, ts="2030-01-05T09:00:00Z"),
        rec("c-1", source="cgs", value=1.25, status="pending",
            ts="2030-01-05T07:00:00Z"),
        rec("d-1", source="sto", value=1.20, status="published",
            ts="2030-01-05T10:00:00Z"),
        rec("e-1", source="xtx", status="withdrawn",
            ts="2030-01-05T11:00:00Z"),
    ]
    baseline = _full_blob(rows, expected=("sto", "xtx"))
    rng = random.Random(20261118)
    for _ in range(20):
        shuffled = copy.deepcopy(rows)
        rng.shuffle(shuffled)
        assert _full_blob(shuffled, expected=("sto", "xtx")) == baseline


def test_duplicate_delivery_is_idempotent_in_kernel():
    one = rec("a-1", source="sto", value=1.25, ts="2030-01-05T08:00:00Z")
    d1 = evaluate_group("alpha", "2030-01-05", build_candidates([one]))
    d2 = evaluate_group("alpha", "2030-01-05",
                        build_candidates([one, copy.deepcopy(one), copy.deepcopy(one)]))
    # The decision itself (state, nav, vote tallies) is unaffected by repeats;
    # only the bookkeeping superseded list may note the duplicate rows.
    for key in ("status", "nav"):
        assert d1[key] == d2[key]
    assert d1["basis"]["value_votes"] == d2["basis"]["value_votes"]
    assert d1["basis"]["rationale"] == d2["basis"]["rationale"]
    assert d2["status"] == d1["status"]


def test_rereport_supersedes_older_evidence_of_same_source():
    old = rec("a-1", source="sto", value=1.20, ts="2030-01-05T08:00:00Z")
    new = rec("a-2", source="sto", value=1.25, ts="2030-01-05T10:00:00Z")
    d = evaluate_group("alpha", "2030-01-05", build_candidates([old, new]))
    eligible_hashes = set(d["basis"]["eligible"])
    superseded_hashes = set(d["basis"]["superseded"])
    assert build_candidate(new)["candidate_hash"] in eligible_hashes
    assert build_candidate(old)["candidate_hash"] in superseded_hashes
    assert d["nav"] == 1.25


def test_rereport_in_reverse_order_still_picks_latest():
    old = rec("a-1", source="sto", value=1.20, ts="2030-01-05T08:00:00Z")
    new = rec("a-2", source="sto", value=1.25, ts="2030-01-05T10:00:00Z")
    d_newest_first = evaluate_group(
        "alpha", "2030-01-05", build_candidates([new, old]))
    d_oldest_first = evaluate_group(
        "alpha", "2030-01-05", build_candidates([old, new]))
    assert d_newest_first["nav"] == d_oldest_first["nav"] == 1.25


# --------------------------------------------------------------------------- #
# Transparency: basis and candidate evidence are reviewable.
# --------------------------------------------------------------------------- #
def test_basis_carries_votes_rule_and_candidate_hashes():
    rows = [rec("a-1", source="sto", value=1.25),
            rec("b-1", source="xtx", value=1.30),
            rec("c-1", source="cgs", value=1.25)]
    (d,) = evaluate(rows)
    hashes = {c["candidate_hash"] for c in d["candidates"]}
    assert set(d["basis"]["eligible"]) <= hashes
    votes = d["basis"]["value_votes"]
    assert sorted(v["count"] for v in votes.values()) == [1, 2]
    assert votes[repr(1.25)]["sources"] == ["cgs", "sto"]
    assert d["basis"]["tie_break"]


def test_valuation_date_is_derived_from_timestamp():
    assert valuation_date_of("2030-01-05T08:00:00Z") == "2030-01-05"
    cand = build_candidate(rec("a-1", ts="2030-01-05T08:00:00Z"))
    assert cand["valuation_date"] == "2030-01-05"


# --------------------------------------------------------------------------- #
# Dividend rebase (own, unchanged responsibility).
# --------------------------------------------------------------------------- #
def test_dividend_rebase_scales_value():
    assert adjust_for_dividend(2.0, 0.9) == pytest.approx(1.8)
    assert adjust_for_dividend(2.0) == pytest.approx(2.0)
    cand = build_candidate(rec("a-1", value=2.0, dividend_factor=0.9))
    assert cand["value"] == pytest.approx(1.8)
    assert cand["raw_value"] == 2.0


# --------------------------------------------------------------------------- #
# Back-compatible reducer.
# --------------------------------------------------------------------------- #
def test_reduce_keeps_legacy_fields_and_adds_status():
    rows = [rec("a-1", source="sto", value=1.25),
            rec("b-1", source="xtx", value=1.25)]
    out = reduce(copy.deepcopy(rows))
    for r in out:
        assert r["reconciled_value"] == 1.25
        assert r["consensus_hits"] == 2
        assert r["conflict"] == 0
        assert r["nav_status"] == PUBLISHED
        assert "rule_version" in r["nav_basis"]
