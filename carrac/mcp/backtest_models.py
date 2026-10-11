"""Canonical backtest-model name constants.

Carrac has three distinct backtest models (see the `BACKTEST MODELS` section
of `BASE_PROMPT` in `carrac/ai/features.py` — the single source of truth for
the vocabulary, also served verbatim as the `carrac://glossary/backtest-models`
MCP resource via `carrac/mcp/primitives.py:_res_backtest_models`).

Every backtest-shaped MCP/HTTP response must say which model produced it, so
a consumer never silently compares numbers that measure different things
(Hold-N hit rate vs. Full-Sim win rate, etc).

All three models are wired to MCP: Full Simulation (`carrac/backtest_sim.py`)
via the `backtest_simulate` tool, Hold-N via `backtest_run`
(absorbs backtest_indicator_formula/backtest_play/
scan_and_backtest via source=formula|play|scan), and Historical Analog via
`backtest_get_historical_analog`.
"""

MODEL_HOLD_N = 'Hold-N Signal Test'
MODEL_HISTORICAL_ANALOG = 'Historical Analog'
MODEL_FULL_SIMULATION = 'Full Simulation'
