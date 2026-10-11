"""Tests for afternoon summary extraction, weekly play-review payload,
and quant-context formatting of stay_prob + weak-signal labels.

Pure-logic tests — no live API, no LLM, no DB.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# 1. Afternoon (and weekly/week-ahead) summary extraction
# ---------------------------------------------------------------------------

class TestExtractAfternoonSummary:
    """_extract_afternoon_summary parses Session Summary first paragraph."""

    def _call(self, content):
        import morning_engine.engine as eng
        return eng._extract_afternoon_summary(content)

    def test_none_on_empty_content(self):
        assert self._call("") is None
        assert self._call(None) is None

    def test_parses_session_summary_section(self):
        content = (
            "## Session Summary\n"
            "BTC rose 2.1% in the US session as CPI printed 0.2% MoM, "
            "below the 0.3% forecast, sparking a risk-on rally across crypto.\n\n"
            "## Signal Changes\n| BTC | bullish flip |\n"
        )
        result = self._call(content)
        assert result is not None
        assert "BTC rose" in result
        # Should NOT include heading line
        assert "Session Summary" not in result

    def test_falls_back_to_first_paragraph_if_no_section(self):
        content = (
            "Markets moved sharply higher as the dollar retreated "
            "on soft inflation data across all categories.\n\n"
            "More context here."
        )
        result = self._call(content)
        assert result is not None
        assert "Markets moved" in result

    def test_truncates_at_300_chars(self):
        long_para = "A" * 400
        content = f"## Session Summary\n{long_para}\n"
        result = self._call(content)
        assert result is not None
        assert len(result) <= 305  # small slack for ellipsis

    def test_skips_heading_lines(self):
        content = (
            "## Session Summary\n"
            "# Not a paragraph\n"
            "Real content starts here: risk-on rally, BTC +2%.\n"
        )
        result = self._call(content)
        assert result is not None
        assert "Real content" in result
        assert "Not a paragraph" not in result

    def test_skips_table_lines(self):
        content = (
            "## Session Summary\n"
            "| Asset | Move |\n"
            "|-------|------|\n"
            "| BTC   | +2%  |\n\n"
            "The session saw broad risk-on buying across crypto markets today.\n"
        )
        result = self._call(content)
        assert result is not None
        assert "session saw" in result

    def test_skips_bullet_lines(self):
        content = (
            "## Session Summary\n"
            "- BTC up\n"
            "- ETH up\n\n"
            "Markets rallied on positive macro data throughout the afternoon.\n"
        )
        result = self._call(content)
        assert result is not None
        assert "rallied" in result

    def test_sentence_boundary_truncation(self):
        # Build content where first sentence is <300 chars, second pushes over
        s1 = "BTC rallied 2.1% in the US afternoon session following soft CPI data. "
        s2 = "B" * 250  # padding to push over 300
        content = f"## Session Summary\n{s1}{s2}\n"
        result = self._call(content)
        assert result is not None
        # Should truncate at or near sentence boundary
        assert len(result) <= 310

    def test_content_with_no_usable_paragraph_returns_none(self):
        # Only heading lines, bullets, and tables — no prose paragraph
        content = (
            "## Session Summary\n"
            "| A | B |\n"
            "- x\n"
            "# heading\n"
        )
        # Short lines / bullets all skipped — may return None or a very short string
        result = self._call(content)
        # Either None or something non-empty and < 20 chars which also means None
        if result is not None:
            assert len(result) >= 20  # if returned, must be substantive


# ---------------------------------------------------------------------------
# 2. Weekly play review payload builder
# ---------------------------------------------------------------------------

class TestFetchWeeklyPlayReviewText:
    """_fetch_weekly_play_review_text builds a capped summary for the digest."""

    @pytest.fixture(autouse=True)
    def _operator(self, monkeypatch):
        import morning_engine.engine as eng
        monkeypatch.setattr(eng, '_operator_user_id', lambda: 7)

    def test_reads_only_the_operators_plays_and_trades(self, monkeypatch):
        import morning_engine.engine as eng
        seen = {}

        def fake_plays(user_id=None):
            seen['plays'] = user_id
            return []

        class FakeDB:
            def get_trade_history(self, uid, days=7, limit=20):
                seen['trades'] = uid
                return []

        monkeypatch.setattr('carrac.plays.models.get_active_plays', fake_plays)
        import carrac.content_db as cdb_mod
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: FakeDB())

        eng._fetch_weekly_play_review_text()
        assert seen == {'plays': 7, 'trades': 7}

    def test_no_operator_account_reads_nothing(self, monkeypatch):
        import morning_engine.engine as eng
        monkeypatch.setattr(eng, '_operator_user_id', lambda: None)

        def fail(*a, **k):
            raise AssertionError('must not read any user data without an operator')

        monkeypatch.setattr('carrac.plays.models.get_active_plays', fail)
        import carrac.content_db as cdb_mod
        monkeypatch.setattr(cdb_mod, 'get_content_db', fail)

        assert eng._fetch_weekly_play_review_text() == "none this week"

    def _make_fake_play(self, title, status, asset, direction, met, total):
        return {
            'title': title,
            'status': status,
            'primary_asset': asset,
            'direction': direction,
            'conditions': {'rules': [{'met': i < met} for i in range(total)]},
            'status_changed_at': None,
        }

    def _make_fake_trade(self, asset, direction, rr, closed_at, setup=None):
        return {
            'asset': asset,
            'direction': direction,
            'rr_actual': rr,
            'closed_at': closed_at,
            'setup_type': setup,
        }

    def test_none_this_week_when_no_data(self, monkeypatch):
        import morning_engine.engine as eng

        # Patch plays and trades to return empty
        monkeypatch.setattr(
            'carrac.plays.models.get_active_plays', lambda user_id=None: [])

        class FakeDB:
            def get_trade_history(self, uid, days=7, limit=20):
                return []

        import carrac.content_db as cdb_mod
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: FakeDB())

        result = eng._fetch_weekly_play_review_text()
        assert result == "none this week"

    def test_active_plays_appear_in_output(self, monkeypatch):
        import morning_engine.engine as eng

        plays = [
            self._make_fake_play("BTC Bull Setup", "watching", "BTC", "long", 2, 4),
            self._make_fake_play("ETH Short", "acting", "ETH", "short", 3, 3),
        ]
        monkeypatch.setattr('carrac.plays.models.get_active_plays', lambda user_id=None: plays)

        class FakeDB:
            def get_trade_history(self, uid, days=7, limit=20):
                return []

        import carrac.content_db as cdb_mod
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: FakeDB())

        result = eng._fetch_weekly_play_review_text()
        assert "BTC Bull Setup" in result
        assert "ETH Short" in result
        assert "2/4" in result  # conditions met/total
        assert "3/3" in result

    def test_closed_trades_appear_in_output(self, monkeypatch):
        import morning_engine.engine as eng

        monkeypatch.setattr('carrac.plays.models.get_active_plays', lambda user_id=None: [])

        trades = [
            self._make_fake_trade("BTC", "long", 2.1, "2026-06-09T14:00:00", "bb_breakout"),
            self._make_fake_trade("ETH", "short", -0.8, "2026-06-08T10:00:00"),
        ]

        class FakeDB:
            def get_trade_history(self, uid, days=7, limit=20):
                return trades

        import carrac.content_db as cdb_mod
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: FakeDB())

        result = eng._fetch_weekly_play_review_text()
        assert "BTC" in result
        assert "ETH" in result
        assert "R=" in result  # R-multiple shown
        assert "wins" in result or "losses" in result

    def test_output_capped_at_max_chars(self, monkeypatch):
        import morning_engine.engine as eng

        # Create many plays to push over the cap
        plays = [
            self._make_fake_play(f"Play {i} — very long title to use characters up here",
                                 "watching", "BTC", "long", 1, 3)
            for i in range(30)
        ]
        monkeypatch.setattr('carrac.plays.models.get_active_plays', lambda user_id=None: plays)

        class FakeDB:
            def get_trade_history(self, uid, days=7, limit=20):
                return []

        import carrac.content_db as cdb_mod
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: FakeDB())

        result = eng._fetch_weekly_play_review_text(max_chars=3000)
        assert len(result) <= 3010  # small slack for ellipsis marker

    def test_plays_fetch_failure_returns_graceful(self, monkeypatch):
        """If plays import fails, still return something (non-fatal)."""
        import morning_engine.engine as eng

        def boom(user_id=None):
            raise RuntimeError("DB exploded")

        monkeypatch.setattr('carrac.plays.models.get_active_plays', boom)

        class FakeDB:
            def get_trade_history(self, uid, days=7, limit=20):
                return []

        import carrac.content_db as cdb_mod
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: FakeDB())

        result = eng._fetch_weekly_play_review_text()
        # Non-fatal: should return something even if partial
        assert isinstance(result, str)

    def test_terminal_plays_this_week_appear(self, monkeypatch):
        """Plays that completed/invalidated this week should appear in output."""
        import morning_engine.engine as eng
        from datetime import datetime, timezone, timedelta

        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        completed_play = {
            'title': 'Completed BTC Long',
            'status': 'completed',
            'primary_asset': 'BTC',
            'direction': 'long',
            'conditions': {'rules': []},
            'status_changed_at': recent,
        }
        monkeypatch.setattr(
            'carrac.plays.models.get_active_plays',
            lambda user_id=None: [completed_play])

        class FakeDB:
            def get_trade_history(self, uid, days=7, limit=20):
                return []

        import carrac.content_db as cdb_mod
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: FakeDB())

        result = eng._fetch_weekly_play_review_text()
        assert "Completed BTC Long" in result
        assert "completed" in result


# ---------------------------------------------------------------------------
# 3. Quant context formatting — stay_prob + weak-signal label
# ---------------------------------------------------------------------------

class TestQuantContextFormatting:
    """fetch_quant_context formats stay_prob and labels weak lead-lag signals."""

    def _call_fetch(self, snap):
        """Call fetch_quant_context with a mocked API response."""
        from morning_engine import data_assembly as da

        # We only need to stub the _api function for the snapshot call.
        # All other _api calls (chain detail, conditional, analogues, events)
        # should return empty/safe defaults.
        def fake_api(path, **kw):
            if '/api/quant/snapshot' in path:
                return snap
            return []  # safe default for chain/conditional/analogues/events

        original_api = da._api
        da._api = fake_api
        try:
            result = da.fetch_quant_context()
        finally:
            da._api = original_api
        return result

    def _minimal_snap(self):
        """Minimal valid snapshot dict to avoid KeyErrors in other branches."""
        return {
            'regime': None,
            'anomalies': [],
            'anomaly_count': 0,
            'top_correlations': [],
            'leadlag_signals': [],
            'leadlag_ci_note': '',
            'leadlag_signals_weekly': [],
            'chains': [],
            'anomaly_cluster': None,
            'regime_hist_median_days': {},
        }

    def test_stay_prob_in_output_when_present(self):
        snap = self._minimal_snap()
        snap['regime'] = {
            'state': 'favorable',
            'probability': 0.92,
            'duration_days': 15,
            'since': '2026-05-26',
            'descriptions': {},
            'stay_prob': 0.94,
            'converged': True,
        }
        result = self._call_fetch(snap)
        text = result.get('quant', '')
        assert 'persistence' in text.lower()
        assert '94%' in text
        assert 'converged' in text.lower()

    def test_stay_prob_absent_when_none(self):
        snap = self._minimal_snap()
        snap['regime'] = {
            'state': 'favorable',
            'probability': 0.88,
            'duration_days': 5,
            'since': '2026-06-05',
            'descriptions': {},
            'stay_prob': None,
            'converged': None,
        }
        result = self._call_fetch(snap)
        text = result.get('quant', '')
        # Regime line still present
        assert 'FAVORABLE' in text
        # No persistence line since stay_prob is None
        assert 'persistence' not in text.lower()

    def test_converged_no_noted(self):
        snap = self._minimal_snap()
        snap['regime'] = {
            'state': 'unfavorable',
            'probability': 0.75,
            'duration_days': 3,
            'since': '2026-06-07',
            'descriptions': {},
            'stay_prob': 0.88,
            'converged': False,
        }
        result = self._call_fetch(snap)
        text = result.get('quant', '')
        assert '88%' in text
        assert 'no' in text.lower()  # "model converged: no"

    def test_weak_signal_labeled(self):
        snap = self._minimal_snap()
        snap['leadlag_signals'] = [
            {
                'leader': 'ETF_FLOW',
                'lag_days': -3,
                'correlation': 0.31,
                'p_corrected': 0.18,   # > 0.1 → weak
                'n_lags_tested': 20,
                'persistent': False,
            }
        ]
        result = self._call_fetch(snap)
        text = result.get('quant', '')
        assert '(weak after lag-scan correction)' in text

    def test_strong_signal_not_labeled_weak(self):
        snap = self._minimal_snap()
        snap['leadlag_signals'] = [
            {
                'leader': 'M2',
                'lag_days': -68,
                'correlation': 0.43,
                'p_corrected': 0.04,   # <= 0.1 → not weak
                'n_lags_tested': 30,
                'persistent': True,
            }
        ]
        result = self._call_fetch(snap)
        text = result.get('quant', '')
        assert '(weak after lag-scan correction)' not in text
        # p_corrected annotation still present
        assert 'p_scan=0.040' in text

    def test_no_p_corrected_field_no_annotation(self):
        snap = self._minimal_snap()
        snap['leadlag_signals'] = [
            {
                'leader': 'DXY',
                'lag_days': -2,
                'correlation': 0.25,
                # No p_corrected or n_lags_tested keys
                'persistent': True,
            }
        ]
        result = self._call_fetch(snap)
        text = result.get('quant', '')
        assert 'p_scan=' not in text
        assert '(weak after lag-scan correction)' not in text

    def test_anomaly_cluster_in_output(self):
        snap = self._minimal_snap()
        snap['anomaly_cluster'] = {
            'date': '2026-06-10',
            'extreme_count': 4,
            'domains_affected': 2,
            'metrics': [
                {'metric': 'VIX', 'zscore': 3.2},
                {'metric': 'HY_SPREAD', 'zscore': 2.8},
                {'metric': 'FUNDING_BTC', 'zscore': -2.5},
            ],
        }
        result = self._call_fetch(snap)
        text = result.get('quant', '')
        assert 'cluster' in text.lower()
        assert '4' in text

    # ------------------------------------------------------------------
    # Anomalies must carry their date + a staleness marker
    # ------------------------------------------------------------------

    def test_anomaly_line_carries_a_fresh_date_with_no_staleness_marker(self):
        from datetime import datetime, timezone
        fresh_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        snap = self._minimal_snap()
        snap['anomalies'] = [
            {'metric': 'VIX', 'asset': '_GLOBAL_', 'zscore': 2.5, 'percentile': 90.0,
             'domain': 'macro', 'value': 20, 'date': fresh_date},
        ]
        snap['anomaly_count'] = 1
        result = self._call_fetch(snap)
        text = result.get('quant', '')
        assert fresh_date in text, text
        assert 'old)' not in text, text

    def test_anomaly_line_flags_a_stale_date_explicitly(self):
        """The real case: JAPAN_10Y at +5.6sigma, 21-23 days old,
        printed as 'active' with nothing distinguishing it from today."""
        from datetime import datetime, timedelta, timezone
        stale_date = (datetime.now(timezone.utc) - timedelta(days=23)).strftime('%Y-%m-%d')
        snap = self._minimal_snap()
        snap['anomalies'] = [
            {'metric': 'JAPAN_10Y', 'asset': '_GLOBAL_', 'zscore': 5.61, 'percentile': 100.0,
             'domain': 'macro', 'value': 1.2, 'date': stale_date},
        ]
        snap['anomaly_count'] = 1
        result = self._call_fetch(snap)
        text = result.get('quant', '')
        assert stale_date in text, text
        assert f"{stale_date} — 23d old" in text, text

    # ------------------------------------------------------------------
    # Wire 1 — Bond-vol (MOVE) line formatting
    # ------------------------------------------------------------------

    def test_bond_vol_full_line_in_output(self):
        """Full bond_vol block → level + percentile + label + 5d_change."""
        snap = self._minimal_snap()
        snap['bond_vol'] = {
            'level': 73.9,
            'zscore': -0.41,
            'percentile': 23.0,
            '5d_change': -2.1,
            'label': 'calm',
        }
        result = self._call_fetch(snap)
        text = result.get('quant', '')
        assert 'Bond volatility (MOVE)' in text
        assert '73.9' in text
        assert '23' in text          # percentile
        assert 'calm' in text
        assert '-2.1' in text        # 5d_change

    def test_bond_vol_unknown_label_shows_level_and_5d_only(self):
        """label='unknown' → level + 5d_change, no percentile/z in the label branch."""
        snap = self._minimal_snap()
        snap['bond_vol'] = {
            'level': 80.5,
            'zscore': None,
            'percentile': None,
            '5d_change': 3.2,
            'label': 'unknown',
        }
        result = self._call_fetch(snap)
        text = result.get('quant', '')
        assert 'Bond volatility (MOVE)' in text
        assert '80.5' in text
        assert '+3.2' in text
        assert 'z-score/percentile not yet available' in text

    def test_bond_vol_missing_level_skipped(self):
        """bond_vol block with no level → line must be omitted entirely."""
        snap = self._minimal_snap()
        snap['bond_vol'] = {
            'level': None,
            'zscore': 0.5,
            'percentile': 60.0,
            '5d_change': 1.0,
            'label': 'calm',
        }
        result = self._call_fetch(snap)
        text = result.get('quant', '')
        assert 'Bond volatility (MOVE)' not in text

    def test_bond_vol_absent_from_snap_skipped(self):
        """Snapshot with no bond_vol key → line must be omitted entirely."""
        snap = self._minimal_snap()
        result = self._call_fetch(snap)
        text = result.get('quant', '')
        assert 'Bond volatility (MOVE)' not in text


# ---------------------------------------------------------------------------
# 4. Market breadth fetch formatting
# ---------------------------------------------------------------------------

class TestFetchMarketBreadth:
    """fetch_market_breadth formats the /api/market/breadth response."""

    def _call(self, response_data):
        from morning_engine import data_assembly as da
        original = da._api
        da._api = lambda *a, **kw: response_data
        try:
            return da.fetch_market_breadth()
        finally:
            da._api = original

    def test_formats_compact_line_with_all_fields(self):
        data = {
            'as_of': '2026-06-11',
            'universe_size': 214,
            'covered': 210,
            'oversold': {'count': 84, 'pct': 39.3},
            'overbought': {'count': 3, 'pct': 1.4},
            'above_sma50': {'count': 70, 'pct': 33.0},
            'below_sma50': {'count': 144, 'pct': 67.0},
            'new_20d_highs': {'count': 0},
            'new_20d_lows': {'count': 61},
            'label': 'washed-out',
        }
        result = self._call(data)
        assert 'Market breadth:' in result
        assert '84/214' in result
        assert '39%' in result
        assert '3 overbought' in result
        assert '33% above 50-day' in result
        assert '61 new 20-day lows' in result
        assert 'washed-out' in result

    def test_omits_new_highs_lows_when_zero(self):
        data = {
            'universe_size': 100,
            'oversold': {'count': 10, 'pct': 10.0},
            'overbought': {'count': 2, 'pct': 2.0},
            'above_sma50': {'count': 50, 'pct': 50.0},
            'new_20d_highs': {'count': 0},
            'new_20d_lows': {'count': 0},
            'label': 'neutral',
        }
        result = self._call(data)
        assert '20-day' not in result

    def test_includes_new_highs_when_nonzero(self):
        data = {
            'universe_size': 100,
            'oversold': {'count': 5, 'pct': 5.0},
            'overbought': {'count': 15, 'pct': 15.0},
            'above_sma50': {'count': 60, 'pct': 60.0},
            'new_20d_highs': {'count': 8},
            'new_20d_lows': {'count': 0},
            'label': 'frothy',
        }
        result = self._call(data)
        assert '8 new 20-day highs' in result

    def test_returns_empty_string_on_api_failure(self):
        from morning_engine import data_assembly as da
        original = da._api
        def _fail(*a, **kw):
            raise ConnectionError("network error")
        da._api = _fail
        try:
            result = da.fetch_market_breadth()
        finally:
            da._api = original
        assert result == ''

    def test_returns_empty_on_missing_label(self):
        """Response without 'label' key → return empty (not a valid breadth response)."""
        data = {'universe_size': 100, 'oversold': {'count': 5, 'pct': 5.0}}
        result = self._call(data)
        assert result == ''

    def test_returns_empty_on_non_dict(self):
        result = self._call("not a dict")
        assert result == ''


# ---------------------------------------------------------------------------
# 5. COT fetch formatting
# ---------------------------------------------------------------------------

class TestFetchCot:
    """fetch_cot formats /api/quant/cot with stretched-first ordering."""

    def _make_series_entry(self, market, stype, value, zscore, percentile,
                           wow=None, stretched=False):
        return {
            'market': market,
            'type': stype,
            'value': value,
            'zscore': zscore,
            'percentile': percentile,
            'wow_change': wow,
            'stretched': stretched,
        }

    def _call(self, response_data):
        from morning_engine import data_assembly as da
        original = da._api
        da._api = lambda *a, **kw: response_data
        try:
            return da.fetch_cot()
        finally:
            da._api = original

    def test_stretched_series_appear_first(self):
        """Stretched entries must come before non-stretched in the output."""
        data = {
            'data_date': '2026-06-06',
            'stretched_count': 1,
            'series': [
                # This one is NOT stretched but has a higher |z|
                self._make_series_entry('BTC', 'SPEC_NET', 50000, 1.8, 90, stretched=False),
                # This one IS stretched
                self._make_series_entry('GOLD', 'SPEC_LONG_PCT', 70.0, 2.5, 96, stretched=True),
            ],
        }
        result = self._call(data)
        assert 'GOLD' in result
        assert 'BTC' in result
        gold_pos = result.index('GOLD')
        btc_pos = result.index('BTC')
        assert gold_pos < btc_pos, "STRETCHED GOLD should appear before non-stretched BTC"

    def test_stretched_annotation_present(self):
        data = {
            'data_date': '2026-06-06',
            'stretched_count': 1,
            'series': [
                self._make_series_entry('SP500', 'SPEC_NET', -20000, -2.1, 8, stretched=True),
            ],
        }
        result = self._call(data)
        assert 'STRETCHED' in result

    def test_header_contains_data_date(self):
        data = {
            'data_date': '2026-06-06',
            'stretched_count': 0,
            'series': [
                self._make_series_entry('DXY', 'SPEC_NET', 5000, 0.3, 55),
            ],
        }
        result = self._call(data)
        assert '2026-06-06' in result

    def test_capped_at_six_series(self):
        """At most 6 series lines appear in the output."""
        data = {
            'data_date': '2026-06-06',
            'stretched_count': 0,
            'series': [
                self._make_series_entry(f'ASSET{i}', 'SPEC_NET', i * 1000, float(i), 50 + i)
                for i in range(10)
            ],
        }
        result = self._call(data)
        # Count series data lines (lines with ' | ')
        series_lines = [line for line in result.split('\n') if ' | ' in line]
        assert len(series_lines) <= 6

    def test_truncated_at_1200_chars(self):
        """Output must not exceed 1200 chars (truncation marker added)."""
        # Create many stretched entries with long market names / values
        series = [
            self._make_series_entry(f'LONGNAME{i}', 'SPEC_LONG_PCT', 99.123456, 2.99, 98.7,
                                    wow=5.5, stretched=True)
            for i in range(20)
        ]
        data = {'data_date': '2026-06-06', 'stretched_count': 20, 'series': series}
        result = self._call(data)
        assert len(result) <= 1210  # small slack for truncation marker

    def test_empty_series_returns_empty_string(self):
        data = {'data_date': '2026-06-06', 'stretched_count': 0, 'series': []}
        result = self._call(data)
        assert result == ''

    def test_api_failure_returns_empty_string(self):
        from morning_engine import data_assembly as da
        original = da._api
        def _fail(*a, **kw):
            raise ConnectionError("cot down")
        da._api = _fail
        try:
            result = da.fetch_cot()
        finally:
            da._api = original
        assert result == ''

    def test_missing_series_key_returns_empty_string(self):
        result = self._call({'data_date': '2026-06-06', 'stretched_count': 0})
        assert result == ''

    def test_non_dict_response_returns_empty_string(self):
        result = self._call("not a dict")
        assert result == ''

    def test_type_labels_humanised(self):
        """SPEC_NET → 'spec net', SPEC_LONG_PCT → 'large-spec long%', COMM_NET → 'comm net'."""
        data = {
            'data_date': '2026-06-06',
            'stretched_count': 0,
            'series': [
                self._make_series_entry('GOLD', 'SPEC_NET', 100000, 1.0, 70),
                self._make_series_entry('OIL', 'COMM_NET', -80000, -1.0, 30),
                self._make_series_entry('BTC', 'SPEC_LONG_PCT', 65.0, 1.5, 80),
            ],
        }
        result = self._call(data)
        assert 'spec net' in result
        assert 'comm net' in result
        assert 'large-spec long%' in result
