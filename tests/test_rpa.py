from stocknav import framework
from stocknav.store import Store


def test_rpa_session_feeds_the_same_reconciliation_layer():
    con = Store().connect()
    Store(con).reset()
    try:
        result = framework.run_rpa(con=con)
        assert result["engine"] == "rpa"
        assert result["logged_in"] is True
        assert result["nav"]["groups"] > 0
        first_versions = result["nav"]["new_versions"]

        # A second RPA session replays identical evidence: no new versions.
        again = framework.run_rpa(con=con)
        assert again["nav"]["new_versions"] == 0
        assert again["nav"]["evidence_added"] == 0

        store = Store(con)
        # Every conclusion is versioned exactly once after the replay.
        for fund, vdate in store.candidate_groups():
            assert len(store.conclusion_versions(fund, vdate)) == 1
        assert first_versions == len(store.current_conclusions())
    finally:
        con.close()
