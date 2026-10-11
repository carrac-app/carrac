"""
Tests for playbook analytics — per-setup expectancy across
journal + plays).

Covers:
  - compute_r_multiple: long win/loss, short win/loss, no-stop fallback,
    zero-risk (stop == entry) fallback
  - compute_pct_return: long, short, zero/None entry
  - bucket_label: exact boundary values for all 7 spec buckets
  - summarize_group: n always present, basis selection (never mixes R and
    pct within one group), win/loss/expectancy/profit_factor math,
    avg_mfe/avg_mae read only stored values with their own sample count,
    small_sample flag boundary at n=19/20
  - build_playbook_analytics: grouping by setup / archetype tag (multi-tag
    membership) / symbol / side / hour-of-day / day-of-week; tags as a
    JSON string vs an already-parsed list; malformed tags don't crash
  - MFE/MAE end-to-end: a real closed trade + a synthetic OHLCV window
    (same fixture pattern as tests/test_open_mfe_hook.py) flows through
    calculate_and_store_mfe_mae into get_playbook_analytics' avg_mfe/avg_mae
  - Tier-gate closed default: TIER_LIMITS has no 'journal_analytics' key
    yet anywhere, so `.get('journal_analytics', False)` is False for every
    tier; the Flask route and the trade_get_analytics MCP tool both honor
    that closed default (panel/field locked until a parallel branch turns
    the knob on).
"""

import json
from datetime import datetime

import pandas as pd
import pytest

import carrac.db_pool as _cdb
from carrac.journal.playbook_analytics import (
    DAY_NAMES,
    R_BUCKET_LABELS,
    SMALL_SAMPLE_N,
    build_playbook_analytics,
    bucket_label,
    compute_pct_return,
    compute_r_multiple,
    get_playbook_analytics,
    summarize_group,
)


# ---------------------------------------------------------------------------
# compute_r_multiple
# ---------------------------------------------------------------------------

class TestComputeRMultiple:
    def test_long_win(self):
        # entry=100, stop=90 -> risk=10; exit=120 -> pnl=20 -> R=2.0
        assert compute_r_multiple(100, 120, 90, 'long') == 2.0

    def test_long_loss(self):
        # entry=100, stop=90 -> risk=10; exit=85 -> pnl=-15 -> R=-1.5
        assert compute_r_multiple(100, 85, 90, 'long') == -1.5

    def test_short_win(self):
        # entry=100, stop=110 -> risk=10; exit=80 -> pnl=entry-exit=20 -> R=2.0
        assert compute_r_multiple(100, 80, 110, 'short') == 2.0

    def test_short_loss(self):
        # entry=100, stop=110 -> risk=10; exit=115 -> pnl=entry-exit=-15 -> R=-1.5
        assert compute_r_multiple(100, 115, 110, 'short') == -1.5

    def test_no_stop_returns_none(self):
        assert compute_r_multiple(100, 120, None, 'long') is None

    def test_zero_risk_stop_equals_entry_returns_none(self):
        assert compute_r_multiple(100, 120, 100, 'long') is None

    def test_missing_entry_or_exit_returns_none(self):
        assert compute_r_multiple(None, 120, 90, 'long') is None
        assert compute_r_multiple(100, None, 90, 'long') is None

    def test_direction_defaults_to_long(self):
        assert compute_r_multiple(100, 120, 90, None) == 2.0

    def test_direction_case_insensitive(self):
        assert compute_r_multiple(100, 80, 110, 'SHORT') == 2.0

    def test_bad_stop_side_is_robust_via_abs_risk(self):
        # Long trade with a mis-recorded stop ABOVE entry: risk is still
        # abs(entry - stop), matching carrac.content_db.log_trade_close's
        # behaviour (robust to bad data rather than raising).
        assert compute_r_multiple(100, 120, 110, 'long') == 2.0


# ---------------------------------------------------------------------------
# compute_pct_return
# ---------------------------------------------------------------------------

