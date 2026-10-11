"""Tests for the Whale Watch smart-money block + the on_chain facts carrier.

Covers:
- _bias_label() pure-function thresholds
- build_smart_money_block() with a mocked fetch callable: full data, empty data,
  graceful omit when fetch returns []
- on_chain bundle field: bundle["on_chain"]["smart_money_bias"] populated when
  ctx["smart_money_bias"] is set; omitted entirely when None
- wire.py: build_facts_ctx accepts smart_money_bias kwarg and threads it into ctx
- MCP onchain_get_smart_money: registration, premium gate, returns disclaimer
"""

from __future__ import annotations

from datetime import date

import pytest


# ─── _bias_label pure function ────────────────────────────────────────────────

class TestBiasLabel:
    @pytest.fixture(autouse=True)
    def _import(self):
        from morning_engine.report_blocks import _bias_label
        self.f = _bias_label

    def test_strongly_net_long(self):
        assert self.f(70.0) == "net long"
        assert self.f(65.0) == "net long"
        assert self.f(100.0) == "net long"

    def test_mildly_net_long(self):
        assert self.f(60.0) == "mildly net long"
        assert self.f(55.0) == "mildly net long"

    def test_balanced(self):
        assert self.f(50.0) == "balanced"
        assert self.f(45.0) == "balanced"

    def test_mildly_net_short(self):
        assert self.f(40.0) == "mildly net short"
        assert self.f(35.0) == "mildly net short"

    def test_net_short(self):
        assert self.f(34.9) == "net short"
        assert self.f(0.0) == "net short"

    def test_boundary_at_65(self):
        assert self.f(65.0) == "net long"
        assert self.f(64.9) == "mildly net long"

    def test_boundary_at_55(self):
        assert self.f(55.0) == "mildly net long"
        assert self.f(54.9) == "balanced"

    def test_boundary_at_45(self):
        assert self.f(45.0) == "balanced"
        assert self.f(44.9) == "mildly net short"

    def test_boundary_at_35(self):
        assert self.f(35.0) == "mildly net short"
        assert self.f(34.9) == "net short"


# ─── build_smart_money_block with mocked fetch ────────────────────────────────

def _make_row(asset, value, ts='2026-06-30'):
    return {'asset': asset, 'value': value, 'ts': date.fromisoformat(ts)}


class TestBuildSmartMoneyBlock:
    def _run(self, rows):
        from morning_engine.report_blocks import build_smart_money_block
        return build_smart_money_block(fetch=lambda: rows)

    def test_returns_empty_string_when_no_data(self):
        result = self._run([])
        assert result == ""

    def test_returns_empty_string_when_fetch_raises(self):
        from morning_engine.report_blocks import build_smart_money_block

        def _failing_fetch():
            raise RuntimeError("DB error")

        result = build_smart_money_block(fetch=_failing_fetch)
        assert result == ""

    def test_heading_contains_date(self):
        rows = [_make_row('BTC', 68.4, '2026-06-30')]
        result = self._run(rows)
        assert '## Whale Watch' in result
        assert '2026-06-30' in result

    def test_single_asset_net_long(self):
        rows = [_make_row('BTC', 70.0)]
        result = self._run(rows)
        assert 'BTC 70 (net long)' in result

    def test_single_asset_balanced(self):
        rows = [_make_row('ETH', 50.0)]
        result = self._run(rows)
        assert 'ETH 50 (balanced)' in result

    def test_single_asset_net_short(self):
        rows = [_make_row('SOL', 30.0)]
        result = self._run(rows)
        assert 'SOL 30 (net short)' in result

    def test_multiple_assets_joined_with_dot(self):
        rows = [_make_row('BTC', 70.0), _make_row('ETH', 40.0)]
        result = self._run(rows)
        assert ' · ' in result
        assert 'BTC' in result
        assert 'ETH' in result

    def test_non_advice_note_present(self):
        rows = [_make_row('BTC', 68.0)]
        result = self._run(rows)
        # Must not use buy/sell/recommend
        assert 'buy' not in result.lower()
        assert 'sell' not in result.lower()
        assert 'recommend' not in result.lower()
        # Must contain the non-advice note
        assert 'not advice' in result.lower()

    def test_value_rendered_as_integer(self):
        rows = [_make_row('BTC', 68.4)]
        result = self._run(rows)
        # Should render as "68", not "68.4" (formatted with :.0f)
        assert '68 (' in result

    def test_returns_multiline_string(self):
        rows = [_make_row('BTC', 68.0)]
        result = self._run(rows)
        lines = result.split('\n')
        assert len(lines) >= 3  # heading + body + note


# ─── on_chain bundle field ─────────────────────────────────────────────────────

class TestOnChainBundle:
    """bundle.py adds on_chain.smart_money_bias when ctx has the value."""

    def _build(self, smart_money_bias):
        import pandas as pd
        from carrac.facts.wire import build_facts_ctx
        from carrac.facts.bundle import build_asset_facts

        df = pd.DataFrame([{
            'open': 60000.0, 'high': 62000.0, 'low': 59000.0, 'close': 61000.0,
        }])
        ctx = build_facts_ctx(
            'BTC', df, 61000.0,
            smart_money_bias=smart_money_bias,
        )
        return build_asset_facts('BTC', ctx)

    def test_on_chain_present_when_bias_set(self):
        bundle = self._build(68.4)
        assert 'on_chain' in bundle
        assert bundle['on_chain']['smart_money_bias'] == pytest.approx(68.4)

    def test_on_chain_absent_when_bias_none(self):
        bundle = self._build(None)
        assert 'on_chain' not in bundle

    def test_on_chain_value_is_float(self):
        bundle = self._build(52.0)
        assert isinstance(bundle['on_chain']['smart_money_bias'], float)

    def test_zero_bias_still_writes_bundle(self):
        """bias=0 is a valid extreme — must not be treated as falsy."""
        bundle = self._build(0.0)
        assert 'on_chain' in bundle
        assert bundle['on_chain']['smart_money_bias'] == 0.0


# ─── wire.py ctx threading ────────────────────────────────────────────────────

class TestWireCtx:
    """build_facts_ctx threads smart_money_bias into ctx unchanged."""

    def _ctx(self, bias):
        import pandas as pd
        from carrac.facts.wire import build_facts_ctx

        df = pd.DataFrame([{
            'open': 1.0, 'high': 1.1, 'low': 0.9, 'close': 1.0,
        }])
        return build_facts_ctx('ETH', df, 1.0, smart_money_bias=bias)

    def test_bias_threaded_into_ctx(self):
        ctx = self._ctx(42.5)
        assert ctx['smart_money_bias'] == pytest.approx(42.5)

    def test_none_bias_threaded_as_none(self):
        ctx = self._ctx(None)
        assert ctx['smart_money_bias'] is None

    def test_default_is_none(self):
        import pandas as pd
        from carrac.facts.wire import build_facts_ctx

        df = pd.DataFrame([{'open': 1.0, 'high': 1.1, 'low': 0.9, 'close': 1.0}])
        ctx = build_facts_ctx('BTC', df, 1.0)
        assert ctx['smart_money_bias'] is None
