# Peer Intelligence — Architecture Reconciliation

Design note, committed to the repo rather than pasted into a chat session —
same reasoning as
[`docs/trade-thesis-architecture-reconciliation.md`](trade-thesis-architecture-reconciliation.md):
this resolves how a newly-proposed "Peer Intelligence" roadmap relates to the
just-completed Hypothesis-Driven Trading Architecture epic (PR 1-12, all
merged to `main`), before any of this new work is written.
**No implementation in this PR — architecture only.**

**Sources:**
- Video: "I Found a Secret Quant Strategy That Made Morgan Stanley \$50
  Million. I Tested It." (Unbiased Trading), on Morgan Stanley's 1980s
  APT/pairs-trading desk under Nunzio Tataglia and a from-scratch backtest
  of the 1998 Yale pairs-trading paper's rules.
  <https://www.youtube.com/watch?v=A-4GMsLPxQc> — logged in DocMost as
  note `019fedbf-f070-769c-b4f7-b68cd79eddc6` ("[stock trading] I Found a
  Secret Quant Strategy That Made Morgan Stanley \$50 Million. I Tested
  It."), transcript/summary only.
- A follow-on discussion with ChatGPT applying that video's ideas to this
  repo's architecture (peer intelligence as a missing middle layer between
  sector and stock, evidence-lifecycle/signal-decay framing) — an external
  conversation with no durable link to cite; relayed inline into this
  session and reconciled against the actual codebase here.

## 0. The core decision: one architecture, not two

The peer-intelligence idea arrived shaped like a second roadmap (its own
numbered PRs, its own "signal decay" concept). It isn't one. The distinction
that resolves this: **producing evidence vs. interpreting evidence.**

```
Peer discovery -> peer-relative metrics -> divergence detection
                                                   |
                                                   v
                                              FEATURES / EVIDENCE
```

None of that needs to know whether "-2.1σ vs. peers" is bullish, bearish, or
noise. That's already the hypothesis engine's job, and it already exists:

```
Feature Registry (PR 2) -> evidence_context (PR 1 §1b) -> Trade Thesis (PR 1)
      -> validation (PR 3) -> invalidation (PR 5) -> re-evaluation (PR 10)
```