class TestComputePctReturn:
    def test_long_positive(self):
        assert compute_pct_return(100, 105, 'long') == pytest.approx(0.05)

    def test_long_negative(self):
        assert compute_pct_return(100, 95, 'long') == pytest.approx(-0.05)

    def test_short_positive(self):
        assert compute_pct_return(100, 95, 'short') == pytest.approx(0.05)

    def test_short_negative(self):
        assert compute_pct_return(100, 105, 'short') == pytest.approx(-0.05)

    def test_none_entry_returns_none(self):
        assert compute_pct_return(None, 105, 'long') is None

    def test_zero_entry_returns_none(self):
        assert compute_pct_return(0, 105, 'long') is None

    def test_none_exit_returns_none(self):
        assert compute_pct_return(100, None, 'long') is None


# ---------------------------------------------------------------------------
# bucket_label — exact boundary values for the 7 spec buckets
# ---------------------------------------------------------------------------

class TestBucketLabel:
    @pytest.mark.parametrize('value,expected', [
        (-5.0, '<=-2R'),
        (-2.0, '<=-2R'),      # boundary: exactly -2 is inclusive in the bottom bucket
        (-1.999, '-2..-1'),
        (-1.0, '-2..-1'),     # boundary
        (-0.999, '-1..0'),
        (0.0, '-1..0'),       # boundary
        (0.001, '0..1'),
        (1.0, '0..1'),        # boundary
        (1.001, '1..2'),
        (2.0, '1..2'),        # boundary
        (2.001, '2..3'),
        (3.0, '2..3'),        # boundary
        (3.001, '>3R'),
        (10.0, '>3R'),
    ])
    def test_boundaries(self, value, expected):
        assert bucket_label(value) == expected

    def test_none_returns_none(self):
        assert bucket_label(None) is None

    def test_all_labels_are_the_spec_set(self):
        assert R_BUCKET_LABELS == ['<=-2R', '-2..-1', '-1..0', '0..1', '1..2', '2..3', '>3R']


# ---------------------------------------------------------------------------
# summarize_group
# ---------------------------------------------------------------------------

def _t(entry=100, exit_price=110, stop=90, direction='long', mfe=None, mae=None,
       closed_at='2026-06-01T12:00:00', opened_at='2026-06-01T10:00:00',
       setup_type=None, tags=None, asset='BTC'):
    return {
        'id': 1, 'asset': asset, 'direction': direction,
        'entry_price': entry, 'exit_price': exit_price, 'stop_price': stop,
        'opened_at': opened_at, 'closed_at': closed_at,
        'setup_type': setup_type, 'tags': tags,
        'mfe': mfe, 'mae': mae,
    }


