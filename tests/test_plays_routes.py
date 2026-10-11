"""Unit tests for the play backtest helper shared by the Flask GET route and
the FastAPI POST route."""


class TestRunPlayBacktestModelField:
    """The /api/plays/<id>/backtest evidence dict is built by
    hand-picking fields off the backtest_indicator_formula result (see
    carrac/plays/routes.py:_run_play_backtest) — a re-wrap, not a passthrough.
    It must not silently strip the 'model' field."""

    def test_passes_model_through(self, monkeypatch):
        from carrac.plays.routes import _run_play_backtest
        from carrac.mcp.tools import indicators as ind_mod
        from carrac.mcp.backtest_models import MODEL_HOLD_N

        def fake_bt(args):
            return {'model': MODEL_HOLD_N, 'trades': 4, 'hit_rate': 0.75,
                    'mean_return': 0.02, 'median_return': 0.015, 'sharpe_like': 1.2,
                    'max_drawdown': -0.03, 'final_equity_multiple': 1.08,
                    'lookback_bars': 365, 'hold_bars': 5}

        monkeypatch.setattr(ind_mod, 'backtest_indicator_formula', fake_bt)

        play = {
            'id': 1, 'primary_asset': 'BTC', 'timeframe': '1D',
            'conditions': {'logic': 'all', 'rules': [
                {'type': 'price_level', 'params': {'level': 50000, 'relation': 'above'}},
            ]},
        }
        out = _run_play_backtest(play, 365, 5)

        assert out['model'] == MODEL_HOLD_N
        assert out['trades'] == 4
