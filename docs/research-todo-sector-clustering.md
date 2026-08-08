# Research TODO: same-sector proposal clustering

Observed live (2026-08-07): a single proposal batch contained BUY
proposals for XLU, LNT, WEC, AEP, DTE, CMS, and NI simultaneously --
seven utility/utility-adjacent names in one list. `mean_reversion` scores
each symbol independently; nothing in the current pipeline treats
"several qualifying symbols in the same sector on the same day" as a
signal in its own right. The existing `sector_cap_block_reason` gate
limits total *dollar exposure* per sector, but does not distinguish "one
strong utility signal" from "the whole sector is oversold together and
every name is triggering for the same underlying reason."

This looks like a sector-wide mean-reversion event, not several
independent opportunities -- worth investigating whether it's diluting
portfolio quality (near-duplicate bets dressed as diversification) or is
actually fine (sector cap already bounds the dollar risk, and buying the
whole basket may just be the correct expression of "this sector is
oversold").

## Open question

Does approving multiple simultaneous same-sector proposals reduce
portfolio quality relative to a stricter policy, holding total sector
dollar exposure constant (the existing cap)?

## Possible future experiment

Same episode-level/portfolio-level backtest harness already built for
Experiments 007-011 (`ingest/research/backtests/`): compare

- **basket**: buy every qualifying symbol in a sector, up to the existing
  sector cap (today's actual behavior).
- **strongest-only**: on any day multiple symbols in the same sector
  qualify, buy only the highest-scoring one, skip the rest.

Reuse the same portfolio-level Monte Carlo pipeline
(`backtest_portfolio_montecarlo.py`) and the same paired-significance
methodology (`backtest_portfolio_relative_strength_significance.py`'s
Phase A/B pattern) rather than building a third parallel harness.

This is a portfolio-construction question, not a signal-generation
question -- distinct from the relative-strength risk filter
(`shared/relative_strength_risk.py`), which is about whether to buy a
*specific* symbol at all, not about concurrency across a sector.

**Update (2026-08-07)**: the *presentation* half of this shipped --
`shared/proposal_ranking.py` now clusters same-sector buy proposals in
the dashboard (best candidate expanded, alternatives collapsed under
"N more in <Sector>"), assigns each a 1-5 priority tier, and shows an
opportunity-cost note on lower-ranked cluster members. This is a
read-time ranking/labeling layer only -- it does not change which
proposals get generated, does not block/reject anything (approve/reject
is still 100% manual), and does NOT answer the open question above. It
still reflects today's actual behavior: a human can still approve every
member of a cluster if they choose to. The backtest comparing basket vs.
strongest-only policies (below) remains open and unstarted -- shipping
the UI does not constitute evidence either way.

Not started. No code changes implied by this note beyond the presentation
layer already shipped.
