"""Fix: "Signal Changes" / RSI-crossing
surfaces leaked non-registry symbols into shared afternoon/general reports.

get_morning_context's _fetch_signal_diff and /api/report/diff (api_report_diff)
both build their change list by iterating db.load_previous_snapshot(), which
spans the FULL ~350-symbol tracked universe — not the 20-symbol chart
registry.

The RS-Rankings sibling of this exact bug (and then the Rotation strip) established the pattern this fix follows:
  - carrac.assets.registry.public_symbol_allowlist() is the one shared
    predicate (never re-derive an allowlist locally).
  - Filter at the point that BUILDS the row list, not just where it renders.
  - Pass through unfiltered (never blank the section) if the registry can't
    be read.

Additionally: even registry members span asset classes (SPX/GOLD/QQQ are
registry AND equities/commodities), so every change is now tagged
is_crypto (via database.py::OHLCVDatabase._is_crypto, the same helper
used by the weekend-bar guard) so a report can never narrate a registry equity as part of
a "crypto alts" cluster.
"""
import inspect


from carrac.reports import routes


# ---------------------------------------------------------------------------
# 1. _signal_diff_registry_symbols — pure registry filter
# ---------------------------------------------------------------------------

class TestSignalDiffRegistrySymbols:
    def test_drops_watchlist_equity_from_a_crypto_labeled_list(self, monkeypatch):
        """The exact fabricated scenario from the fix spec: a watchlist
        equity (TSLA) sitting amid what a report would narrate as a crypto
        RSI-oversold cluster, plus NVDA (a watchlist-only symbol) in a
        broad scanner screen — both must be dropped; registry members
        (BTC, SPX) survive."""
        monkeypatch.setattr(
            "carrac.assets.registry.public_symbol_allowlist",
            lambda: {"BTC", "ETH", "SPX", "GOLD"},
        )
        kept = routes._signal_diff_registry_symbols(["BTC", "TSLA", "NVDA", "SPX", "META"])
        assert kept == ["BTC", "SPX"]

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(
            "carrac.assets.registry.public_symbol_allowlist",
            lambda: {"BTC"},
        )
        assert routes._signal_diff_registry_symbols(["btc"]) == ["btc"]

    def test_passes_through_unfiltered_when_registry_unavailable(self, monkeypatch):
        """Idiom: an over-inclusive diff someone notices beats
        a vanished section nobody can explain."""
        monkeypatch.setattr(
            "carrac.assets.registry.public_symbol_allowlist",
            lambda: None,
        )
        kept = routes._signal_diff_registry_symbols(["BTC", "TSLA", "NVDA"])
        assert kept == ["BTC", "TSLA", "NVDA"]

    def test_empty_input(self, monkeypatch):
        monkeypatch.setattr(
            "carrac.assets.registry.public_symbol_allowlist",
            lambda: {"BTC"},
        )
        assert routes._signal_diff_registry_symbols([]) == []


# ---------------------------------------------------------------------------
# 2. _tag_signal_diff_asset_class — pure classification tag
# ---------------------------------------------------------------------------

class _StubDB:
    def __init__(self, crypto_symbols):
        self._crypto = {s.upper() for s in crypto_symbols}

    def _is_crypto(self, symbol):
        return symbol.upper() in self._crypto


class TestTagSignalDiffAssetClass:
    def test_tags_crypto_and_non_crypto_correctly(self):
        db = _StubDB({"BTC", "ETH"})
        changes = [
            {"symbol": "BTC", "change_type": "RSI entered oversold (<30)"},
            {"symbol": "SPX", "change_type": "1D signal flip (bearish)"},
        ]
        out = routes._tag_signal_diff_asset_class(changes, db)
        assert out[0]["is_crypto"] is True
        assert out[1]["is_crypto"] is False

    def test_missing_symbol_key_does_not_crash(self):
        db = _StubDB({"BTC"})
        changes = [{"change_type": "no symbol here"}]
        out = routes._tag_signal_diff_asset_class(changes, db)
        assert out[0]["is_crypto"] is False

    def test_returns_the_same_list_for_convenience(self):
        db = _StubDB({"BTC"})
        changes = [{"symbol": "BTC"}]
        assert routes._tag_signal_diff_asset_class(changes, db) is changes


# ---------------------------------------------------------------------------
# 3. Wiring: both routes actually call the filter + tagger
# ---------------------------------------------------------------------------

class TestSignalDiffWiring:
    def test_morning_context_filters_before_the_compute_loop(self):
        src = inspect.getsource(routes.get_morning_context)
        filter_pos = src.index("_signal_diff_registry_symbols(symbols)")
        loop_pos = src.index("for symbol in symbols:")
        assert filter_pos < loop_pos, "filter must run BEFORE the expensive per-symbol compute loop"

    def test_morning_context_tags_asset_class(self):
        src = inspect.getsource(routes.get_morning_context)
        assert "_tag_signal_diff_asset_class(changes, db)" in src

    def test_report_diff_route_filters_before_the_compute_loop(self):
        src = inspect.getsource(routes.api_report_diff)
        filter_pos = src.index("_signal_diff_registry_symbols(symbols)")
        loop_pos = src.index("for symbol in symbols:")
        assert filter_pos < loop_pos

    def test_report_diff_route_tags_asset_class(self):
        src = inspect.getsource(routes.api_report_diff)
        assert "_tag_signal_diff_asset_class(changes, db)" in src


# ---------------------------------------------------------------------------
# 4. Rendering: morning_engine.data_assembly's shared line formatter
# ---------------------------------------------------------------------------

class TestFormatSignalDiffLine:
    def _fmt(self, c):
        from morning_engine.data_assembly import _format_signal_diff_line
        return _format_signal_diff_line(c)

    def test_crypto_tag_shown(self):
        line = self._fmt({"symbol": "BTC", "significance": "high",
                           "change_type": "RSI entered oversold (<30)", "is_crypto": True})
        assert "(crypto)" in line
        assert "BTC" in line

    def test_non_crypto_tag_shown(self):
        line = self._fmt({"symbol": "SPX", "significance": "high",
                           "change_type": "RSI entered oversold (<30)", "is_crypto": False})
        assert "(non-crypto)" in line

    def test_no_tag_when_is_crypto_absent(self):
        """Backward-compatible: a payload that predates this fix (no
        is_crypto key) must not crash or fabricate a tag."""
        line = self._fmt({"symbol": "BTC", "significance": "high", "change_type": "flip"})
        assert "(crypto)" not in line
        assert "(non-crypto)" not in line

    def test_fetch_context_data_uses_the_shared_formatter(self):
        import inspect
        from morning_engine import data_assembly as da
        src = inspect.getsource(da.fetch_context_data)
        assert "_format_signal_diff_line(c)" in src

    def test_legacy_fallback_uses_the_shared_formatter(self):
        import inspect
        from morning_engine import data_assembly as da
        src = inspect.getsource(da._format_signal_diff_legacy)
        assert "_format_signal_diff_line(c)" in src
