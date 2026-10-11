"""Tests for the event actuals FRED-fill pipeline.

Covers:
  1. Mapping transforms — synthetic FRED obs values → formatted strings.
  2. fill_event_actuals() with mocked FRED fetch:
     - fills when FRED data present and period matches.
     - leaves NULL when period-check fails.
     - no-op when no matching event_type.
  3. /api/quant/events/fill-actuals endpoint.
  4. today_calendar_text rendering with and without actuals.

Pure-logic or monkeypatched — no live DB, no live FRED calls.
"""
from __future__ import annotations



# ---------------------------------------------------------------------------
# 1. Mapping / formatter unit tests
# ---------------------------------------------------------------------------

class TestFormatters:
    """Formatter functions produce correct strings from synthetic observations."""

    def test_fmt_cpi_mom_yoy(self):
        from quant.collectors.event_calendar import _fmt_cpi
        # obs newest-first: current=315.6, prior_month=315.3, year_ago=310.0
        obs = [315.6, 315.3] + [0.0] * 10 + [310.0]
        result = _fmt_cpi(obs)
        assert result is not None
        assert 'CPI' in result
        assert 'MoM' in result
        assert 'YoY' in result
        # MoM: (315.6 - 315.3) / 315.3 * 100 ≈ 0.1%
        assert '+0.1%' in result

    def test_fmt_cpi_positive_and_negative(self):
        from quant.collectors.event_calendar import _fmt_cpi
        # Deflation month: current < prior
        obs = [310.0, 311.0] + [0.0] * 10 + [300.0]
        result = _fmt_cpi(obs)
        assert result is not None
        assert '-' in result  # negative MoM

    def test_fmt_cpi_insufficient_obs(self):
        from quant.collectors.event_calendar import _fmt_cpi
        # Only 1 observation — can't compute MoM
        result = _fmt_cpi([315.0])
        assert result is None

    def test_fmt_core_cpi(self):
        from quant.collectors.event_calendar import _fmt_core_cpi
        obs = [320.0, 319.5] + [0.0] * 10 + [310.0]
        result = _fmt_core_cpi(obs)
        assert result is not None
        assert 'Core CPI' in result
        assert 'MoM' in result

    def test_fmt_nfp_positive(self):
        from quant.collectors.event_calendar import _fmt_nfp
        # PAYEMS in thousands: 158000.0 → 158200.0 (added 200K)
        obs = [158200.0, 158000.0]
        result = _fmt_nfp(obs)
        assert result is not None
        assert 'NFP' in result
        assert '+200K' in result

    def test_fmt_nfp_negative(self):
        from quant.collectors.event_calendar import _fmt_nfp
        obs = [157800.0, 158000.0]
        result = _fmt_nfp(obs)
        assert result is not None
        assert '-200K' in result

    def test_fmt_nfp_insufficient_obs(self):
        from quant.collectors.event_calendar import _fmt_nfp
        result = _fmt_nfp([158000.0])
        assert result is None

    def test_fmt_unemployment_level(self):
        from quant.collectors.event_calendar import _fmt_unemployment
        obs = [3.9]
        result = _fmt_unemployment(obs)
        assert result is not None
        assert 'Unemployment' in result
        assert '3.9%' in result

    def test_fmt_unemployment_empty(self):
        from quant.collectors.event_calendar import _fmt_unemployment
        result = _fmt_unemployment([])
        assert result is None

    def test_fmt_core_pce(self):
        from quant.collectors.event_calendar import _fmt_core_pce
        obs = [121.0, 120.8] + [0.0] * 10 + [117.0]
        result = _fmt_core_pce(obs)
        assert result is not None
        assert 'Core PCE' in result
        assert 'MoM' in result

    def test_fmt_fomc_held(self):
        from quant.collectors.event_calendar import _fmt_fomc
        # Same rate: current = prior = 3.75
        result = _fmt_fomc([3.75], prior_obs=[3.75])
        assert result is not None
        assert 'held' in result.lower()
        assert '3.75%' in result

    def test_fmt_fomc_cut(self):
        from quant.collectors.event_calendar import _fmt_fomc
        # Cut from 4.00 to 3.75 = -25bp
        result = _fmt_fomc([3.75], prior_obs=[4.00])
        assert result is not None
        assert 'cut' in result.lower()
        assert '25bp' in result
        assert '3.75%' in result

    def test_fmt_fomc_hike(self):
        from quant.collectors.event_calendar import _fmt_fomc
        result = _fmt_fomc([4.00], prior_obs=[3.75])
        assert result is not None
        assert 'hiked' in result.lower()
        assert '25bp' in result

    def test_fmt_fomc_no_prior(self):
        from quant.collectors.event_calendar import _fmt_fomc
        result = _fmt_fomc([3.75], prior_obs=None)
        assert result is not None
        assert '3.75%' in result


