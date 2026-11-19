# stocknav

A small offline data-processing service with a local HTTP fixture and SQLite storage. It is self-contained and does not require network access.

## Usage

```text
make run       # process the bundled source
make run_inc   # process using the incremental command-line mode
make test      # run the test suite
make mock      # start the local fixture service
```

The package exposes a Python entry point under `src/stocknav`. The fixture service supports the source formats used by the package, and the command-line entry point writes results to the local SQLite database. Re-running commands is safe for an existing database.

## NAV reconciliation

When the same fund / valuation day is reported by more than one source, operators see more than a single number. Each conclusion is one of four lifecycle states with the evidence behind it stored for review:

| State       | Meaning                                                                 |
|-------------|-------------------------------------------------------------------------|
| `published` | every eligible source has published the same NAV                        |
| `pending`   | evidence is tentative, or an expected source has not reported yet       |
| `conflict`  | eligible sources report different NAV values                            |
| `review`    | a source itself flagged its figure as conflicting; a human must review  |

### Determinism guarantees

The decision kernel (`stocknav.kernel`) is pure: no I/O, no wall clock, no hidden state.

* **Out-of-order arrival** – candidates are sorted by a fixed key
  (`source`, delivery timestamp, record id, value); shuffling the input
  never changes the decision.
* **Duplicate / repeated delivery** – candidates are content-addressed, so
  identical re-reports are stored once. When one source reports again, only
  its latest row is eligible; older rows remain visible as `superseded` in
  the decision basis.
* **Source outage** – sources configured in `SPEC.expected_sources` that
  send nothing are listed as `missing_sources` and keep the day `pending`;
  suspended/withdrawn notices are recorded separately, never silently
  dropped. When the source recovers, the next run promotes the day without
  any random variation.
* **Interruption / resume** – runs carry a deterministic id derived from
  their inputs, and a new conclusion version is appended only when the
  decision actually changes. Replaying an interrupted run adds nothing.
* **Full vs incremental** – both modes converge on the same state; the
  incremental mode only filters raw ingestion, while the reconciliation
  layer always evaluates the complete evidence set per valuation day.
* **Append-only history** – revisions insert a new version row and move the
  current pointer; previously published versions are never updated or
  erased (`nav_conclusions`).

### Reviewable basis

Every conclusion stores a `basis` snapshot containing the rule version
(`nav-rules-v1`), the machine-readable rationale, eligible/superseded/
withdrawn/conflicting candidate hashes, per-value vote tallies, missing and
suspended sources, and the deterministic tie-break rule. The full candidate
payloads live in `nav_candidates`, so the data, source conditions and the
rules that applied at decision time can always be reconstructed.

### SQLite tables

* `records` / `meta` – original raw record store and watermark (unchanged).
* `nav_candidates` – append-only, content-hashed candidate evidence.
* `nav_conclusions` – every decision version, append-only.
* `nav_current` – one pointer per fund / valuation day to the current version.
* `nav_runs` – run ledger for interruption and resume.

### Preserved responsibilities

* **Dividend rebase (分红折算)** – `kernel.adjust_for_dividend` applies the
  per-record `dividend_factor` before any voting, as its own testable step.
* **Valuation-day progress (估值日进度)** – `Reconciler.valuation_progress`
  rolls up funds, sources and candidate counts per valuation day.
* **Suspension evidence dedup (暂停证据去重)** –
  `Reconciler.suspension_evidence` keeps one latest notice per
  fund / day / source.
