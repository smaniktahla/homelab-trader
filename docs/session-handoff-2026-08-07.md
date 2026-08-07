# Session handoff — 2026-08-07

Continuation of `docs/session-handoff-2026-08-05.md`. This session ran
2026-08-05 evening through 2026-08-07: first a small production fix, then
the entire Market Structure Engine build, then a four-experiment research
chain (each one testing/reviewing the previous), ending in one real
production feature and a dashboard reorg. All facts below re-verified
against GitHub and the live deployment (ubuntu-box, 10.10.10.13)
immediately before writing this.

## 1. Repository state

**`main`**: `5c3dbc5` ("Reconcile proposal UI: separate Decision Factors
from Market Context (#48)"). This is exactly what's deployed on
ubuntu-box (`invest-api`/`invest-ingest`, rebuilt and restarted after
every merge this session).

**No open PRs.** Eleven PRs this session (#38–#48), all merged, none
abandoned. In merge order:

| # | Title | Notes |
|---|---|---|
| 38 | Fix Alpaca backfill batch-poisoning + drop bogus VIX universe entry | Alpaca rejects hyphenated share-class tickers (BF-B/BRK-B) and fails the WHOLE batch, not just the bad symbol — silently starved BDX/BEN/BG/BIIB/BKNG/BKR/BLDR and BLK/BMY/BNY/BR/BRO/BSX/BX of pre-2025 history. Fixed with dot-notation translation + per-symbol batch-failure fallback. Backfill re-run live: 27,709 new rows. |
| 39 | Market Structure Engine core | Pure swing/trend/BOS/CHoCH classification, Monthly/Weekly/Daily only (no 4H/1H — not enough intraday history). No DB, no wiring. |
| 40 | Market Structure Engine PR 2: persistence | `market_structure_history` table, `compute_market_structure()`, wired into `ingest.py` per-cycle, `trade_proposals.market_structure_snapshot` — purely additive, no score/gate impact. |
| 41 | Market Structure Engine PR 3: dashboard + scoring | `.structure-badge` UI, `shared/structure_scoring.py` — new adjustment mechanism, **disabled by default** (`structure_scoring_enabled=0`), same precedent as `regime_scoring_enabled`. |
| 42 | Experiment 008: Market Structure significance test | Trend/CHoCH/BOS vs. mean excess return: **none significant** (p=0.10/0.42/0.97). |
| 43 | Experiment 009: risk-separation test | Same structure groupings, but risk/dispersion metrics instead of return. First version, structure-only. |
| 44 | Extend Experiment 009 with hierarchy groupings + worst-loss metric | Added market×sector alignment and stock-vs-sector relative strength groupings to the same run (before its first execution). |
| 45 | Experiment 010: portfolio-level relative-strength replay | 3 variants (baseline/gate/reduce_size_50) on paired MC windows — gate variant: lower stop-out rate (1.2% vs 5.3%), exposure-adjusted return actually *higher* than baseline. |
| 46 | Experiment 011: paired significance + definition sensitivity | Paired sign-flip test on Exp 010's data + 3 fresh definitions. **Stop-out rate and max drawdown replicated robustly; Sharpe/Sortino/return significance from Exp 010 did NOT replicate on a fresh sample.** |
| 47 | Relative-strength risk eligibility filter (production) | `shared/relative_strength_risk.py`, `relative_strength_risk_mode` (0=off/1=gate/2=size_reduce-reserved), **disabled by default**. First production feature this repo's research pipeline drove end to end. |
| 48 | Proposal UI: Decision Factors vs Market Context | Pure presentation change — score_signal's rationale becomes bulleted Decision Factors; regime/structure badges move to a de-emphasized "Market Context (research)" section. |

**Test count**: 301 passing (up from 244 at session start — some sessions
in between weren't captured in the 08-05 handoff's own count of 241).
Every PR verified against a fresh disposable `postgres:16-alpine` before
merge.

**No uncommitted work anywhere** — confirmed clean on both AI2 and
ubuntu-box, and matching `origin/main`, immediately before writing this.

**Stale merged branches still on GitHub** (squash-merged, safe to
delete, low priority — attempted once this session, blocked by a GitHub
repository ruleset on branch deletion via the API, not a permissions
issue on my end; user said fine to clean up manually later): 13 branches
from PRs #38–#48, plus older ones already flagged in the 08-05 handoff.

## 2. The research chain, in one place (Experiments 007→011)

This is the most important throughline this session — worth reading as
one story, not five isolated PRs:

1. **Exp 007** (prior session, 08-05): hierarchy regime (market×sector
   alignment + stock-vs-sector relative strength) — no significant mean-
   return edge. `regime_scoring_enabled` stays off.
2. **Exp 008** (this session): Market Structure Engine (trend/CHoCH/BOS)
   — same question, same answer: no significant mean-return edge.
   `structure_scoring_enabled` stays off.
3. **Exp 009**: reframed the question — not "does it predict return" but
   "does it predict *risk*" (stop-out rate, MAE, downside deviation,
   tail percentiles, worst-loss). Tested on the same episode pool, both
   the structure groupings AND (after a same-day extension, before first
   execution) the hierarchy groupings. **Finding: stock-vs-sector
   relative strength shows a real, notable-effect-size, p<0.001 downside-
   risk reduction** (stop-out rate -6.1pp, 10th-percentile return
   +4.2pp) that Exp 007/008's return-only tests couldn't have found.
4. **Exp 010**: does that episode-level finding survive at the
   *portfolio* level (position sizing, sector caps, correlated-episode
   overlap can wash out a clean symbol-level effect)? Three variants —
   baseline/gate/reduce_size_50 — on 40 paired Monte Carlo windows.
   Gate variant: stop-out rate 1.2% vs baseline 5.3%, lower drawdown,
   and — after exposure-matching so the answer can't just be "it held
   more cash" — exposure-adjusted return was *higher* for gate, not
   lower.
5. **Exp 011**: is that portfolio-level result itself real, or a handful
   of favorable windows (the exact concern the user raised before this
   experiment was built)? Paired sign-flip permutation test (correct
   null for paired data, not the independent-episode shuffle 007-009
   use) on Exp 010's own 40 windows, PLUS three fresh, differently-
   defined replications (default/short-lookback/strict-threshold) on a
   newly seeded window pool. **Result: stop-out rate and max drawdown
   reductions replicated significantly in 3 of 4 conditions (the 4th,
   strict-threshold, just rarely fires — a power problem, not a
   contradiction). Sharpe/Sortino/exposure-adjusted-return significance
   from Exp 010 did NOT replicate on the fresh sample — one alternate
   definition even showed a significant *negative* total-return effect.**

**Conclusion that actually shipped (PR #47)**: relative-strength risk
filtering is real evidence for *risk reduction* (stop-out rate, drawdown)
and NOT demonstrated evidence for *return improvement*. Implemented
exactly on that distinction: `evaluate_relative_strength_risk()` is a
hard reject-or-pass eligibility gate (matches "underperformers are worse
trades"), never a score bonus (would have implied "outperformers deserve
extra conviction," which the data does not support). Both
`regime_scoring_enabled` and `structure_scoring_enabled` remain off
indefinitely unless they produce their own positive evidence — the
research chain's own conclusion, not a default nobody revisited.

**Backtest infra note**: `backtest_portfolio_montecarlo.py` (Experiment
003, prior session) gained two rounds of small *additive* hooks
(`rs_policy`/`sector_series` in PR #45, `rs_classify_fn` in PR #46) so
Experiments 010/011 could reuse its ~400-line gate/exit/execution
pipeline exactly rather than forking it. Both hooks default to `None`,
reproducing Experiment 003's original behavior byte-for-byte when unset
— confirmed no regression to Exp 003's own historical results.

## 3. Architectural invariants (carried forward + new)

Carried forward from 08-03/08-04/08-05, still binding: `trades` append-
only, `position_lifecycles` always derived; missing risk/score/data is
`NULL` never coerced to 0; default deployment is paper trading, no live-
money code path; DB-access code sets its own `cursor_factory` explicitly;
`shared/risk_engine.py::evaluate_proposal()` is the one authoritative
`approved_quantity` source; every buy submits as an Alpaca OTO order with
a resting stop.

New this session:
- **Market Structure Engine**: Monthly/Weekly/Daily only. 4H/1H
  deliberately unbuilt — `price_history_hourly` still only has a few
  weeks of history for a subset of symbols; `ingest/
  backfill_intraday_alpaca.py` still hasn't been run (flagged in 08-05's
  handoff too, still true).
- **Both new score-adjustment flags stay off**: `structure_scoring_enabled`
  (Market Structure trend/BOS/CHoCH) and the pre-existing
  `regime_scoring_enabled` (hierarchy regime). Both tested via the
  research chain above; neither showed a significant mean-return effect,
  and — new this session — regime "aligned" episodes actually showed
  *worse* risk (higher stop-out, worse MAE) than misaligned ones in
  Experiment 009's risk-separation test. If either is ever reconsidered,
  it needs its own new positive evidence, not just "seems reasonable."
- **`relative_strength_risk_mode` (new, default 0/off)**: 0=off, 1=gate
  (reject `underperforming_sector` buys outright), 2=size_reduce
  (reserved enum value, NOT implemented — a documented no-op if set).
  Risk-control only, never touches score/final_score — architecturally
  separate from `regime_scoring.py`/`structure_scoring.py`'s adjustment
  pattern on purpose. Reuses `shared/security_regime.py::
  classify_security_regime` directly (a test asserts literal function
  identity between the live and backtest import paths, not just
  behavioral equivalence).
- **Dashboard proposal cards now distinguish Decision Factors (RSI/BB/
  ATR/counter-trend-penalty — what actually produced the score today)
  from Market Context (hierarchy regime + Market Structure badges —
  research signals, visually de-emphasized, labeled as not wired into
  scoring by default).** Pure presentation, no backend change.

## 4. Open risks and unresolved decisions

Carried forward, still unresolved: same-symbol positions from multiple
theses; partial-fill allocation across lifecycles; intraday MAE/MFE
sequencing; broker paper/live identity verification; capital-graduation
plan (never written to a doc); Strategy Creation UI; Options/earnings-
scanner work (shelved, ADR 0001); `alloc_modifier`'s dual score+size
regime penalty (flagged repeatedly, still not fixed, still low priority);
`rule_adherence.py`'s now-fully-redundant position-sizing checks
(cosmetic cleanup, no correctness risk).

New this session:
- **`relative_strength_risk_mode` has never been enabled, even in paper
  trading.** Per the research's own conclusion (and the PR's explicit
  acceptance criteria), the next real step is a controlled paper-trading
  window with `relative_strength_risk_mode=1`, judged on stop-out rate /
  max drawdown as the primary success criteria — explicitly NOT on
  return/Sharpe improvement. Not started.
- **Sector-clustering research question, newly documented** (`docs/
  research-todo-sector-clustering.md`): a live screenshot showed 7
  simultaneous utility-sector BUY proposals. Open question: does
  approving multiple same-sector proposals concurrently reduce portfolio
  quality, holding the existing sector-cap dollar exposure constant? A
  concrete follow-up experiment (basket vs. strongest-only) is sketched
  in that doc, reusing the same portfolio-level harness. Not started.
- **`relative_strength_risk`'s reserved `size_reduce` mode (enum value 2)
  is unimplemented.** Motivated by Experiment 011's finding that the
  short-lookback definition showed a significant *negative* total-return
  effect under gate mode — i.e. outright rejection can remove real
  profitable opportunity, and a softer resize might do better. Not
  backtested. The enum slot exists so a future PR doesn't need another
  schema/wiring change, but implementing it requires its own Experiment-
  010/011-style validation first, not just flipping the mode.
- **Dashboard UI change (PR #48) was not visually verified in a real
  browser this session** — `claude-in-chrome` hit a Chrome HTTPS-auto-
  upgrade quirk against the plain-HTTP-only dashboard port (server itself
  confirmed healthy via `curl`, and the new JS/CSS confirmed present in
  the served template via `curl | grep`). Worth an actual visual check
  next session, or whenever a human is next looking at the dashboard.
- **Stale merged branches** (13 total from this session, more from
  before) still sitting on GitHub — a repository ruleset blocks deletion
  via the API; needs to be done from the GitHub UI by a human, or the
  ruleset relaxed first.

## 5. Operational notes

Environment variables: unchanged, no new ones. All new config
(`structure_scoring_*`, `relative_strength_risk_*`) is DB-seeded via
`schema.sql`, not env vars.

**Deployment state, verified directly**: `invest-api`/`invest-ingest` on
ubuntu-box both rebuilt and restarted after every merge this session,
confirmed clean logs each time. Schema changes confirmed applied live
(`market_structure_history` table exists, `trade_proposals` has the new
`market_structure_snapshot`/`structure_*`/`relative_strength_risk`-related
columns, all new `signal_params` rows present with correct off-by-default
values — spot-checked via direct psycopg2 queries against
`10.10.10.201:5432`, not just assumed from the migration having run).

**DB access pattern reconfirmed**: no `psql` binary or postgres
container on either ubuntu-box or truenas — the real DB is bare TCP
Postgres on truenas (`10.10.10.201:5432`, `invest`/`investpass`/`invest`,
credentials from `docker inspect invest-api`'s env). Query directly with
Python's `psycopg2` from AI2 — no SSH/docker-exec needed for reads or
writes against the live DB. `docker exec invest-ingest python3 research/
backtests/<script>.py` is still how one-off research scripts run
(against the real production data, safe/read-mostly except the final
`save_backtest_result` insert).

**Research script iteration pattern used this session**: for anything
beyond a trivial script, `docker cp` the file directly into the running
`invest-ingest` container for a fast smoke test (reduced `MC_RUNS`/
`HORIZON_DAYS` via env override) *before* committing — catches real bugs
in seconds instead of after a 10–40 minute full-scale run. Full commit/
PR/merge/rebuild/redeploy only after the smoke test comes back clean.

**Every PR this session** verified via `pytest tests/ -q` against a
fresh disposable `postgres:16-alpine` before pushing; full suite is
301/301 as of `5c3dbc5`.

**Nothing this session touched infrastructure/manifest-level facts** — no
new hosts, ports, services, or credentials. The homelab infrastructure
manifest does not need updating from this session's work.

## 6. Next-session bootstrap prompt

Paste this into a fresh session to resume:

```
You're picking up work on smaniktahla/homelab-trader (a paper-trading
research platform, currently deployed on ubuntu-box at 10.10.10.13).
Prior handoffs exist back through docs/session-handoff-2026-07-31.md;
docs/session-handoff-2026-08-07.md (this one) is the most current and
supersedes everything earlier on any conflict.

Do not trust any of the following without re-verifying against GitHub
and the working tree first:

- main should be at 5c3dbc5 or later, no open PRs. Check `git log main`
  and the GitHub API (public repo, unauthenticated pulls?state=open
  works) before assuming anything below is still true.
- ubuntu-box's live deployment should match main exactly.
- Read docs/session-handoff-2026-08-07.md section 2 (the Experiments
  007-011 research chain) before touching anything regime/structure/
  relative-strength related -- it's the load-bearing context for why
  the current feature set looks the way it does, and re-deriving it from
  scratch risks repeating already-answered questions.

Architectural invariants (verify before building on them):
- shared/risk_engine.py::evaluate_proposal() is the ONE authoritative
  source for approved_quantity.
- regime_scoring_enabled AND structure_scoring_enabled are BOTH 0 (off).
  Neither showed a significant mean-return effect across Experiments
  007/008, and hierarchy "aligned" episodes showed WORSE risk in
  Experiment 009. Don't enable either without new positive evidence.
- relative_strength_risk_mode is 0 (off) -- the one feature this
  session's research chain actually validated (stop-out rate / max
  drawdown reduction, NOT a return/Sharpe improvement -- don't claim the
  latter anywhere). Next real step: a controlled paper-trading window
  with mode=1 (gate), judged on stop-out rate/max drawdown only. Not
  started yet -- ask the user before flipping it, this is a real (if
  paper-money) behavior change.
- Every buy submits as an Alpaca OTO order with a resting stop_loss leg.
- Missing risk/score/data is always NULL, never coerced to zero.
- Default deployment is paper trading; no live-trading code path exists.

Immediate open items (none started, in rough priority order):
1. Enable relative_strength_risk_mode=1 in a controlled paper-trading
   window (needs explicit user go-ahead -- it's a real behavior change,
   even in paper trading) and monitor stop-out rate / max drawdown.
2. Visually verify PR #48's dashboard changes in a real browser (hit a
   claude-in-chrome HTTPS-upgrade quirk last session; server itself is
   confirmed healthy).
3. docs/research-todo-sector-clustering.md's open question (basket vs.
   strongest-only same-sector proposals) -- not started, experiment
   sketch already written.
4. Stale branch cleanup on GitHub (blocked by a repo ruleset via API,
   needs a human via the UI).

Required inspection before writing any new plan or code: re-read
shared/relative_strength_risk.py and shared/signals.py's buy-gate
pipeline (both current-final-state, not the individual PR diffs) before
touching either -- and re-read docs/session-handoff-2026-08-07.md section
2 in full before proposing any new "does X predict Y" research question,
since the exact same question may already have been asked and answered
this session.
```
