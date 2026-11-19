import copy
import sqlite3

import pytest

from stocknav.kernel import (
    build_candidate, build_candidates, PENDING, PUBLISHED, CONFLICT, REVIEW,
)
from stocknav.recon import Reconciler
from stocknav.store import Store


def rec(rid, name="Alpha Fund", value=1.25, status="published",
        ts="2030-01-05T08:00:00Z", source="sto", **extra):
    d = {"id": rid, "name": name, "value": value, "status": status,
         "ts": ts, "source": source}
    d.update(extra)
    return d


@pytest.fixture()
def store():
    con = sqlite3.connect(":memory:")
    st = Store(con)
    yield st
    con.close()


def _recon(store, expected=("sto", "xtx")):
    return Reconciler(store, expected_sources=expected)


# --------------------------------------------------------------------------- #
# Candidate evidence dedup.
# --------------------------------------------------------------------------- #
def test_identical_candidate_is_stored_once(store):
    rec_ = Reconciler(store)
    c = build_candidate(rec("a-1"))
    assert rec_.ingest(copy.deepcopy(c)) is True
    assert rec_.ingest(copy.deepcopy(c)) is False
    assert len(store.all_candidates()) == 1


def test_rereport_with_new_value_is_new_evidence_but_group_decides_deterministically(store):
    rec_ = _recon(store, expected=())
    old = build_candidate(rec("a-1", value=1.20, ts="2030-01-05T08:00:00Z"))
    new = build_candidate(rec("a-2", value=1.25, ts="2030-01-05T10:00:00Z"))
    rec_.ingest_many([old, new])
    report = rec_.reconcile("run-1")
    assert report["decisions"][0]["nav"] == 1.25
    # Reconcile again -> no new version, same result.
    again = rec_.reconcile("run-1")
    assert again["new_versions"] == 0
    assert again["decisions"][0]["nav"] == 1.25


# --------------------------------------------------------------------------- #
# Append-only history: revisions never erase published history.
# --------------------------------------------------------------------------- #
def test_revision_appends_version_and_keeps_published_history(store):
    rec_ = _recon(store)
    rec_.ingest_many(build_candidates([
        rec("a-1", source="sto", value=1.25),
        rec("b-1", source="xtx", value=1.25)]))
    first = rec_.reconcile("run-1")
    assert first["decisions"][0]["status"] == PUBLISHED
    assert first["decisions"][0]["version"] == 1

    # A later amendment from xtx conflicts with the published figure.
    rec_.ingest(build_candidate(rec("b-2", source="xtx", value=1.30,
                                    ts="2030-01-05T11:00:00Z")))
    second = rec_.reconcile("run-2")
    assert second["decisions"][0]["status"] == CONFLICT
    assert second["decisions"][0]["version"] == 2

    history = rec_.history("alpha", "2030-01-05")
    assert [v["version"] for v in history] == [1, 2]
    assert history[0]["status"] == PUBLISHED   # history not overwritten
    assert history[1]["status"] == CONFLICT
    current = rec_.current()[0]
    assert current["version"] == 2 and current["status"] == CONFLICT


def test_unchanged_decision_creates_no_extra_version(store):
    rec_ = _recon(store)
    rec_.ingest_many(build_candidates([rec("a-1", source="sto", value=1.25)]))
    r1 = rec_.reconcile("run-1")
    r2 = rec_.reconcile("run-2")
    r3 = rec_.reconcile("run-3")
    assert r1["new_versions"] == 1
    assert r2["new_versions"] == r3["new_versions"] == 0
    assert len(rec_.history("alpha", "2030-01-05")) == 1


def test_full_lifecycle_through_versions(store):
    rec_ = _recon(store)
    # pending (only tentative evidence)
    rec_.ingest(build_candidate(rec("a-1", source="sto", status="pending")))
    rec_.reconcile("run-1")
    assert rec_.current()[0]["status"] == PENDING
    # published after both sources confirm
    rec_.ingest_many(build_candidates([
        rec("a-2", source="sto", value=1.25, ts="2030-01-05T09:00:00Z"),
        rec("b-1", source="xtx", value=1.25, ts="2030-01-05T09:05:00Z")]))
    rec_.reconcile("run-2")
    assert rec_.current()[0]["status"] == PUBLISHED
    # conflict after amendment
    rec_.ingest(build_candidate(rec("b-2", source="xtx", value=1.30,
                                    ts="2030-01-05T10:00:00Z")))
    rec_.reconcile("run-3")
    assert rec_.current()[0]["status"] == CONFLICT
    # review once a source flags it
    rec_.ingest(build_candidate(rec("b-3", source="xtx", status="conflict",
                                    value=1.30, ts="2030-01-05T11:00:00Z")))
    rec_.reconcile("run-4")
    assert rec_.current()[0]["status"] == REVIEW
    statuses = [v["status"] for v in rec_.history("alpha", "2030-01-05")]
    assert statuses == [PENDING, PUBLISHED, CONFLICT, REVIEW]


