"""Parallelize the per-symbol precompute loop with a small
thread pool.

These tests exercise the orchestration added to ``_compute_assets_response``
(symbol order preservation, facts-counter accumulation, per-symbol failure
isolation) across ``PRECOMPUTE_WORKERS=1`` (serial, no executor at all) vs
``PRECOMPUTE_WORKERS=3`` (ThreadPoolExecutor) — using a fully mocked
ServiceContainer + synthetic OHLCV data. No network/DB access.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

import carrac.precompute as precompute


# ---------------------------------------------------------------------------
# Synthetic data + fake services (deterministic, no network/DB)
# ---------------------------------------------------------------------------

def _make_ohlcv(n: int = 300, seed: int = 7) -> pd.DataFrame:
    """A well-formed synthetic daily OHLCV DataFrame, long enough to satisfy
    every ``len(df) >= N`` guard in the per-symbol pipeline (VP >=30, ATR
    >=15, key levels >=5, CPR trend >=60, MACD >=26, engulfing >=22, ...).
    """
    rng = np.random.default_rng(seed)
    closes = 200.0 + np.cumsum(rng.standard_normal(n))
    closes = np.abs(closes) + 100.0
    return pd.DataFrame(
        {
            "open": closes * 0.995,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": rng.uniform(1000, 5000, n),
            "taker_buy_base": rng.uniform(500, 2500, n),
        },
        index=pd.date_range("2024-01-01", periods=n, freq="D"),
    )


class _FakeDB:
    """Stands in for ``svc.db`` — deterministic, in-memory, and lets one or
    more symbols be configured to raise (simulating a real per-symbol
    failure) without touching any real data source.
    """

    def __init__(self, symbols, df, bad_symbols=()):
        self._symbols = list(symbols)
        self._df = df
        self._bad_symbols = set(bad_symbols)

    def get_all_symbols(self):
        return list(self._symbols)

    def get_combined_crypto_prices(self):
        return {}

    def load_previous_snapshot(self):
        return {}

    def should_save_snapshot(self):
        return False

    def get_asset_info(self, symbol):
        if symbol in self._bad_symbols:
            raise RuntimeError(f"synthetic failure for {symbol}")
        return {"type": "crypto", "name": symbol, "category": "Major"}

    def get_multi_timeframe_data_with_realtime(self, symbol, rt_price):
        return {"1D": self._df, "3D": self._df, "1W": self._df}

    def get_multi_timeframe_data(self, symbol):
        return {"1D": self._df, "3D": self._df, "1W": self._df}

    def get_ltf_multi_timeframe_data(self, symbol):
        return {"1h": self._df, "2h": self._df, "4h": self._df}

    def calculate_volume_metrics(self, df):
        return {"volume_ratio": 1.0}

    def get_funding_rate(self, symbol):
        return None

    def get_perp_basis(self, symbol):
        return None


class _FakeConsensio:
    def calculate_multi_timeframe(self, d1, d3, d1w):
        return {
            "1D": {"signal": "neutral"},
            "3D": {"signal": "neutral"},
            "1W": {"signal": "neutral"},
            "total": {"positions": 0, "max_positions": 5},
        }


class _FakeRSI:
    def calculate(self, df):
        return 55.0


class _FakeEmaRibbon:
    def calculate(self, df):
        return {"trend": "bull"}


class _FakeDivergence:
    def detect(self, df):
        return None


class _FakeADX:
    def calculate(self, df):
        return {"adx": 22.0}


class _FakeCPR:
    def calculate_monthly_cpr(self, df):
        return {"position": "inside", "pivot": 100.0}

    def calculate_weekly_cpr(self, df):
        return {"position": "inside", "pivot": 100.0}

    def calculate_daily_cpr(self, df):
        return {"position": "inside", "pivot": 100.0}


class _FakeScorer:
    def calculate_score(self, **kwargs):
        return {"score": 50}


class _FakeStructure:
    def analyze(self, df):
        return {}


class _FakeBB:
    def calculate(self, df):
        last = float(df["close"].iloc[-1])
        return {"upper": last * 1.05, "lower": last * 0.95}


class _FakeTrendAnalyzer:
    def analyze(self, asset_result, prev_data):
        return {}


class _FakeServices:
    def __init__(self, db):
        self.db = db
        self.consensio = _FakeConsensio()
        self.rsi_calc = _FakeRSI()
        self.ema_ribbon_calc = _FakeEmaRibbon()
        self.trend_analyzer = _FakeTrendAnalyzer()
        self.divergence_detector_daily = _FakeDivergence()
        self.divergence_detector_weekly = _FakeDivergence()
        self.adx_calc = _FakeADX()
        self.cpr_calc = _FakeCPR()
        self.opportunity_scorer = _FakeScorer()
        self.research_scorer = _FakeScorer()
        self.user_scorer = _FakeScorer()
        self.ema_focused_scorer = _FakeScorer()
        self.balanced_scorer = _FakeScorer()
        self.structure_analyzer = _FakeStructure()
        self.bb_calc = _FakeBB()


@pytest.fixture
def wire_env(monkeypatch):
    """Wires a fake ServiceContainer + neutralizes every network/DB touch
    reachable from ``_compute_assets_response`` (funding/smart-money batch
    fetch, Redis for facts storage + heartbeat). Returns a setup function
    the test calls with its own symbol universe.
    """
    # These are imported inline (call-time) inside carrac/precompute.py, so
    # patching the source module's attribute is picked up on every call.
    monkeypatch.setattr(
        "carrac.facts.funding.fetch_funding_history",
        lambda window_days=90, conn=None: {},
    )
    monkeypatch.setattr(
        "carrac.facts.on_chain.fetch_smart_money_bias",
        lambda conn=None: {},
    )
    monkeypatch.setattr("carrac.redis_cache.get_redis", lambda: None)

    df = _make_ohlcv()

    def _setup(symbols, bad_symbols=()):
        db = _FakeDB(symbols, df, bad_symbols=bad_symbols)
        services = _FakeServices(db)
        monkeypatch.setattr(precompute, "get_services", lambda app=None: services)
        return db, services

    return _setup


def _run_pass(monkeypatch, workers, caplog):
    """Run one HTF pass with PRECOMPUTE_WORKERS forced to `workers`, with a
    fresh previous_snapshot (module global) so runs don't leak into each
    other, and return (response_dict, (facts_ok, facts_attempted)).
    """
    monkeypatch.setattr(precompute, "PRECOMPUTE_WORKERS", workers)
    monkeypatch.setattr(precompute, "previous_snapshot", None)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="carrac.precompute"):
        response = precompute._compute_assets_response("htf")
    facts_counts = None
    for record in caplog.records:
        if record.msg == "facts bundles: %d/%d assets stored":
            facts_counts = record.args
            break
    assert facts_counts is not None, "facts bundles log line not found"
    return response, facts_counts


# ---------------------------------------------------------------------------
# (a) order + facts-counter equality: PRECOMPUTE_WORKERS=1 vs 3
# ---------------------------------------------------------------------------

class TestOrderAndFactsCountersMatchAcrossWorkerCounts:
    def test_symbol_order_identical(self, monkeypatch, caplog, wire_env):
        symbols = [f"SYM{i}" for i in range(12)]
        wire_env(symbols)

        serial, _ = _run_pass(monkeypatch, 1, caplog)
        parallel, _ = _run_pass(monkeypatch, 3, caplog)

        serial_order = [a["symbol"] for a in serial["assets"]]
        parallel_order = [a["symbol"] for a in parallel["assets"]]

        assert serial_order == symbols
        assert parallel_order == symbols
        assert serial["count"] == parallel["count"] == len(symbols)

    def test_facts_counters_equal(self, monkeypatch, caplog, wire_env):
        symbols = [f"SYM{i}" for i in range(12)]
        wire_env(symbols)

        _, serial_facts = _run_pass(monkeypatch, 1, caplog)
        _, parallel_facts = _run_pass(monkeypatch, 3, caplog)

        assert serial_facts == parallel_facts
        # Sanity: with all-good symbols in HTF mode the facts bundle is
        # attempted (and stored) for every one of them — not just 0/0.
        ok, attempted = serial_facts
        assert attempted == len(symbols)
        assert ok == len(symbols)

    def test_workers_of_one_uses_no_executor(self, monkeypatch, caplog, wire_env):
        """PRECOMPUTE_WORKERS=1 must be genuinely serial (no ThreadPoolExecutor
        instantiated at all) so a rollback keeps simple stack traces. Patches
        the name as resolved inside carrac.precompute (a `from ... import`
        binding, not the concurrent.futures module attribute) since that's
        what _compute_assets_response actually looks up."""
        symbols = [f"SYM{i}" for i in range(5)]
        wire_env(symbols)

        created = []
        real_executor = precompute.ThreadPoolExecutor

        def _spy_executor(*a, **k):
            created.append(1)
            return real_executor(*a, **k)

        monkeypatch.setattr(precompute, "ThreadPoolExecutor", _spy_executor)

        _run_pass(monkeypatch, 1, caplog)
        assert created == [], "ThreadPoolExecutor must not be created when workers=1"

        _run_pass(monkeypatch, 3, caplog)
        assert created == [1], "ThreadPoolExecutor must be created when workers>1"


# ---------------------------------------------------------------------------
# (b) one symbol raising inside the helper doesn't kill the pass
# ---------------------------------------------------------------------------

class TestOneBadSymbolDoesNotKillThePass:
    @pytest.mark.parametrize("workers", [1, 3])
    def test_bad_symbol_skipped_rest_survive(self, monkeypatch, caplog, wire_env, workers):
        symbols = ["GOOD1", "BADSYM", "GOOD2", "GOOD3"]
        wire_env(symbols, bad_symbols={"BADSYM"})

        response, facts_counts = _run_pass(monkeypatch, workers, caplog)

        returned_symbols = [a["symbol"] for a in response["assets"]]
        assert returned_symbols == ["GOOD1", "GOOD2", "GOOD3"]
        assert response["count"] == 3
        # The bad symbol never reached the facts-bundle stage.
        ok, attempted = facts_counts
        assert attempted == 3
        assert ok == 3

    def test_compute_one_symbol_returns_none_on_failure_not_raise(self, wire_env):
        """Direct unit check on the extracted helper itself: a raising
        dependency (here, db.get_asset_info) is caught internally and
        surfaces as (None, zero-delta) — it must never propagate."""
        db, services = wire_env(["BADSYM"], bad_symbols={"BADSYM"})

        asset_result, facts_delta = precompute._compute_one_symbol(
            "BADSYM",
            is_ltf=False,
            db=services.db,
            consensio=services.consensio,
            rsi_calc=services.rsi_calc,
            ema_ribbon_calc=services.ema_ribbon_calc,
            trend_analyzer=services.trend_analyzer,
            divergence_detector_daily=services.divergence_detector_daily,
            divergence_detector_weekly=services.divergence_detector_weekly,
            adx_calc=services.adx_calc,
            cpr_calc=services.cpr_calc,
            opportunity_scorer=services.opportunity_scorer,
            research_scorer=services.research_scorer,
            user_scorer=services.user_scorer,
            ema_focused_scorer=services.ema_focused_scorer,
            balanced_scorer=services.balanced_scorer,
            structure_analyzer=services.structure_analyzer,
            bb_calc=services.bb_calc,
            realtime_prices={},
            previous_snapshot={},
            funding_hist={},
            smart_money_bias_map={},
        )

        assert asset_result is None
        assert facts_delta == {"elapsed": 0.0, "ok": 0, "attempted": 0}
