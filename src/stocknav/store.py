
"""Real sqlite3 store: content-hash primary key, INSERT-OR-IGNORE dedup, watermark.

The original ``records`` / ``meta`` tables keep their exact behaviour.  The
NAV reconciliation layer adds append-only evidence and decision tables:

* ``nav_candidates`` -- every candidate ever observed, keyed by a content
  hash, so duplicate delivery is idempotent;
* ``nav_conclusions`` -- every published decision version (append-only,
  later revisions never overwrite or erase earlier history);
* ``nav_current``    -- one pointer per fund/valuation day to the current
  version;
* ``nav_runs``       -- run ledger so interrupted/resumed or partial runs can
  resume without double counting.
"""
import json, sqlite3
from .config import DB_PATH

class Store:
    def __init__(self, con=None):
        self.con = con
        if con is not None:
            self._init()

    def connect(self):
        con = sqlite3.connect(DB_PATH)
        self.__init__(con)
        return con

    def _init(self):
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS records("
            "content_hash TEXT PRIMARY KEY, id TEXT, ts TEXT, payload TEXT NOT NULL)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS nav_candidates("
            "candidate_hash TEXT PRIMARY KEY, fund_key TEXT NOT NULL,"
            " valuation_date TEXT NOT NULL, source TEXT NOT NULL,"
            " record_ts TEXT, payload TEXT NOT NULL)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS nav_conclusions("
            "version_id TEXT PRIMARY KEY, fund_key TEXT NOT NULL,"
            " valuation_date TEXT NOT NULL, version INTEGER NOT NULL,"
            " status TEXT NOT NULL, nav TEXT, basis TEXT NOT NULL,"
            " run_id TEXT NOT NULL)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS nav_current("
            "fund_key TEXT NOT NULL, valuation_date TEXT NOT NULL,"
            " version_id TEXT NOT NULL, status TEXT NOT NULL,"
            " PRIMARY KEY(fund_key, valuation_date))")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS nav_runs("
            "run_id TEXT PRIMARY KEY, mode TEXT, started_ts TEXT,"
            "finished_ts TEXT, summary TEXT)")
        self.con.execute(
            "CREATE INDEX IF NOT EXISTS idx_nav_cand_group "
            "ON nav_candidates(fund_key, valuation_date)")
        self.con.execute(
            "CREATE INDEX IF NOT EXISTS idx_nav_concl_group "
            "ON nav_conclusions(fund_key, valuation_date)")
        self.con.commit()

    @staticmethod
    def hash(record):
        blob = json.dumps(record, sort_keys=True, ensure_ascii=False)
        import hashlib
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    def upsert(self, record):
        cur = self.con.execute(
            "INSERT OR IGNORE INTO records(content_hash,id,ts,payload) VALUES(?,?,?,?)",
            (self.hash(record), str(record.get("id", "")),
             str(record.get("ts", "")), json.dumps(record, ensure_ascii=False)))
        self.con.commit()
        return cur.rowcount > 0

    def count(self):
        return self.con.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    def all_records(self):
        return [json.loads(p) for (_h, p) in
                self.con.execute("SELECT content_hash,payload FROM records")]

    def get(self, content_hash):
        row = self.con.execute(
            "SELECT payload FROM records WHERE content_hash=?", (content_hash,)).fetchone()
        return json.loads(row[0]) if row else None

    def watermark(self):
        row = self.con.execute("SELECT v FROM meta WHERE k='watermark'").fetchone()
        return row[0] if row else None

    def set_watermark(self, value):
        self.con.execute(
            "INSERT INTO meta(k,v) VALUES('watermark',?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(value),))
        self.con.commit()

    def reset(self):
        self.con.execute("DELETE FROM records")
        self.con.execute("DELETE FROM meta")
        for tbl in ("nav_candidates", "nav_conclusions", "nav_current", "nav_runs"):
            self.con.execute("DELETE FROM %s" % tbl)
        self.con.commit()

    # ------------------------------------------------------------------ #
    # NAV reconciliation layer.
    # ------------------------------------------------------------------ #
    def add_candidate(self, candidate):
        """Idempotent evidence insert; returns True only when newly stored."""
        cur = self.con.execute(
            "INSERT OR IGNORE INTO nav_candidates(candidate_hash, fund_key,"
            " valuation_date, source, record_ts, payload) VALUES(?,?,?,?,?,?)",
            (candidate["candidate_hash"], candidate["fund_key"],
             candidate["valuation_date"], candidate["source"],
             candidate.get("record_ts", ""),
             json.dumps(candidate, ensure_ascii=False)))
        self.con.commit()
        return cur.rowcount > 0

    def all_candidates(self, fund_key=None, valuation_date=None):
        sql = "SELECT payload FROM nav_candidates"
        args = []
        if fund_key is not None:
            sql += " WHERE fund_key=?"
            args.append(fund_key)
            if valuation_date is not None:
                sql += " AND valuation_date=?"
                args.append(valuation_date)
        sql += " ORDER BY fund_key, valuation_date, source, record_ts"
        return [json.loads(r[0]) for r in self.con.execute(sql, args)]

    def candidate(self, candidate_hash):
        row = self.con.execute(
            "SELECT payload FROM nav_candidates WHERE candidate_hash=?",
            (candidate_hash,)).fetchone()
        return json.loads(row[0]) if row else None

    def candidate_groups(self):
        rows = self.con.execute(
            "SELECT DISTINCT fund_key, valuation_date FROM nav_candidates "
            "ORDER BY fund_key, valuation_date").fetchall()
        return [(r[0], r[1]) for r in rows]

    def current_version(self, fund_key, valuation_date):
        row = self.con.execute(
            "SELECT version_id FROM nav_current WHERE fund_key=? AND valuation_date=?",
            (fund_key, valuation_date)).fetchone()
        return row[0] if row else None

    def latest_version_no(self, fund_key, valuation_date):
        row = self.con.execute(
            "SELECT MAX(version) FROM nav_conclusions WHERE fund_key=? AND valuation_date=?",
            (fund_key, valuation_date)).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def add_conclusion(self, version_id, fund_key, valuation_date, version,
                       status, nav, basis, run_id):
        """Append a decision version. History rows are never updated."""
        self.con.execute(
            "INSERT INTO nav_conclusions(version_id, fund_key, valuation_date,"
            " version, status, nav, basis, run_id) VALUES(?,?,?,?,?,?,?,?)",
            (version_id, fund_key, valuation_date, version, status,
             None if nav is None else json.dumps(float(nav)),
             json.dumps(basis, ensure_ascii=False), run_id))
        self.con.execute(
            "INSERT INTO nav_current(fund_key, valuation_date, version_id, status)"
            " VALUES(?,?,?,?)"
            " ON CONFLICT(fund_key, valuation_date) DO UPDATE SET"
            " version_id=excluded.version_id, status=excluded.status",
            (fund_key, valuation_date, version_id, status))
        self.con.commit()

    def conclusion_versions(self, fund_key, valuation_date):
        return [
            {"version_id": r[0], "version": r[1], "status": r[2],
             "nav": None if r[3] is None else json.loads(r[3]),
             "basis": json.loads(r[4]), "run_id": r[5]}
            for r in self.con.execute(
                "SELECT version_id, version, status, nav, basis, run_id"
                " FROM nav_conclusions WHERE fund_key=? AND valuation_date=?"
                " ORDER BY version", (fund_key, valuation_date))]

    def current_conclusions(self):
        rows = self.con.execute(
            "SELECT nc.fund_key, nc.valuation_date, nc.version, nc.status,"
            " nc.nav, nc.basis, nc.run_id FROM nav_current cur"
            " JOIN nav_conclusions nc ON nc.version_id = cur.version_id"
            " ORDER BY nc.fund_key, nc.valuation_date").fetchall()
        return [{"fund_key": r[0], "valuation_date": r[1], "version": r[2],
                 "status": r[3], "nav": None if r[4] is None else json.loads(r[4]),
                 "basis": json.loads(r[5]), "run_id": r[6]} for r in rows]

    # ------------------------------------------------------------------ #
    # Run ledger (interruption / resume support).
    # ------------------------------------------------------------------ #
    def start_run(self, run_id, mode, started_ts):
        cur = self.con.execute(
            "INSERT OR IGNORE INTO nav_runs(run_id, mode, started_ts) VALUES(?,?,?)",
            (run_id, mode, started_ts))
        self.con.commit()
        return cur.rowcount > 0

    def finish_run(self, run_id, finished_ts, summary):
        self.con.execute(
            "UPDATE nav_runs SET finished_ts=?, summary=? WHERE run_id=?",
            (finished_ts, json.dumps(summary, ensure_ascii=False), run_id))
        self.con.commit()

    def get_run(self, run_id):
        row = self.con.execute(
            "SELECT run_id, mode, started_ts, finished_ts, summary FROM nav_runs"
            " WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        return {"run_id": row[0], "mode": row[1], "started_ts": row[2],
                "finished_ts": row[3],
                "summary": json.loads(row[4]) if row[4] else None}

    def commit(self):
        self.con.commit()