class TestSummarizeGroup:
    def test_n_always_present_even_with_no_metric_values(self):
        # Every trade lacks entry_price -> no metric values, but n must
        # still reflect the group size (spec: "n ALWAYS displayed").
        trades = [dict(_t(), entry_price=None), dict(_t(), entry_price=None)]
        g = summarize_group(trades)
        assert g['n'] == 2
        assert g['scored_n'] == 0
        assert g['win_rate'] is None
        assert g['expectancy'] is None

    def test_empty_group_returns_none(self):
        assert summarize_group([]) is None

    def test_all_stops_present_uses_r_basis(self):
        trades = [_t(entry=100, exit_price=120, stop=90),
                  _t(entry=100, exit_price=85, stop=90)]
        g = summarize_group(trades)
        assert g['basis'] == 'r'

    def test_one_missing_stop_drops_whole_group_to_pct(self):
        # Never mixes R and percent numbers within one group: even though
        # trade #1 HAS a stop, trade #2 doesn't, so the whole group must
        # report on the percent basis.
        trades = [_t(entry=100, exit_price=120, stop=90),
                  _t(entry=100, exit_price=110, stop=None)]
        g = summarize_group(trades)
        assert g['basis'] == 'pct'
        # Confirm trade #1's contribution was recomputed as pct, not R:
        # (120-100)/100 = 0.20 and (110-100)/100 = 0.10 -> mean = 0.15
        assert g['expectancy'] == pytest.approx(0.15)

    def test_win_rate_and_expectancy(self):
        # R values: +2.0, -1.5, +1.0 -> 2 wins / 3 = 66.7%; mean = 0.5
        trades = [
            _t(entry=100, exit_price=120, stop=90),   # R = +2.0
            _t(entry=100, exit_price=85, stop=90),     # R = -1.5
            _t(entry=100, exit_price=110, stop=90),    # R = +1.0
        ]
        g = summarize_group(trades)
        assert g['win_rate'] == pytest.approx(66.7, abs=0.1)
        assert g['expectancy'] == pytest.approx(0.5)

    def test_profit_factor(self):
        # gross_win = 2.0 + 1.0 = 3.0, gross_loss = 1.5 -> PF = 2.0
        trades = [
            _t(entry=100, exit_price=120, stop=90),
            _t(entry=100, exit_price=85, stop=90),
            _t(entry=100, exit_price=110, stop=90),
        ]
        g = summarize_group(trades)
        assert g['profit_factor'] == pytest.approx(2.0)

    def test_profit_factor_none_when_no_losses(self):
        trades = [_t(entry=100, exit_price=120, stop=90)]
        g = summarize_group(trades)
        assert g['profit_factor'] is None  # gross_loss == 0 -> undefined, not inf

    def test_avg_win_avg_loss(self):
        trades = [
            _t(entry=100, exit_price=120, stop=90),   # R = +2.0
            _t(entry=100, exit_price=130, stop=90),   # R = +3.0
            _t(entry=100, exit_price=85, stop=90),    # R = -1.5
        ]
        g = summarize_group(trades)
        assert g['avg_win'] == pytest.approx(2.5)
        assert g['avg_loss'] == pytest.approx(1.5)

    def test_breakeven_counts_as_non_win(self):
        # Genuine breakeven: exit == entry -> R = 0.0 exactly -> not a win
        # (strict > 0), falls in the losses bucket.
        trades = [_t(entry=100, exit_price=100, stop=90)]
        g = summarize_group(trades)
        assert g['win_rate'] == 0.0

    def test_avg_mfe_avg_mae_only_use_present_values_with_own_count(self):
        trades = [
            _t(entry=100, exit_price=120, stop=90, mfe=2.5, mae=0.5),
            _t(entry=100, exit_price=110, stop=90, mfe=None, mae=None),  # not computed yet
            _t(entry=100, exit_price=105, stop=90, mfe=1.5, mae=0.2),
        ]
        g = summarize_group(trades)
        assert g['n'] == 3
        assert g['avg_mfe'] == pytest.approx(2.0)   # mean of 2.5, 1.5
        assert g['avg_mfe_n'] == 2
        assert g['avg_mae'] == pytest.approx(0.35)  # mean of 0.5, 0.2
        assert g['avg_mae_n'] == 2

    def test_r_distribution_sums_to_scored_n(self):
        trades = [
            _t(entry=100, exit_price=120, stop=90),   # R=+2.0 -> '1..2'
            _t(entry=100, exit_price=85, stop=90),     # R=-1.5 -> '-2..-1'
        ]
        g = summarize_group(trades)
        total = sum(b['count'] for b in g['r_distribution'])
        assert total == g['scored_n'] == 2
        by_label = {b['bucket']: b['count'] for b in g['r_distribution']}
        assert by_label['1..2'] == 1
        assert by_label['-2..-1'] == 1

    def test_small_sample_flag_boundary(self):
        below = [_t() for _ in range(SMALL_SAMPLE_N - 1)]
        at = [_t() for _ in range(SMALL_SAMPLE_N)]
        assert summarize_group(below)['small_sample'] is True
        assert summarize_group(at)['small_sample'] is False


# ---------------------------------------------------------------------------
# build_playbook_analytics — grouping dimensions
# ---------------------------------------------------------------------------