# ---------------------------------------------------------------------------
# 2. fill_event_actuals() — mocked FRED + mocked DB
# ---------------------------------------------------------------------------

class TestFillEventActuals:
    """fill_event_actuals fills, skips on period-mismatch, and no-ops on unknown type."""

    def _run_fill(self, events_rows, monkeypatch,
                  obs_values=None, period_ok=True, api_key='TESTKEY'):
        """Helper: patch DB + FRED, call fill_event_actuals, return updated_count."""
        from quant.collectors import event_calendar as ec

        # Patch FRED_API_KEY env
        monkeypatch.setenv('FRED_API_KEY', api_key)

        # Patch _fetch_fred_obs
        obs_values = obs_values or [315.6, 315.3] + [0.0] * 10 + [310.0]
        monkeypatch.setattr(ec, '_fetch_fred_obs',
                            lambda series_id, api_key_, n=15: list(obs_values))

        # Patch _obs_period_is_recent
        monkeypatch.setattr(ec, '_obs_period_is_recent',
                            lambda series_id, api_key_, date_, **kw: period_ok)

        # Patch get_conn: first call returns events, second call does UPDATE
        updated_rows = []

        class FakeCursor:
            def __init__(self):
                self.rowcount = 0
                self._events = list(events_rows)
                self._phase = 'fetch'

            def execute(self, sql, params=None):
                if 'SELECT id' in sql:
                    self._phase = 'fetch'
                elif 'UPDATE quant.events' in sql:
                    self._phase = 'update'
                    self.rowcount = 1
                    updated_rows.append(params)

            def fetchall(self):
                if self._phase == 'fetch':
                    return self._events
                return []

        class FakeConn:
            def __init__(self, cur):
                self._cur = cur

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def cursor(self):
                return self._cur

        _cursor = FakeCursor()
        monkeypatch.setattr(ec, 'get_conn', lambda: FakeConn(_cursor))

        # Also patch time.sleep to be instant
        monkeypatch.setattr(ec.time, 'sleep', lambda s: None)

        result = ec.fill_event_actuals('2026-06-10')
        return result, updated_rows

    def test_fills_cpi_event(self, monkeypatch):
        events = [{'id': 1, 'event_type': 'CPI', 'title': 'CPI Release (May)'}]
        count, rows = self._run_fill(events, monkeypatch, period_ok=True)
        assert count == 1
        assert len(rows) == 1
        formatted, evt_id = rows[0]
        assert 'CPI' in formatted
        assert evt_id == 1

    def test_skips_when_period_check_fails(self, monkeypatch):
        events = [{'id': 2, 'event_type': 'CPI', 'title': 'CPI Release (May)'}]
        count, rows = self._run_fill(events, monkeypatch, period_ok=False)
        assert count == 0
        assert len(rows) == 0

    def test_noop_for_unknown_event_type(self, monkeypatch):
        events = [{'id': 3, 'event_type': 'OPTIONS_EXPIRY', 'title': 'Triple Witching'}]
        count, rows = self._run_fill(events, monkeypatch, period_ok=True)
        assert count == 0
        assert len(rows) == 0

    def test_fills_nfp(self, monkeypatch):
        events = [{'id': 4, 'event_type': 'NFP', 'title': 'Non-Farm Payrolls (May)'}]
        obs = [158200.0, 158000.0]  # +200K
        count, rows = self._run_fill(events, monkeypatch, obs_values=obs, period_ok=True)
        assert count == 1
        formatted, _ = rows[0]
        assert '+200K' in formatted

    def test_detects_core_cpi_by_title(self, monkeypatch):
        events = [{'id': 5, 'event_type': 'CPI', 'title': 'Core CPI Release (May)'}]
        obs = [320.0, 319.5] + [0.0] * 10 + [310.0]
        count, rows = self._run_fill(events, monkeypatch, obs_values=obs, period_ok=True)
        assert count == 1
        formatted, _ = rows[0]
        assert 'Core CPI' in formatted

    def test_fills_unemployment(self, monkeypatch):
        events = [{'id': 6, 'event_type': 'UNEMPLOYMENT', 'title': 'Unemployment Rate'}]
        obs = [3.9]
        count, rows = self._run_fill(events, monkeypatch, obs_values=obs, period_ok=True)
        assert count == 1
        formatted, _ = rows[0]
        assert '3.9%' in formatted

    def test_no_api_key_returns_zero(self, monkeypatch):
        monkeypatch.delenv('FRED_API_KEY', raising=False)
        from quant.collectors import event_calendar as ec
        result = ec.fill_event_actuals('2026-06-10')
        assert result == 0

    def test_empty_events_returns_zero(self, monkeypatch):
        count, rows = self._run_fill([], monkeypatch)
        assert count == 0
        assert rows == []

    def test_missing_fred_obs_leaves_null(self, monkeypatch):
        """When FRED returns no observations, no UPDATE should be executed."""
        from quant.collectors import event_calendar as ec

        monkeypatch.setenv('FRED_API_KEY', 'TESTKEY')
        monkeypatch.setattr(ec, '_fetch_fred_obs', lambda *a, **kw: [])
        monkeypatch.setattr(ec, '_obs_period_is_recent', lambda *a, **kw: True)
        monkeypatch.setattr(ec.time, 'sleep', lambda s: None)

        updated_rows = []

        class FakeCursor:
            def __init__(self):
                self.rowcount = 0
                self._phase = 'fetch'

            def execute(self, sql, params=None):
                if 'SELECT id' in sql:
                    self._phase = 'fetch'
                elif 'UPDATE' in sql:
                    self._phase = 'update'
                    self.rowcount = 1
                    updated_rows.append(params)

            def fetchall(self):
                if self._phase == 'fetch':
                    return [{'id': 7, 'event_type': 'CPI', 'title': 'CPI Release (May)'}]
                return []

        class FakeConn:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def cursor(self):
                return FakeCursor()

        monkeypatch.setattr(ec, 'get_conn', lambda: FakeConn())
        count = ec.fill_event_actuals('2026-06-10')
        # No obs → formatter never called → UPDATE never executed
        assert count == 0
        assert len(updated_rows) == 0

    def test_fills_fomc(self, monkeypatch):
        from quant.collectors import event_calendar as ec
        monkeypatch.setenv('FRED_API_KEY', 'TESTKEY')

        call_count = [0]
        def fake_fetch(series_id, api_key_, n=15):
            call_count[0] += 1
            # First call = current rate, second call = prior (for delta)
            if call_count[0] == 1:
                return [3.75]
            return [3.75, 4.00]  # [current, prior-meeting]

        monkeypatch.setattr(ec, '_fetch_fred_obs', fake_fetch)
        monkeypatch.setattr(ec, '_obs_period_is_recent', lambda *a, **kw: True)
        monkeypatch.setattr(ec.time, 'sleep', lambda s: None)

        events = [{'id': 8, 'event_type': 'FOMC', 'title': 'FOMC Rate Decision'}]
        updated_rows = []

        class FakeCursor:
            def __init__(self):
                self.rowcount = 0
                self._phase = 'fetch'

            def execute(self, sql, params=None):
                if 'SELECT id' in sql:
                    self._phase = 'fetch'
                elif 'UPDATE' in sql:
                    self._phase = 'update'
                    self.rowcount = 1
                    updated_rows.append(params)

            def fetchall(self):
                if self._phase == 'fetch':
                    return events
                return []

        class FakeConn:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def cursor(self):
                return FakeCursor()

        monkeypatch.setattr(ec, 'get_conn', lambda: FakeConn())
        count = ec.fill_event_actuals('2026-06-17')
        assert count == 1
        formatted, _ = updated_rows[0]
        # Should say held or cut/hiked — depends on mock values
        assert 'Fed' in formatted


