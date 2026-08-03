# Session handoff — 2026-07-31

Written at the end of a long session on `homelab-trader`, stopping before the context limit. All facts below were re-verified against GitHub and the live deployment host (ubuntu-box, 10.10.10.13) immediately before writing this — nothing here is asserted from memory alone. No new code was written after verification began.

**2026-08-01 addendum**: `docs/research-backtesting-improvements.md` (its own docs-only PR) adds six research/backtesting roadmap items — transaction-cost sensitivity, a shared realistic fill model, intraday ambiguity handling, robustness/concentration reporting, intraday execution diagnostics, and an opening-range breakout research backlog entry. All extend Platform Improvements PR B/D (§3 below) and share PR A's dependency. Still design-only, not implemented.

## 1. Repository state

**`main`**: `5833b2a` ("Add DB rollback in per-symbol error handler and alert on ingest cycle failures") — unchanged all session. This is also exactly what's currently deployed and running on ubuntu-box (`invest-api`/`invest-ingest` containers, up 2 days, untouched by this session).

**Open PRs** (verified via `gh pr list`, all `OPEN`, `MERGEABLE`, all draft, none merged, none deployed):

| # | Title | Branch | Head commit | Base | Notes |
|---|---|---|---|---|---|
| [1](https://github.com/smaniktahla/homelab-trader/pull/1) | Signal-component shadow infrastructure (PR #1 of multi-source signal integration) | `feat/symbol-features-shadow-mode` | `39ead65` | `main` | Foundational — everything else in this session builds on it |
| [2](https://github.com/smaniktahla/homelab-trader/pull/2) | Add Symbol Performance Summary and round-trip history to symbol page | `feat/symbol-performance-summary` | `0eeaa52` | `main` | Independent of #1; average-cost reconstruction, explicitly a stand-in for future lifecycle accounting |
| [3](https://github.com/smaniktahla/homelab-trader/pull/3) | Add SEC EDGAR fundamentals collector in shadow mode (PR #2 of multi-source signal integration) | `feat/fundamentals-shadow-mode` | `161d675` | `feat/symbol-features-shadow-mode` | **Stacked on #1** — depends on its schema/infra, must merge after #1 |

**This handoff**: branch `docs/session-handoff-2026-07-31`, based on `main`, documentation-only.

**Branches not represented by a PR**: none. Every local/remote branch created this session maps to exactly one of the three PRs above (or this handoff branch).

## 2. Completed work

### PR #1 — Signal-component shadow infrastructure
- `symbol_features` (new, append-only, versioned per `symbol, side, as_of, feature_version`) and component-score columns on `signal_outcomes` (`technical_score`, `fundamental_score`, `earnings_score`, `news_score`, `options_score`, `macro_fit_score`, `data_confidence`, `feature_snapshot_id`, `feature_version`, `model_version`, `component_weights`, `vetoes`).
- `shared/signal_components.py` — pure `weighted_component_score()`: available-weight normalization, rejects negative weights, returns `None` (not `0`) when nothing is available or available weight is zero.
- `shared/feature_store.py` — `record_symbol_feature_snapshot()` / `attach_feature_snapshot()`, both fail-open (best-effort, own rollback, never raise past their own boundary), idempotent via `ON CONFLICT DO NOTHING` + fallback `SELECT`.
- Formalized `universe`/`universe_scan` into `schema.sql` (recovered from the live DB — pre-existing drift, unrelated to this PR's own logic, now fixed for these two tables specifically; **`trade_proposals`/`signal_params` remain undocumented in `schema.sql`, a larger pre-existing gap left unfixed and explicitly called out in the schema comments**).
- `GET /api/symbol-features/{symbol}` + a Signal Breakdown panel on the symbol page (`live`/`shadow`/`unavailable`/`stale` states).
- **component_weights is always `{"technical": 1.0}`** — this PR changes zero live decision weight.

**Bugs caught and fixed during review, before merge:**
- `Decimal`/`float` `TypeError` in `weighted_component_score()` when called from the API (psycopg2 returns `NUMERIC` as `Decimal`, jsonb weights decode as `float`) — fixed by normalizing at the API boundary.
- `ORDER BY as_of DESC` with no tiebreaker on `feature_version` — could nondeterministically surface a stale snapshot once a future `feature_version` bump coexists with an older one at the same `as_of`. Fixed with `ORDER BY as_of DESC, feature_version DESC, id DESC`.
- `source_timestamps`/`component_weights` built via raw `%`-string interpolation instead of `json.dumps()` — fragile precedent, fixed.
- `git show main:...` failed on a genuinely fresh clone (only `origin/main` exists there, not a local `main` branch) — the fixture-equivalence test's baseline-resolution logic was fixed to try both.
- The disposable test Postgres was initially configured with `invest`/`investpass` — which turned out to be the **actual production DB credential pair**, read earlier in the session from `/home/salil/docker/invest/.env` on ubuntu-box. Caught and sanitized to `invest_test`/`not_a_real_credential` before anything was pushed to the public repo.

**Test count**: 19 passing (grew from 18 across the review-fix commit).

### PR #2 — Symbol Performance Summary
- `shared/round_trips.py` — average-cost reconstruction of per-symbol round trips from the **full** trades ledger (never a paginated slice). Handles pyramiding (multiple buys before a full exit), partial exits, oversell/pre-ledger-holding capping (never fabricates a matching entry), and per-exit-slice cost proration.
- `GET /api/symbol-performance/{symbol}` and `/round-trips` — reporting-only, no writes, no proposal/sizing/execution code touched.
- Symbol Performance Summary card + round-trip history table on the symbol page, visually distinguishing the open position from completed lifecycles.
- Explicit `methodology: "average_cost_reconstruction"` label everywhere in the response and UI — a deliberate, documented stand-in for the not-yet-built `position_lifecycles` work (§3).
- Structured `reconciliation` field (`ledger_status`/`broker_status`/`status`/`detail`) distinguishing `match`/`qty_mismatch`/`ledger_only`/`broker_only` between the local reconstruction and Alpaca's live position.
- `methodology_status: "complete"|"partial"` + `unmatched_sell_qty` — surfaces when a symbol's realized P&L is known to be incomplete (an oversell or pre-ledger holding couldn't be matched), rather than silently looking complete.
- Portfolio-wide `contribution_to_gross_gains_pct` / `contribution_to_net_pnl_pct`, kept as genuinely separate metrics (net contribution can exceed 100% or go negative), with the raw dollar numerator/denominator returned alongside each percentage so the UI can show its work.

**Bugs caught and fixed:**
- First cost-proration attempt charged 100% of an entry's commission as "realized" the instant a position opened, before any shares were sold — caught by a test assertion mismatch, fixed by prorating cost by actual shares sold so far.
- `node --check` on the extracted inline `<script>` caught a real naming collision: a new `fmtUSD()` helper duplicated one already defined earlier in `symbol.html` for the position card. Fixed by reusing the existing one.

**Test count**: 27 passing after the initial build, growing to match the review-fix round (16 → 27).

### PR #3 — SEC EDGAR fundamentals collector (shadow mode)
- `fundamental_facts` (raw, append-only, point-in-time — `accepted_at` uses the filing's own `filed` date, documented as a conservative simplification that can only make point-in-time filtering stricter, never introduce look-ahead bias).
- `shared/fundamentals.py::compute_fundamental_score()` — point-in-time scoring (revenue YoY growth, gross margin, net margin), explicitly documented as a first-cut threshold formula, not a calibrated model. Missing inputs are excluded from the average, never zero-filled.
- `ingest/fundamentals_collector.py` — SEC EDGAR `companyconcept` collector, daily cadence (same `app_settings`-gated pattern as the existing digest/postmortem schedules), per-symbol fail-isolated, inert (zero requests) without `SEC_USER_AGENT` set.
- `shared/feature_store.py::attach_fundamental_score()` — same fail-open pattern as PR #1's `attach_feature_snapshot()`.
- **Zero API/UI changes needed** — PR #1's existing `_component_status()` logic already renders a populated-but-zero-weight component as `"shadow"`, so `fundamental_score` shows up correctly on the existing symbol-page panel automatically.

**A real bug caught before it reached a build:** the scoring module (`shared/fundamentals.py`) and the collector were both about to be named `fundamentals.py`. `ingest/Dockerfile` does `COPY shared/ .` then `COPY ingest/ .` into the same flat `/app` directory — two files sharing that basename would have had the second `COPY` silently overwrite the first on disk, breaking `shared/signals.py`'s `from fundamentals import compute_fundamental_score` at container startup. Renamed the collector to `ingest/fundamentals_collector.py` before this was ever built or pushed.

**Test count**: 37 passing (18 new on top of PR #1's 19).

### Clean-clone / real-Postgres verification (performed for every PR, every push)
Every PR was verified, before pushing, against a genuine `git clone` into a fresh directory (not just the working tree) running the full test suite against a disposable `postgres:16-alpine` container (never the live production DB) — catching two real fragility bugs this way (a `git show main:...` failure on a ref that doesn't exist locally on a fresh clone; a schema-bootstrap ordering issue). Two PRs' key claims were also **mutation-tested**, not just passed: a deliberate scoring-logic mutation was confirmed to break the corresponding fixture-equivalence test before trusting that the passing (unmutated) version proves anything.

## 3. Unimplemented plans

Ordered roughly by how far along the design work is:

- **Signal-component roadmap remaining work** (from the original multi-source signal integration plan): PR #3 (news event intelligence), PR #4 (macro regime expansion), PR #5 (options features, Tier 2), PR #6 (evidence-based activation — the only PR in that sequence allowed to move any component's weight off zero). None started. Depends on PR #1 (and, for news, arguably nothing else new).
- **Fundamentals refinement**: PR #3 (this session, now open) is a real but deliberately first-cut pipeline. Sector-relative normalization, more XBRL metrics, and any real statistical calibration are explicitly deferred — documented in `docs/fundamentals-collector.md` as required before this component's weight ever leaves zero.
- **Position lifecycle and R-multiple foundation** ("Platform Improvement PR A" in this session's own numbering): a fully revised schema and lifecycle-matching algorithm spec was produced and approved in principle (see conversation history — `position_lifecycles`, `position_trades`, planned-vs-actual risk separation, pathwise MAE/MFE, deterministic FIFO matching rules including over-sell/pre-ledger/cross-thesis handling). **Not implemented.** This is the single most-depended-upon piece of unbuilt work — see §5.
- **Expectancy reporting** ("PR B"): extends `ingest/postmortem.py`'s existing bucket-stats machinery with R-normalized expectancy/profit-factor/sample-quality labeling. Designed at a high level only; not schema-specified in the same depth as PR A. Depends on PR A.
- **Rule-adherence reporting** ("PR C"): versioned rule-check records, advisory-only, separating financial outcome from process quality. Designed at a high level only. Soft-depends on PR A/B for full value but not schema-blocked by them.
- **Exit-policy research** ("PR D"): offline counterfactual stop/target simulator, point-in-time-correct, R-normalized. Designed at a high level only. Hard-depends on PR A (needs `planned_initial_stop_price`/actual MAE-MFE-in-R to have anything to simulate against).
- **Versioned launch-risk profiles and live-validation rollout** ("Launch PR 1–5"): a full phased plan was produced (`launch_risk_profiles`, `launch_risk_events`, operating-mode state machine `paper → live_validation → live_scaled`/`paused`, preflight gate ordering, account/credential cross-validation). **Not implemented, explicitly deferred until after Platform Improvements PR A lands** — the user set this sequencing explicitly mid-session. Three corrections to the original plan were also recorded but not yet written into a persisted design doc (see §6 — this is a documentation gap this handoff is flagging, not resolving).

## 4. Settled architectural decisions

These were explicitly discussed and agreed during the session and should be treated as constraints on any future work here, not just historical color:

- **The raw ledger (`trades`) stays append-only.** No lifecycle work rewrites or annotates individual trade rows beyond additive columns; `trades.position_id` was explicitly rejected in favor of a normalized `position_trades` join, specifically because one trade can legitimately be allocated across more than one lifecycle (cross-thesis edge case).
- **Analytical positions are derived lifecycles**, computed from the ledger after the fact by an idempotent builder — never hand-maintained, never mutating `trades` itself. This mirrors the `price_history → signals → signal_outcomes` derivation pattern already established in this codebase before this session.
- **Planned risk and actual (executed) risk are kept as distinct fields at every layer** — `planned_entry_price`/`planned_initial_stop_price`/`planned_risk_per_share`/`planned_risk_dollars` on the proposal vs. `initial_stop_price` (copied immutably from the approved proposal)/`actual_initial_risk_per_share`/`actual_initial_risk_dollars` (from actual fill price and actual qty) on the trade/lifecycle. This is what makes slippage and quantity overrides measurable instead of hidden.
- **Missing risk data is `NULL`, never zero**, at every layer this session touched — signal components, fundamental scores, planned/actual risk figures, capital-deployed figures. Treating "unknown" as "0" or "worst case" was identified and fixed as a real bug pattern more than once this session (see the fundamental-score growth-component test, the round-trips cost-proration bug).
- **Signal-level MAE/MFE and actual-position MAE/MFE are different concepts, not to be conflated.** The existing `signal_outcomes.mae`/`.mfe` (in `ingest/outcomes.py`) are hypothetical, fixed-20-day-window, computed for every signal whether or not it became a trade. The planned position-lifecycle MAE/MFE (§3) is real, pathwise, over the actual holding period, with time-varying qty and cost basis. Any future work reusing one where the other is meant would be a real bug.
- **PR #2's average-cost reconstruction is explicitly temporary** — labeled `methodology: "average_cost_reconstruction"` in every API response and in the UI, specifically so it's unmistakable once `position_lifecycles` (§3) becomes the canonical source. The response shape was deliberately kept close to what the lifecycle-based version will look like, so the eventual swap is a data-source change, not a UI rewrite.
- **Reporting/measurement PRs must not alter execution behavior.** Enforced concretely, not just as a stated principle, via frozen-fixture pre/post equivalence tests on every PR that touches `shared/signals.py`, each one mutation-tested to confirm it actually has teeth.
- **Default deployment remains paper trading.** No operating-mode concept exists in the deployed code yet; the only broker config is `ALPACA_BASE_URL`, defaulting to the paper endpoint. Nothing this session changed that.
- **Live-account identity failures must block all automated orders, full stop** — including exits. The user explicitly corrected an earlier draft of the launch-risk plan on this point: the system must not assume it's safe to submit even an exit order when it cannot verify which account it's connected to. Normal strategy exits, emergency liquidation, and broker-only manual intervention need to be distinguishable outcomes, not conflated into "the app tries to exit anyway." **This principle is settled; the mechanism to enforce it is unimplemented** (blocked on Launch PR 1+, itself blocked on Platform Improvements PR A per the user's explicit sequencing).

## 5. Dependencies and sequencing

**Hard dependencies** (schema/code, not just logical ordering):
- PR #3 (fundamentals) → **hard depends on PR #1** (uses its `symbol_features`/`signal_outcomes` columns and `shared/feature_store.py` directly). Already correctly stacked as a PR against PR #1's branch.
- Signal-integration PR #3 (news) / PR #4 (macro) → hard depend on PR #1, likely not on PR #3 (fundamentals) specifically, though all three would eventually feed the same `weighted_component_score()` combination step in signal-integration PR #6.
- Platform Improvements PR A (position lifecycles / R-multiples) → **no hard dependency on PR #1/#2/#3**, but its schema (`planned_initial_stop_price` etc.) is what Platform Improvements PR B/D and the entire Launch PR sequence need. Nothing currently open blocks starting it.
- Platform Improvements PR B (expectancy) → hard depends on PR A's schema.
- Platform Improvements PR D (exit-policy research) → hard depends on PR A's schema (needs real planned-stop and actual-MAE/MFE-in-R data to simulate against).
- Platform Improvements PR C (rule-adherence) → soft depends on PR A/B for full value, not schema-blocked.
- **Launch PR 1–5 → explicitly deferred by the user until Platform Improvements PR A is implemented**, specifically because the risk engine's per-trade risk calculation has no other source for planned-stop/actual-risk data. This is a user-set sequencing decision, not just a technical inference — do not start Launch PR 1 before PR A exists, even though Launch PR 1 itself (schema + read-only status, no execution change) has no *technical* schema dependency on PR A.

**Recommended merge order**: #1 → #3 (stacked) → #2 (independent, can merge anytime relative to the other two) → then Platform Improvements PR A → B/C/D in any order → then Launch PR 1–5.

**Must not be developed in parallel**: any future PR touching `shared/signals.py`'s `compute_signals()` main loop should not be developed concurrently with another PR touching the same function — both PR #1 and PR #3 modified the exact same call site (right after `_record_outcome()`), and a third concurrent change there would need careful merge-order awareness to avoid one PR's frozen-fixture equivalence test silently baselining against a state that doesn't match what actually merges first.

## 6. Open risks and unresolved decisions

Carried forward, unresolved, from this session's design discussions — none of these have a settled answer yet:

- **Same-symbol positions from multiple theses.** Alpaca has exactly one real position per symbol; there's no broker-side segregation by which thesis's intent opened it. The lifecycle-matching spec (§3) handles this by flagging both lifecycles with `data_quality_flags: 'concurrent_multi_thesis_symbol'` rather than resolving it — this is a known, accepted limitation, not a bug to fix later, but any reporting built on top of lifecycle data needs to handle (or explicitly exclude) flagged lifecycles.
- **Partial fills and position allocation.** The FIFO-allocation spec (§3) handles multi-entry-lot exits via multiple `position_trades` rows per exit trade, but this is design-only — never implemented or tested against real partial-fill sequences.
- **Intraday sequencing for MAE/MFE.** The pathwise MAE/MFE algorithm (§3) prefers `price_history_hourly` and falls back to daily bars with an `excursion_resolution` flag, but daily-bar fallback is explicitly documented as capable of overstating excursions on entry/exit days specifically (can't distinguish before-entry/after-exit movement from during-holding movement at daily granularity). Unresolved: whether this is acceptable for Launch PR 4's real-money risk calculations or needs `price_history_hourly` coverage guaranteed first.
- **Broker paper/live identity verification.** The user explicitly corrected the original plan's reliance on an Alpaca account-number prefix (`PA...`) as the *primary* paper/live detector — that's now only an additional warning signal, not the gate. The canonical check needs an explicitly configured expected environment + base URL + expected account ID + documented broker account metadata, but **which Alpaca API field(s) reliably distinguish paper/live accounts has not been verified against Alpaca's actual documentation** — flagged as an open question in the original plan, still open.
- **Fractional-share precision.** Alpaca's per-symbol fractional-share precision/`fractionable` flag was identified as needed for the sizing function's "round down where broker precision requires it" step, but nothing in the current codebase queries `/v2/assets/{symbol}` for this — not investigated further this session.
- **Authentication and approver identity.** Current auth is a single shared Basic Auth credential pair plus one read-only agent API key — there is no per-user identity. `decided_by='human'` in `trade_proposals` is a literal constant, not a real identity. The launch-risk plan's audit trail (`approved_by`, override attribution) has no real identity source to draw from yet — unresolved whether this needs a real auth upgrade before Launch PR 4, or whether a manually-configured single-operator name is acceptable for a solo homelab deployment.
- **Lifecycle reconstruction of pre-ledger holdings.** Both PR #2's average-cost reconstruction and the (unbuilt) lifecycle-matching spec explicitly refuse to fabricate a matching entry for a sell that exceeds locally-known history — correct per the "no speculative historical backfill" principle, but it means realized P&L and total-open-risk calculations are **provably incomplete** for any symbol with real trading history predating this session's tracking. `methodology_status: "partial"` (PR #2) surfaces this for reporting; the risk-engine plan (§3/Launch PR 2) says total open risk must be treated as *unknown* (blocking new `live_validation` entries) if any open position has unknown risk — but there's no automated way yet to detect "this symbol's history is provably incomplete" versus "this symbol's history is complete and small."

## 7. Operational notes

**Environment variables** (verified against `.env.example` on `main`, plus what PR #3 adds):
```
DATABASE_URL              # required, Postgres connection string
ALPACA_API_KEY             # required
ALPACA_API_SECRET          # required
ALPACA_BASE_URL             # default https://paper-api.alpaca.markets — DO NOT change to a live URL without the (unimplemented) launch-risk safeguards in place
INVEST_USER                 # default 'invest'
INVEST_PASS                  # required — API refuses to start without it
FINNHUB_API_KEY               # optional, news ingestion
SMTP_USER / SMTP_PASS / DIGEST_TO  # optional, email digests
ATQ_URL                       # optional, WhatsApp proxy
SEC_USER_AGENT                 # new in PR #3, optional — fundamentals sync is a no-op without it
```

**Credentials that should be rotated:** none discovered as actually leaked to the public repo — the near-miss (production `investpass` accidentally used as a *local test container's* password) was caught and fixed before any push. Worth a manual double-check that `investpass` (the real production DB password, read from `/home/salil/docker/invest/.env` on ubuntu-box during this session) hasn't ended up anywhere else written during this session — I did not write it to any file outside that one local `.env` read.

**Database migration/bootstrap assumptions** (pre-existing, confirmed multiple times this session, not fixed): `ingest/schema.sql` is **not** bootstrappable on a truly empty database — it assumes `trade_proposals`, `signal_params`, and `theses` already exist partway through the file, and none of the three have a `CREATE TABLE` anywhere in the tracked repo (`theses` only exists via the separately-tracked, never-auto-run `ingest/migrations/001_multi_thesis_architecture.sql`). This predates this session. Every test suite in every PR this session works around it with a test-only shim (`tests/fixture_schema.sql`) — that workaround is not a fix and does not belong in production `schema.sql`.

**Deployment and rollback precautions:** nothing from this session has been deployed. All three PRs are additive-only (new tables, new columns with `IF NOT EXISTS`, no destructive schema changes, no proposal-decision code path altered in PR #1/#3, no writes at all in PR #2) — rollback for any of them, if merged and then reverted, is `git revert` + rebuild, no schema rollback needed, consistent with this repo's established practice of never dropping a column once added.

## 8. Next-session bootstrap prompt

Paste this into a fresh session to resume:

```
You're picking up work on smaniktahla/homelab-trader (a paper-trading
research platform, currently deployed on ubuntu-box at 10.10.10.13). A
prior session left a detailed handoff at docs/session-handoff-2026-07-31.md
on main (merged via a docs-only PR) — read it in full before doing
anything else.

Do not trust any of the following as current fact without re-verifying
against GitHub and the working tree first, since state may have changed
since the handoff was written:

- Three draft PRs should exist: #1 (signal-component shadow infra, base
  main), #2 (Symbol Performance Summary, base main), #3 (SEC EDGAR
  fundamentals, STACKED on PR #1's branch, not main). Check `gh pr list`
  and confirm branch/base/mergeable/draft status for each before assuming
  any of them merged or changed.
- main's HEAD commit, and whether ubuntu-box's live deployment
  (/home/salil/docker/invest) still matches it, should both be re-checked
  — do not assume nothing has been merged/deployed since this handoff.

Architectural invariants established in the prior session (verify these
still hold in the code before building on them, don't just take this
list's word for it):
- trades ledger is append-only; analytical positions are DERIVED
  lifecycles, never hand-maintained or backfilled onto trades itself.
- Planned risk (at proposal time) and actual/executed risk (at fill time)
  are kept as separate fields, always — never conflated.
- Missing risk/score/data is always NULL, never coerced to zero.
- Signal-level MAE/MFE (existing, hypothetical, fixed 20-day window) and
  actual-position MAE/MFE (planned, pathwise, real holding period) are
  different concepts — do not reuse one where the other is needed.
- PR #2's average-cost reconstruction is an explicitly temporary stand-in
  for a not-yet-built position_lifecycles system — labeled as such
  everywhere; do not treat it as canonical once lifecycles exist.
- Reporting/measurement PRs must never change proposal/sizing/execution
  behavior -- prove it with a frozen-fixture pre/post equivalence test,
  and mutation-test that test before trusting it.
- Default deployment is paper trading; there is no live-trading mode in
  the deployed code. Do not wire in real-money execution.
- If any future live-trading work touches account/broker identity
  verification: a live-account mismatch must block ALL automated orders,
  including exits — never assume it's safe to auto-exit when the
  connected account can't be verified.

Immediate next task: [ASK THE USER — the handoff lists several
unstarted threads (Platform Improvements PR A / position lifecycles is
the most-depended-upon; signal-integration PR #3/#4 for news/macro; or
something the user has decided since]. Do not assume which one without
asking, since §5 of the handoff notes explicit user-set sequencing
(Launch PR work is deliberately blocked on Platform Improvements PR A
landing first) that a fresh session has no way to know unless it reads
the handoff.

Required inspection before writing any plan or code: re-read the actual
current schema (ingest/schema.sql plus whatever's live-only per the
handoff's §7 bootstrap-gap note), the actual current shared/signals.py
call flow, and the actual current test suite/count on whichever PR
branch you're extending — the handoff is a snapshot, not a substitute
for looking.
```

## 9. Final verification performed

Before writing this document: fetched `main`'s actual remote HEAD via `git ls-remote`, listed all remote branches, queried `gh pr list` for full state/mergeability/base/head on every open PR, and separately SSHed to ubuntu-box to confirm the live deployment's git state and running container status. Nothing in this document is deployed; nothing is merged; every commit SHA and PR status above was read directly from GitHub and the deployment host in this final step, not carried over from earlier turns' memory. No implementation work was performed after verification began.
