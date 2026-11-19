from stocknav.framework import (
    content_hash, dedup, Http, Sleeper, Transport,
)
from stocknav.store import Store
from stocknav.pipeline import run


def test_record_identity_is_stable():
    first = {"id": "1", "v": 2}
    same = {"id": "1", "v": 2}
    other = {"id": "1", "v": 3}
    assert content_hash(first) == content_hash(same)
    values, duplicate_count = dedup([first, same, other])
    assert len(values) == 2
    assert duplicate_count == 1


def test_transport_outage_is_retried_until_the_source_recovers():
    """The flaky /probe endpoint 429s twice and then recovers; the bounded
    retry loop must transparently resume instead of failing the run."""
    http = Http(Transport(), Sleeper())
    status, _hdr, _body, attempts = http.get("/probe")
    assert status == 200
    assert attempts == 2  # two failures were retried, then success


def test_fetch_all_survives_a_temporary_outage():
    con = Store().connect()
    Store(con).reset()
    try:
        result = run(con=con)
        assert result["nav"]["groups"] > 0
        # Resuming (second run) after the source "came back" adds no versions.
        resumed = run(con=con)
        assert resumed["nav"]["new_versions"] == 0
    finally:
        con.close()