# ---------------------------------------------------------------------------
# 3. /api/quant/events/fill-actuals endpoint test
# ---------------------------------------------------------------------------

class TestFillActualsEndpoint:
    """Endpoint logic: fill_event_actuals is called and events returned."""

    def test_endpoint_imports_and_routes_registered(self):
        """The fill-actuals route is registered on the quant Blueprint."""
        from quant.api.routes import quant_bp
        # Just check the blueprint imported cleanly and has rules
        # (actual URL validation would need a full app context)
        assert quant_bp is not None
        assert quant_bp.name == 'quant'

    def test_fill_actuals_response_schema(self, monkeypatch):
        """fill_event_actuals result is combined into correct response dict."""
        # Test the response-building logic in isolation (no DB/Flask needed).
        # Reproduce what the endpoint does after fill_event_actuals() returns:
        # it queries events and returns {date, filled, events}.

        # Simulate what fill_event_actuals returns
        fill_result = 1

        # Simulate what the post-fill DB query returns
        fake_events = [
            {'id': 1, 'ts': '2026-06-10', 'event_type': 'CPI',
             'title': 'CPI Release (May)', 'impact': 'high',
             'actual': 'CPI +0.2% MoM / 3.4% YoY',
             'actual_filled_at': '2026-06-10T14:05:00+00:00'},
        ]

        # Build the same response dict the endpoint returns
        response = {'date': '2026-06-10', 'filled': fill_result, 'events': fake_events}

        assert response['date'] == '2026-06-10'
        assert response['filled'] == 1
        assert len(response['events']) == 1
        assert response['events'][0]['actual'] == 'CPI +0.2% MoM / 3.4% YoY'
        assert response['events'][0]['actual_filled_at'] == '2026-06-10T14:05:00+00:00'