Peer intelligence is a new evidence *source* feeding an already-built
interpretation layer, not a parallel system. Concretely: new modules that
compute and persist peer relationships/metrics (mirroring
`shared/sector_regime.py`'s compute/store/load shape), registered as new
`FEATURES`/`PROVIDERS` entries in `shared/feature_registry.py` (PR 2,
already built to grow this way — see its own module docstring). No new
epic, no new schema family for "hypothesis" concepts, no new evaluator.

## 1. What this explicitly rejects

Classic statistical arbitrage (long one leg, short the other, dollar
neutrality, borrow costs, margin, locate availability, cointegration tests)
is a different trading platform. This repo is long-only swing trades on a
single-account paper/live pipeline (`shared/risk_engine.py`,
`shared/position_lifecycles.py` — long-only is a stated assumption, not an
oversight, per that module's own docstring). Peer divergence is adopted
purely as a **feature/evidence signal**, never as a standalone strategy or
a second leg of anything. This mirrors how `shared/relative_strength_risk.py`
already treats stock-vs-sector underperformance: a risk input, never a
trade in itself.

## 2. Naming collision: `relative_strength_risk.py` already exists

`shared/relative_strength_risk.py` is stock-vs-**sector**, a backtested
**risk gate** (rejects/resizes buy proposals — see its own docstring,
backed by `backtest_results` experiments 009-011), explicitly never a
scoring input. The new peer-relative-strength idea is stock-vs-its-**closest
correlated peers**, meant as a **scoring/evidence** input, not a risk gate.
Same general shape (a stock underperforming *something*), different axis,
different purpose, different consumer — close enough in name to invite
confusion later.

**Resolution: new, distinctly-named modules, existing one untouched.**

```
shared/peer_mapping.py    -- mirrors sector_mapping.py's role, but computed
                              (rolling correlation), not a static dict --
                              see §3 for why this can't just be a dict.
shared/peer_regime.py     -- mirrors sector_regime.py's compute/store/load
                              shape: classify_peer_regime() (pure) +
                              compute_peer_regime()/store_peer_regime_day()/
                              load_latest_peer_regime() (I/O).
```

`relative_strength_risk.py` is not touched, not extended, not renamed.

## 3. Why peer discovery can't be a static dict like `sector_mapping.py`

`sector_mapping.py` is a fixed `SECTOR_ETF_MAP` dict because GICS sector
membership barely changes. Peer relationships (rolling price correlation)
drift continuously — AMD's closest correlated peers today are not
necessarily its closest peers six months from now. This has to be
**computed and persisted per date**, not hardcoded.

```sql
-- Peer PR 1 schema sketch (illustrative -- final field list is Peer PR 1's job)
CREATE TABLE peer_relationships (
    trading_date     DATE NOT NULL,
    symbol           TEXT NOT NULL,
    peer_symbol      TEXT NOT NULL,
    correlation      NUMERIC NOT NULL,
    rank             INTEGER NOT NULL,   -- 1 = closest peer that day
    lookback_days    INTEGER NOT NULL,   -- correlation window used
    PRIMARY KEY (trading_date, symbol, peer_symbol)
);
CREATE INDEX idx_peer_relationships_symbol ON peer_relationships (symbol, trading_date DESC);
```

Refreshed on a slower cadence than daily regime classification (peer sets
don't need to be recomputed every cycle) — proposed monthly, matching the
1998 pairs-trading paper's own 12-month formation / periodic re-formation
convention the source video cites, though nothing here requires matching
the paper's specific numbers. Read back via `load_latest_peer_set(conn,
symbol)`, same "compute once elsewhere, read back here" split every other
regime module in this codebase already follows.

**Parameters (mine to propose, per your go-ahead — Peer PR 1's job to
finalize, not fixed here):**
- Correlation window: 120 trading days (~6 months) — long enough to
  filter noise, short enough to track real relationship drift, roughly
  splitting the difference between the paper's 252-day formation and
  `shared/relative_strength_risk.py`'s existing 20-day lookback.
- Peer count: top 5 by correlation — enough to compute a stable peer-basket
  average without diluting it toward the whole market.
- Minimum peer correlation to qualify at all: 0.5 — below this a "closest
  peer" is not meaningfully related, and the classification should degrade
  to `insufficient_data` (same convention every other regime module uses)
  rather than force a peer basket that isn't real.
- Correlation computed by a plain-Python Pearson-correlation helper
  (`shared/regime_common.py` or a new `peer_mapping.py`-local one) — this
  codebase has no numpy/scipy dependency anywhere (`ingest/requirements.txt`
  checked directly) and every existing statistical helper
  (`compute_bollinger`, `regime_common.sma`, etc.) is hand-rolled. No new
  dependency introduced for this.

## 4. Evidence lifecycle: staleness folds into the EXISTING invalidation
model, not a new decay framework

ChatGPT's "signal half-life" / "confidence decay" idea and the reframe
that evidence itself has a validity lifecycle both point at the same gap
— and it's a gap that already has a stub waiting for it. Quoting
`shared/trade_thesis_reevaluation.py`'s own docstring, written when PR 10
shipped:

> `'weakening' is part of trade_thesis.STATUSES (PR 1) but has no defined
> trigger anywhere in this epic — not produced by this PR, flagged rather
> than invented.`

Evidence staleness is that trigger. **No new table, no `decay_class`
concept, no half-life field on `trade_proposals`.** Instead:

- `evidence_context.providers[*]` (PR 1 §1b) already carries an implicit
  `as_of`/version per provider — the freshness signal already exists in
  the schema, just unused for this purpose.
- Extend `shared/trade_thesis_invalidation.py::evaluate_thesis_invalidation()`
  (PR 5) with a fourth signal source: a provider whose evidence has aged
  past some per-provider staleness threshold contributes a new
  `evidence_stale:<provider>` reason — same `InvalidationResult.reasons`
  list every other signal source already appends to, same three-valued
  "unknown, never guessed" discipline the module's docstring establishes.
- Extend `shared/trade_thesis_reevaluation.py::_next_state()` (PR 10) with
  a fourth branch: staleness-only (no contradicting invalidation signal,
  no success) transitions `active` -> `weakening` rather than leaving the
  thesis at `active` indefinitely. `weakening` is not terminal
  (`_TERMINAL_STATES` unchanged) — a thesis can recover to `active` if
  fresh evidence arrives before it's ever actually invalidated.
- Per-provider staleness thresholds belong in `feature_registry.py`'s
  `ProviderSpec` (PR 2) — e.g. `technical` (daily bars) stales faster than
  `market_regime` (multi-day regime shifts) — a natural field addition to
  an existing dataclass, not a new concept.

This also directly answers the structure-aware-stop connection raised in
discussion: a thesis should not necessarily die because an arbitrary
percentage stop was touched (that's `stop_loss`, the blunt safety net,
`shared/signals.py::check_stop_losses()`, unchanged) — it dies because the
evidence supporting it stopped being true. That is already exactly what
`exit_reason='thesis_invalidated'` (PR 8) represents; staleness becomes
one more way to arrive there, or at the softer `weakening` waypoint before
it.

## 5. Sequencing

```
TRACK A -- can start now, no dependency on the epic (already complete)
────────────────────────────────────────────────────────────────────────
Peer PR 1   shared/peer_mapping.py -- rolling-correlation peer discovery,
            peer_relationships table (§3)
    |
Peer PR 2   shared/peer_regime.py -- peer-relative strength
            (classify_peer_regime, mirrors sector_regime.py's shape)
    |
Peer PR 3   Peer divergence (σ-distance from peer-basket mean) -- same
            module as Peer PR 2, the actual feature the source video's
            strategy keyed off, exposed as evidence only (§1)

Backtest PR (point-in-time universe) -- PARKED, see §6. Not sequenced
until the open data-source question is resolved.


TRACK B -- already done, nothing further needed first
────────────────────────────────────────────────────────────────────────
Hypothesis-Driven Trading Architecture epic, PR 1-12 (merged to main)


INTEGRATION -- depends on completed Track A + existing Track B
────────────────────────────────────────────────────────────────────────
Integration PR 1   Register peer_regime/peer.divergence_sigma/
                    peer.relative_strength_pct in feature_registry.py (PR 2
                    extension) + evidence_context legal provider keys (§1b)
    |
Integration PR 2   Evidence staleness -> invalidation/'weakening' (§4:
                    extends PR 5 + PR 10, the actual "signal decay" idea,
                    generalized and owned by the existing invalidation
                    model instead of a parallel framework)
    |
Integration PR 3   New hypothesis_type(s) (e.g. 'peer_divergence_reversion')
                    in shared/trade_thesis.py::HYPOTHESIS_TYPES (PR 1
                    extension) + peer-aware entry_conditions/invalidation_spec
                    once Track A evidence is real and registered
    |
Integration PR 4   Peer-aware scoring: peer divergence as one more input to
                    shared/signals.py::score_signal(), same "layered score
                    modifier" precedent as regime_scoring.py/
                    structure_scoring.py -- disabled-by-default flag,
                    same staged-rollout discipline every PR in the epic
                    already used
```

Original epic Phase 4 (hypothesis library, strategy generation, promotion
workflow — PR 13-15 in the epic's own numbering) is unaffected by this and
resumes after Integration, if/when prioritized.

## 6. Explicitly deferred, not decided here

- **Backtest integrity / point-in-time universe (survivorship-bias-safe
  backtesting).** Checked directly: `universe` (`ingest/schema.sql`) is a
  plain current-symbol table (`symbol, name, exchange, sector, scannable`)
  — no delisted-symbol tracking, no point-in-time historical constituent
  data anywhere in this codebase today. This is a **new data source
  question, not an engineering task** — every existing backtest
  (`ingest/research/backtests/*.py`) already implicitly survivorship-biased
  against today's live universe. Real fix requires sourcing historical
  constituent/delisted data (a vendor, or a specific free dataset) before
  any "Backtest PR" can be scoped concretely. Not blocking Track A (peer
  discovery/regime run forward-looking against the live universe, same as
  every other regime module already does) — only blocking a rigorous
  historical backtest of peer-divergence strategies specifically.
- **Whether peer-divergence *closing* (the pairs reverting to their normal
  relationship) should read as thesis `completed` (the premise played out)
  or a distinct exit concept.** Likely `completed` via `success_spec`,
  consistent with how `thesis_complete` already works for the existing
  mean-reversion-to-SMA20 case (PR 4's `_THESIS_COMPLETE_BB_PCT_B`
  precedent) — Integration PR 3's job to confirm.
- **Exact correlation window / peer count / staleness thresholds** — §3/§4
  propose defaults; Peer PR 1 and Integration PR 2 are where these get
  finalized against real data, not fixed by this doc.

No code changes accompany this doc. Next step, on your go-ahead: scope
Peer PR 1 concretely (mirroring how PR 1 of the hypothesis epic got a
dedicated §7 scope section before implementation started).
