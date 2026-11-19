
from types import SimpleNamespace

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "stocknav.db"

SPEC = SimpleNamespace(
    slug="stocknav",
    fmt="json",
    engine="crawl",
    endpoint="items",
    columns=["id", "name", "value", "status", "ts"],
    page_size=4,
    int_fields=["value"],
    ts_field="ts",
    # NAV reconciliation settings.  Expected sources make "source outage"
    # explicit: a day with no evidence from one of them stays `pending` with
    # the missing source recorded in the decision basis.
    expected_sources=("sto", "xtx"),
)