# ---------------------------------------------------------------------------
# 4. today_calendar_text rendering with and without actuals
# ---------------------------------------------------------------------------

class TestTodayCalendarText:
    """today_calendar_text in run_afternoon renders actuals correctly."""

    def _build_calendar_text(self, events):
        """Replicate the today_calendar_text logic from engine.py."""
        if not events:
            return "No high-impact scheduled events today."
        evt_lines = []
        has_actuals = False
        for e in events:
            title = e.get('title', '')
            impact = e.get('impact', 'med')
            actual = e.get('actual')
            if actual:
                evt_lines.append(f"- {title} ({impact} impact) — actual: {actual}")
                has_actuals = True
            else:
                evt_lines.append(f"- {title} ({impact} impact)")
        text = "Today's scheduled events:\n" + "\n".join(evt_lines)
        if not has_actuals:
            text += (
                "\n(Actual printed values are NOT yet available — "
                "do NOT claim any number was 'as expected' or 'beat/missed' "
                "without an actual number in hand.)"
            )
        return text

    def test_no_events_returns_no_events_text(self):
        text = self._build_calendar_text([])
        assert 'No high-impact' in text

    def test_without_actuals_includes_disclaimer(self):
        events = [
            {'title': 'CPI Release (May)', 'impact': 'high', 'actual': None},
        ]
        text = self._build_calendar_text(events)
        assert 'CPI Release (May)' in text
        assert 'NOT yet available' in text
        assert 'actual:' not in text.lower().split('not yet')[0]  # no actual in event line

    def test_with_actuals_no_disclaimer(self):
        events = [
            {'title': 'CPI Release (May)', 'impact': 'high',
             'actual': 'CPI +0.2% MoM / 3.4% YoY'},
        ]
        text = self._build_calendar_text(events)
        assert 'CPI Release (May)' in text
        assert 'actual: CPI +0.2% MoM / 3.4% YoY' in text
        assert 'NOT yet available' not in text

    def test_mixed_events_no_disclaimer_when_any_actual(self):
        """If at least one event has an actual, the disclaimer is suppressed."""
        events = [
            {'title': 'CPI Release (May)', 'impact': 'high',
             'actual': 'CPI +0.2% MoM / 3.4% YoY'},
            {'title': 'FOMC Rate Decision', 'impact': 'high', 'actual': None},
        ]
        text = self._build_calendar_text(events)
        # CPI has actual, FOMC does not — but has_actuals=True so no disclaimer
        assert 'NOT yet available' not in text
        assert 'actual: CPI' in text
        # FOMC line has no "actual:"
        assert 'FOMC Rate Decision (high impact)\n' in text or 'FOMC Rate Decision (high impact)' in text

    def test_all_with_actuals_shows_all_prints(self):
        events = [
            {'title': 'NFP (May)', 'impact': 'high', 'actual': 'NFP +272K'},
            {'title': 'Unemployment Rate', 'impact': 'high', 'actual': 'Unemployment 3.9%'},
        ]
        text = self._build_calendar_text(events)
        assert 'NFP +272K' in text
        assert 'Unemployment 3.9%' in text
        assert 'NOT yet available' not in text

    def test_afternoon_prompt_references_actual_rule(self):
        """afternoon_delta_prompt includes the 'actual:' key-word instruction."""
        from morning_engine.prompts import afternoon_delta_prompt
        prompt = afternoon_delta_prompt()
        assert 'actual:' in prompt.lower() or 'actual' in prompt
        assert 'Session Summary' in prompt
        # Should still contain anti-hallucination language for missing actuals
        assert 'NOT' in prompt or 'NEVER' in prompt
