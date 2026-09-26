# Macro Regime Overlay Research
# This module contains exploratory analysis of macro regime predictiveness
# for homelab-trader strategies.
#
# Keep separate from production code:
# - Reads from signal_outcomes, market_regime_history
# - Does not modify live proposal scoring, position sizing, or entry/exit logic
# - Macro features staged in research/ only, not integrated into ingest.py yet
