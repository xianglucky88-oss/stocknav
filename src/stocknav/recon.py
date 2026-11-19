"""Stateful NAV reconciliation service on top of the pure kernel.

Responsibilities kept distinct and testable:

* evidence ingestion  -- candidate rows are content-addressed, so repeated
  delivery from one source is deduped idempotently;
* decision versions   -- conclusions are append-only.  A revision creates a
  new version row and moves the current pointer; previously published
  history is never updated or deleted;
* resumability        -- re-running the same evaluation only appends a
  version when the decision actually changed, so an interrupted and resumed
  run (or out-of-order arrivals) produces exactly the same final state as an
  uninterrupted one;
* valuation-day progress and suspension evidence are reported as separate
  read-model helpers.
"""
from __future__ import annotations

import hashlib
import json

from . import kernel


def _decision_digest(decision):
    blob = json.dumps(
        {"status": decision["status"], "nav": decision["nav"],
         "basis": decision["basis"]},
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def _version_id(fund_key, valuation_date, version, digest):
    return "v-%s-%s-%04d-%s" % (fund_key, valuation_date, version, digest)


class Reconciler:
    def __init__(self, store, expected_sources=()):
        self.store = store
        self.expected_sources = tuple(str(s).lower() for s in expected_sources)

    # ------------------------------------------------------------------ #
    # Evidence.
    # ------------------------------------------------------------------ #
    def ingest(self, candidate):
        """Store one candidate.  Repeated identical delivery is a no-op."""
        return self.store.add_candidate(candidate)

    def ingest_many(self, candidates):
        added = 0
        for c in candidates:
            added += 1 if self.ingest(c) else 0
        return added

    # ------------------------------------------------------------------ #
    # Decisions.
    # ------------------------------------------------------------------ #
    def evaluate_group(self, fund_key, valuation_date):
        candidates = self.store.all_candidates(fund_key, valuation_date)
        return kernel.evaluate_group(
            fund_key, valuation_date, candidates, self.expected_sources)

    def _current_digest(self, fund_key, valuation_date):
        version_id = self.store.current_version(fund_key, valuation_date)
        if not version_id:
            return None
        versions = self.store.conclusion_versions(fund_key, valuation_date)
        current = next(v for v in versions if v["version_id"] == version_id)
        return _decision_digest(current)

    def apply_decision(self, decision, run_id):
        """Append a version only when the decision differs from current.

        Returns ``(changed, version_id, version_no)``.  Idempotent: feeding
        the same decision twice never creates a second row, which is what
        makes interrupted/resumed runs safe.
        """
        fund_key, vdate = decision["fund_key"], decision["valuation_date"]
        digest = _decision_digest(decision)
        if self._current_digest(fund_key, vdate) == digest:
            version_id = self.store.current_version(fund_key, vdate)
            versions = self.store.conclusion_versions(fund_key, vdate)
            version_no = next(v["version"] for v in versions
                              if v["version_id"] == version_id)
            return False, version_id, version_no

        version_no = self.store.latest_version_no(fund_key, vdate) + 1
        version_id = _version_id(fund_key, vdate, version_no, digest)
        self.store.add_conclusion(
            version_id, fund_key, vdate, version_no,
            decision["status"], decision["nav"], decision["basis"], run_id)
        return True, version_id, version_no

    def reconcile(self, run_id, groups=None):
        """Re-evaluate the given groups (default: every group with evidence)."""
        groups = groups if groups is not None else self.store.candidate_groups()
        report = {"groups": 0, "new_versions": 0, "unchanged": 0,
                  "statuses": {s: 0 for s in kernel.STATUSES},
                  "decisions": []}
        for fund_key, vdate in sorted(groups):
            decision = self.evaluate_group(fund_key, vdate)
            changed, version_id, version_no = self.apply_decision(decision, run_id)
            report["groups"] += 1
            report["new_versions"] += 1 if changed else 0
            report["unchanged"] += 0 if changed else 1
            report["statuses"][decision["status"]] += 1
            report["decisions"].append({
                "fund_key": fund_key, "valuation_date": vdate,
                "status": decision["status"], "nav": decision["nav"],
                "version": version_no, "version_id": version_id,
                "changed": changed})
        return report

    # ------------------------------------------------------------------ #
    # Read models.
    # ------------------------------------------------------------------ #
    def valuation_progress(self):
        """Per valuation-day roll-up (估值日进度)."""
        progress = {}
        for c in self.store.all_candidates():
            slot = progress.setdefault(c["valuation_date"], {
                "valuation_date": c["valuation_date"],
                "funds": set(), "sources": set(),
                "candidates": 0, "suspended": 0, "published": 0})
            slot["funds"].add(c["fund_key"])
            slot["sources"].add(c["source"])
            slot["candidates"] += 1
            if c["condition"] == "withdrawn":
                slot["suspended"] += 1
            if c["condition"] == "published":
                slot["published"] += 1
        out = []
        for vdate in sorted(progress):
            slot = progress[vdate]
            slot["funds"] = len(slot["funds"])
            slot["sources"] = len(slot["sources"])
            out.append(slot)
        return out

    def suspension_evidence(self):
        """Deduped suspension/pause evidence (暂停证据去重).

        One entry per (fund, valuation day, source): if a source sends the
        same suspension notice repeatedly, only its latest notice survives.
        """
        latest = {}
        for c in self.store.all_candidates():
            if c["condition"] != "withdrawn":
                continue
            key = (c["fund_key"], c["valuation_date"], c["source"])
            prev = latest.get(key)
            if prev is None or (c["record_ts"], c["record_id"]) > \
                    (prev["record_ts"], prev["record_id"]):
                latest[key] = c
        return [latest[k] for k in sorted(latest)]

    def history(self, fund_key, valuation_date):
        return self.store.conclusion_versions(fund_key, valuation_date)

    def current(self):
        return self.store.current_conclusions()
