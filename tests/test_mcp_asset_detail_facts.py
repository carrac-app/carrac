"""market_get_detail MCP tool: facts field injection tests.

Verifies that market_get_detail:
* Attaches result['facts'] from get_asset_facts (stub returns sentinel)
* Is None-safe when get_asset_facts returns None
"""

from __future__ import annotations

import pytest


_SENTINEL_BUNDLE = {
    "symbol": "ETH",
    "ma_stack": {"signal": "bullish", "score": 2},
    "rsi": {"value": 48.0, "zone": "neutral"},
}

_FAKE_COMPACT = {
    "symbol": "ETH",
    "price": 3500.0,
    "change_24h": 1.5,
    "technicals": {},
    "scanner": {},
}


class TestGetAssetDetailFacts:

    def _call(self, monkeypatch, *, facts_return):
        """Call market_get_detail with assembler + facts stubbed out.

        get_asset_facts is imported lazily inside the handler body, so we
        patch it at carrac.facts.wire (the canonical source), not at the
        calling module.
        """
        monkeypatch.setattr(
            "carrac.composite.assemblers.assemble_asset_detail",
            lambda symbol, user_id=None: {},
        )
        monkeypatch.setattr(
            "carrac.composite.formatters.compact_asset_detail",
            lambda data: dict(_FAKE_COMPACT),
        )
        monkeypatch.setattr(
            "carrac.facts.wire.get_asset_facts",
            lambda sym, r=None: facts_return,
        )

        from carrac.mcp.tools.market import market_get_detail
        return market_get_detail({"symbol": "ETH"})

    def test_facts_field_present_with_sentinel(self, monkeypatch):
        result = self._call(monkeypatch, facts_return=_SENTINEL_BUNDLE)
        assert "facts" in result
        assert result["facts"] == _SENTINEL_BUNDLE

    def test_facts_field_none_when_cache_miss(self, monkeypatch):
        result = self._call(monkeypatch, facts_return=None)
        assert "facts" in result
        assert result["facts"] is None

    def test_facts_none_does_not_raise(self, monkeypatch):
        try:
            self._call(monkeypatch, facts_return=None)
        except Exception as exc:
            pytest.fail(f"market_get_detail raised on None facts: {exc}")

    def test_other_fields_still_present(self, monkeypatch):
        result = self._call(monkeypatch, facts_return=_SENTINEL_BUNDLE)
        assert result["symbol"] == "ETH"
        assert result["price"] == 3500.0