class TestBuildPlaybookAnalytics:
    def test_ignores_open_trades(self):
        trades = [_t(closed_at=None, exit_price=None)]
        result = build_playbook_analytics(trades)
        assert result['total_closed_trades'] == 0

    def test_groups_by_setup_type_and_untagged_bucket(self):
        trades = [
            _t(setup_type='breakout'),
            _t(setup_type='breakout'),
            _t(setup_type=None),  # -> 'untagged'
        ]
        result = build_playbook_analytics(trades)
        assert result['by_setup']['breakout']['n'] == 2
        assert result['by_setup']['untagged']['n'] == 1

    def test_multi_tag_trade_appears_in_every_tag_group(self):
        trades = [
            _t(tags=json.dumps(['src:agent', 'strat:bb_breakdown'])),
            _t(tags=json.dumps(['strat:bb_breakdown'])),
        ]
        result = build_playbook_analytics(trades)
        assert result['by_tag']['src:agent']['n'] == 1
        assert result['by_tag']['strat:bb_breakdown']['n'] == 2

    def test_tags_accepts_already_parsed_list(self):
        trades = [_t(tags=['src:agent'])]
        result = build_playbook_analytics(trades)
        assert result['by_tag']['src:agent']['n'] == 1

    def test_malformed_tags_do_not_crash(self):
        trades = [_t(tags='not valid json'), _t(tags=json.dumps({'a': 1})), _t(tags=None)]
        result = build_playbook_analytics(trades)
        # None of the 3 trades contribute a tag group; no crash.
        assert result['by_tag'] == {}
        assert result['total_closed_trades'] == 3

    def test_groups_by_symbol_uppercased(self):
        trades = [_t(asset='btc'), _t(asset='BTC')]
        result = build_playbook_analytics(trades)
        assert result['by_symbol']['BTC']['n'] == 2

    def test_groups_by_side(self):
        trades = [_t(direction='long'), _t(direction='short'), _t(direction='long')]
        result = build_playbook_analytics(trades)
        assert result['by_side']['long']['n'] == 2
        assert result['by_side']['short']['n'] == 1

    def test_groups_by_hour_of_day(self):
        trades = [
            _t(opened_at='2026-06-01T09:15:00'),
            _t(opened_at='2026-06-01T09:45:00'),
            _t(opened_at='2026-06-01T14:00:00'),
        ]
        result = build_playbook_analytics(trades)
        assert result['by_hour_of_day']['09:00']['n'] == 2
        assert result['by_hour_of_day']['14:00']['n'] == 1

    def test_groups_by_day_of_week(self):
        dt = datetime(2026, 6, 1, 9, 0, 0)  # ground truth via datetime.weekday()
        expected_day = DAY_NAMES[dt.weekday()]
        trades = [_t(opened_at=dt.isoformat())]
        result = build_playbook_analytics(trades)
        assert result['by_day_of_week'][expected_day]['n'] == 1

    def test_day_of_week_ordering_follows_day_names(self):
        # Insert out of order — output key order must follow DAY_NAMES.
        trades = [
            _t(opened_at='2026-06-07T09:00:00'),  # a Sunday
            _t(opened_at='2026-06-01T09:00:00'),  # a Monday
        ]
        result = build_playbook_analytics(trades)
        assert list(result['by_day_of_week'].keys()) == ['Mon', 'Sun']


# ---------------------------------------------------------------------------
# MFE/MAE end-to-end: synthetic OHLCV window -> stored R -> group average
# (mirrors the mocking pattern in tests/test_open_mfe_hook.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    _cdb._db_type = None
    monkeypatch.setenv('DB_TYPE', 'sqlite')
    from carrac.content_db import ContentDatabase
    return ContentDatabase(db_path=str(tmp_path / 'test_playbook.db'))


def _fake_ohlcv_db(highs, lows):
    from unittest.mock import MagicMock
    mock_db = MagicMock()
    mock_db.fetch_klines_for_range.return_value = pd.DataFrame({'high': highs, 'low': lows})
    return mock_db