# --------------------------------------------------------------------------- #
# Out-of-order arrivals converge to the same state.
# --------------------------------------------------------------------------- #
def test_out_of_order_arrivals_converge(store):
    batches = [
        [rec("b-1", source="xtx", value=1.30, ts="2030-01-05T11:00:00Z")],
        [rec("b-0", source="xtx", value=1.25, ts="2030-01-05T09:00:00Z")],
        [rec("a-1", source="sto", value=1.25, ts="2030-01-05T08:00:00Z")],
    ]
    rec_ = _recon(store)
    for i, batch in enumerate(batches, 1):
        rec_.ingest_many(build_candidates(batch))
        rec_.reconcile("run-%d" % i)
    current = rec_.current()[0]
    assert current["status"] == CONFLICT  # latest xtx value 1.30 vs sto 1.25
    # The same evidence, ingested in one shot, must give the identical decision.
    con2 = sqlite3.connect(":memory:")
    st2 = Store(con2)
    flat = [r for batch in batches for r in batch]
    r2 = _recon(st2)
    r2.ingest_many(build_candidates(flat))
    ref = r2.reconcile("run-once")["decisions"][0]
    assert (ref["status"], ref["nav"]) == (current["status"], current["nav"])
    con2.close()


# --------------------------------------------------------------------------- #
# Interruption and resume: replaying remaining work changes nothing extra.
# --------------------------------------------------------------------------- #
def test_interrupted_then_resumed_run_is_equivalent(store):
    rec_ = _recon(store)
    rows = [
        rec("a-1", source="sto", value=1.25),
        rec("b-1", source="xtx", value=1.25),
        rec("c-1", name="Beta Fund", source="sto", value=2.10,
            ts="2030-01-06T08:00:00Z"),
        rec("d-1", name="Beta Fund", source="xtx", value=2.10,
            ts="2030-01-06T08:00:00Z"),
    ]
    # "Crash" halfway: first two candidates ingested, one group reconciled.
    rec_.ingest_many(build_candidates(rows[:2]))
    rec_.reconcile("run-partial")
    # Resume from scratch with the full evidence set.
    rec_.ingest_many(build_candidates(rows[2:]))
    resumed = rec_.reconcile("run-resumed")
    assert resumed["groups"] == 2
    assert {d["status"] for d in resumed["decisions"]} == {PUBLISHED}
    # Replaying the whole thing one more time adds nothing.
    replay = rec_.reconcile("run-replay")
    assert replay["new_versions"] == 0
    assert len(rec_.current()) == 2
    for fund, vdate in store.candidate_groups():
        assert len(rec_.history(fund, vdate)) == 1


def test_run_ledger_records_completion(store):
    store.start_run("run-ledger", "full", "2030-01-05T00:00:00Z")
    rec_ = _recon(store)
    rec_.ingest_many(build_candidates([rec("a-1")]))
    summary = rec_.reconcile("run-ledger")
    store.finish_run("run-ledger", "2030-01-05T12:00:00Z", summary)
    info = store.get_run("run-ledger")
    assert info is not None and info["summary"]["groups"] == 1
    assert info["finished_ts"] == "2030-01-05T12:00:00Z"


# --------------------------------------------------------------------------- #
# Source outage handling.
# --------------------------------------------------------------------------- #
def test_source_outage_keeps_day_pending_then_recovers(store):
    rec_ = _recon(store, expected=("sto", "xtx"))
    rec_.ingest(build_candidate(rec("a-1", source="sto", value=1.25)))
    rec_.reconcile("run-1")
    current = rec_.current()[0]
    assert current["status"] == PENDING
    assert current["basis"]["missing_sources"] == ["xtx"]

    rec_.ingest(build_candidate(rec("b-1", source="xtx", value=1.25,
                                    ts="2030-01-05T09:00:00Z")))
    rec_.reconcile("run-2")
    assert rec_.current()[0]["status"] == PUBLISHED
    # The earlier pending decision remains in history.
    assert [v["status"] for v in rec_.history("alpha", "2030-01-05")] == \
        [PENDING, PUBLISHED]


# --------------------------------------------------------------------------- #
# Valuation-day progress and suspension evidence dedup (separate duties).
# --------------------------------------------------------------------------- #
def test_valuation_progress_rolls_up_per_day(store):
    rec_ = _recon(store)
    rec_.ingest_many(build_candidates([
        rec("a-1", name="Alpha Fund", source="sto", ts="2030-01-05T08:00:00Z"),
        rec("b-1", name="Beta Fund", source="xtx", ts="2030-01-05T08:00:00Z"),
        rec("c-1", name="Alpha Fund", source="sto", ts="2030-01-06T08:00:00Z"),
    ]))
    progress = rec_.valuation_progress()
    by_day = {p["valuation_date"]: p for p in progress}
    assert by_day["2030-01-05"]["funds"] == 2
    assert by_day["2030-01-05"]["sources"] == 2
    assert by_day["2030-01-05"]["candidates"] == 2
    assert by_day["2030-01-06"]["candidates"] == 1


def test_suspension_evidence_is_deduped_per_source_latest_wins(store):
    rec_ = _recon(store)
    rec_.ingest_many(build_candidates([
        rec("x-1", source="xtx", status="withdrawn",
            ts="2030-01-05T08:00:00Z"),
        rec("x-1", source="xtx", status="withdrawn",
            ts="2030-01-05T08:00:00Z"),  # identical repeat -> single evidence
        rec("x-2", source="xtx", status="withdrawn",
            ts="2030-01-05T09:00:00Z"),  # later notice supersedes
        rec("y-1", source="cgs", status="suspended",
            ts="2030-01-05T08:30:00Z"),
    ]))
    evidence = rec_.suspension_evidence()
    assert len(evidence) == 2  # one per (fund, day, source)
    xtx = next(e for e in evidence if e["source"] == "xtx")
    assert xtx["record_id"] == "x-2"
