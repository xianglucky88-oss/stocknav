from stocknav.config import SPEC
from stocknav.pipeline import run
from stocknav.recon import Reconciler
from stocknav.store import Store


def _fresh_con():
    con = Store().connect()
    Store(con).reset()
    return con


def test_pipeline_returns_a_status():
    con = _fresh_con()
    try:
        result = run(con=con)
    finally:
        con.close()
    assert isinstance(result, dict)
    assert result.get("engine") == SPEC.engine
    assert result


def test_full_and_incremental_runs_are_regression_safe():
    con = _fresh_con()
    try:
        first = run(mode="full", con=con)
        baseline = first["nav"]
        assert baseline["new_versions"] == baseline["groups"] > 0

        # Re-running the full run must not create extra versions or evidence.
        rerun = run(mode="full", con=con)
        assert rerun["nav"]["new_versions"] == 0
        assert rerun["nav"]["evidence_added"] == 0
        assert rerun["nav"]["groups"] == baseline["groups"]

        # An incremental run over the same data converges to the same state.
        inc = run(mode="incremental", con=con)
        assert inc["nav"]["new_versions"] == 0
        store = Store(con)
        current_a = store.current_conclusions()
        assert len(current_a) == baseline["groups"]

        # And another full run after that still changes nothing.
        run(mode="full", con=con)
        assert store.current_conclusions() == current_a
    finally:
        con.close()


def test_incremental_run_after_new_evidence_revises_but_keeps_history():
    con = _fresh_con()
    try:
        run(mode="full", con=con)
        store = Store(con)
        # Pick an existing fund/day and deliver an amendment through the
        # reconciliation layer directly, mimicking a later source revision.
        existing = store.all_candidates()[0]
        fund, vdate = existing["fund_key"], existing["valuation_date"]
        before = store.conclusion_versions(fund, vdate)

        # Mimic a later source revision: a new row with a distinct record id,
        # a later delivery timestamp and a different NAV.
        base = existing["record_ts"].partition("T")[0]
        amended = {
            "fund_key": fund,
            "valuation_date": vdate,
            "source": existing["source"],
            "record_id": existing["record_id"] + "-R1",
            "record_ts": base + "T23:59:00Z",
            "raw_value": 9.99,
            "value": (existing["value"] or 1.0) + 0.5,
            "numeric": True,
            "condition": existing["condition"],
            "superseded": False,
        }
        from stocknav.kernel import build_candidate, _hash
        amended["candidate_hash"] = _hash({
            "fund_key": amended["fund_key"],
            "valuation_date": amended["valuation_date"],
            "source": amended["source"],
            "record_id": amended["record_id"],
            "record_ts": amended["record_ts"],
            "value": amended["value"],
            "condition": amended["condition"],
        })
        assert store.add_candidate(amended) is True

        Reconciler(store, expected_sources=getattr(SPEC, "expected_sources", ())) \
            .reconcile("run-test-amendment")

        after = store.conclusion_versions(fund, vdate)
        assert len(after) == len(before) + 1
        # Earlier versions remain untouched.
        assert [v["version_id"] for v in before] == \
               [v["version_id"] for v in after[:len(before)]]

        # Full regression run afterwards does not add another version.
        run(mode="full", con=con)
        assert len(store.conclusion_versions(fund, vdate)) == len(after)
    finally:
        con.close()


def test_interrupted_resume_via_repeated_pipeline_runs_is_stable():
    con = _fresh_con()
    try:
        # Simulate crash/resume: two consecutive full runs with identical input.
        r1 = run(mode="full", con=con)
        r2 = run(mode="full", con=con)
        store = Store(con)
        # Every group has exactly one conclusion version; no duplicates.
        for fund, vdate in store.candidate_groups():
            versions = store.conclusion_versions(fund, vdate)
            assert len(versions) == 1
        assert r2["nav"]["new_versions"] == 0
        # Run ledger records the deterministic run ids.
        assert store.get_run(r1["nav"]["run_id"]) is not None
    finally:
        con.close()