class TestMfeMaeSyntheticWindow:
    def test_avg_mfe_avg_mae_reflect_persisted_synthetic_window(self, db, monkeypatch):
        from carrac.journal.mfe_mae import calculate_and_store_mfe_mae

        tid = db.log_trade_open(
            user_id=1, asset='BTC', timeframe='1D', direction='long',
            entry_price=65000.0, stop_price=63000.0, target_price=70000.0,
            setup_type='breakout',
        )
        db.log_trade_close(tid, exit_price=67000.0, exit_reason='manual')

        with monkeypatch.context() as m:
            import database
            m.setattr(database, 'OHLCVDatabase', lambda: _fake_ohlcv_db(
                [65500, 67000, 66000],   # highs -> MFE price = 67000 -> MFE R = 1.0
                [64500, 63500, 64000],   # lows  -> MAE price = 63500 -> MAE R = 0.75
            ))
            result = calculate_and_store_mfe_mae(db, tid)

        assert 'error' not in result
        assert result['mfe_r'] == 1.0
        assert result['mae_r'] == 0.75

        analytics = get_playbook_analytics(db, user_id=1, days=90)
        setup_group = analytics['by_setup']['breakout']
        assert setup_group['n'] == 1
        assert setup_group['avg_mfe'] == pytest.approx(1.0)
        assert setup_group['avg_mfe_n'] == 1
        assert setup_group['avg_mae'] == pytest.approx(0.75)
        assert setup_group['avg_mae_n'] == 1


# ---------------------------------------------------------------------------
# Tier-gate closed default (`.get('journal_analytics',
# False)` — the knob is owned by a parallel branch; this
# module must never assume it exists).
# ---------------------------------------------------------------------------

class TestTierGateClosedDefault:
    # Post-launch matrix: journal_analytics is a paid knob — locked for
    # free, open for every paid tier (defined in TIER_LIMITS, read with a
    # closed .get default so the feature stays dark if the knob vanishes).
    @pytest.mark.parametrize('tier,expected', [
        ('free', False), ('member', True), ('tester', True),
        ('premium', True), ('admin', True),
    ])
    def test_journal_analytics_matrix(self, tier, expected):
        from carrac.auth.decorators import get_tier_limits
        limits = get_tier_limits(tier)
        assert limits.get('journal_analytics', False) is expected

    def test_route_unlocked_for_member_tier(self, auth_client):
        # auth_client session tier == 'member' (see conftest.auth_client) —
        # unlocked since the launch matrix added the journal_analytics knob.
        resp = auth_client.get('/api/journal/analytics/playbook')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get('locked') is not True
        assert 'by_setup' in data

    def test_mcp_tool_playbook_field_locked_by_default(self, tmp_path, monkeypatch, bind_mcp_user):
        _cdb._db_type = None
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        from carrac import content_db as cdb_mod
        from carrac.content_db import ContentDatabase
        from carrac.mcp.registry import TOOL_REGISTRY
        from carrac.mcp import tools  # noqa: F401 — registers all tools

        test_db = ContentDatabase(db_path=str(tmp_path / 'mcp_playbook.db'))
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: test_db)

        fn = TOOL_REGISTRY['trade_get_analytics']['handler']
        result = fn({})
        # Existing top-level fields are untouched (backward compatible).
        assert 'trades' in result
        # New additive field, locked by the closed default.
        assert result['playbook']['locked'] is True
        assert 'reason' in result['playbook']


class TestKnobRemovedFailsClosed:
    def test_journal_analytics_knob_removed_locks_route(self, auth_client, monkeypatch):
        """The .get(key, False) closed default must survive the knob being
        deleted from TIER_LIMITS entirely."""
        from carrac.auth import decorators as dec
        stripped = {t: {k: v for k, v in lim.items() if k != 'journal_analytics'}
                    for t, lim in dec.TIER_LIMITS.items()}
        monkeypatch.setattr(dec, 'TIER_LIMITS', stripped)
        resp = auth_client.get('/api/journal/analytics/playbook')
        assert resp.status_code == 200
        assert resp.get_json()['locked'] is True
