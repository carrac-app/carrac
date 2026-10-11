"""
Tests for Plays Monitor — condition evaluation, auto-transitions, state persistence.
"""

import pytest
from datetime import datetime, timezone, timedelta


@pytest.fixture(autouse=True)
def _reset_sqlite_conn():
    """Reset SQLite thread-local connection between tests."""
    import carrac.db_pool as _cdb
    if hasattr(_cdb._sqlite_local, 'conn') and _cdb._sqlite_local.conn is not None:
        try:
            _cdb._sqlite_local.conn.close()
        except Exception:
            pass
        _cdb._sqlite_local.conn = None
    yield
    if hasattr(_cdb._sqlite_local, 'conn') and _cdb._sqlite_local.conn is not None:
        try:
            _cdb._sqlite_local.conn.close()
        except Exception:
            pass
        _cdb._sqlite_local.conn = None


class TestPlaysMonitorEvaluation:
    """Condition evaluation via the monitor."""

    def test_evaluate_play_price_level_met(self, app):
        """Price above condition evaluates as met when price is above target."""
        from carrac.plays.monitor import PlaysMonitor

        monitor = PlaysMonitor()
        play = {
            'id': 1,
            'user_id': 'test',
            'direction': 'long',
            'conditions': {
                'rules': [{
                    'id': 'r1',
                    'type': 'price_level',
                    'params': {'level': 50000, 'relation': 'above'},
                    'met': False,
                }],
                'logic': 'all',
            },
            'invalidation': None,
            'expiry': None,
        }
        market_data = {
            'price': 55000,
            'asset_data': {'realtime': {'price': 55000}},
            'all_assets': {},
        }

        result = monitor.play_evaluate(play, market_data)
        rules = result['conditions_result']['rules']
        assert len(rules) == 1
        assert rules[0]['met'] is True
        assert result['conditions_result']['all_met'] is True

    def test_evaluate_play_price_level_not_met(self, app):
        """Price above condition evaluates as not met when price is below target."""
        from carrac.plays.monitor import PlaysMonitor

        monitor = PlaysMonitor()
        play = {
            'id': 1,
            'user_id': 'test',
            'direction': 'long',
            'conditions': {
                'rules': [{
                    'id': 'r1',
                    'type': 'price_level',
                    'params': {'level': 50000, 'relation': 'above'},
                    'met': False,
                }],
                'logic': 'all',
            },
            'invalidation': None,
            'expiry': None,
        }
        market_data = {
            'price': 45000,
            'asset_data': {'realtime': {'price': 45000}},
            'all_assets': {},
        }

        result = monitor.play_evaluate(play, market_data)
        assert result['conditions_result']['rules'][0]['met'] is False
        assert result['conditions_result']['all_met'] is False

    def test_evaluate_play_no_conditions(self, app):
        """Play with no conditions returns empty result."""
        from carrac.plays.monitor import PlaysMonitor

        monitor = PlaysMonitor()
        play = {
            'id': 1,
            'user_id': 'test',
            'direction': 'long',
            'conditions': {'rules': [], 'logic': 'all'},
            'invalidation': None,
            'expiry': None,
        }
        market_data = {'price': 50000, 'asset_data': {}, 'all_assets': {}}

        result = monitor.play_evaluate(play, market_data)
        assert result['conditions_result']['met_count'] == 0
        assert result['conditions_result']['total_count'] == 0
        assert result['invalidation_triggered'] is False

    def test_evaluate_play_multiple_conditions(self, app):
        """Multiple conditions — partial met."""
        from carrac.plays.monitor import PlaysMonitor

        monitor = PlaysMonitor()
        play = {
            'id': 1,
            'user_id': 'test',
            'direction': 'long',
            'conditions': {
                'rules': [
                    {'id': 'r1', 'type': 'price_level', 'params': {'level': 50000, 'relation': 'above'}, 'met': False},
                    {'id': 'r2', 'type': 'price_level', 'params': {'level': 60000, 'relation': 'above'}, 'met': False},
                ],
                'logic': 'all',
            },
            'invalidation': None,
            'expiry': None,
        }
        market_data = {'price': 55000, 'asset_data': {'realtime': {'price': 55000}}, 'all_assets': {}}

        result = monitor.play_evaluate(play, market_data)
        assert result['conditions_result']['met_count'] == 1
        assert result['conditions_result']['total_count'] == 2
        assert result['conditions_result']['all_met'] is False


class TestPlaysMonitorInvalidation:
    """Invalidation condition triggers status transition."""

    def test_invalidation_triggers_transition(self, app):
        """When invalidation condition is met, play should transition to invalidated."""
        from carrac.plays.monitor import PlaysMonitor

        monitor = PlaysMonitor()
        play = {
            'id': 1,
            'user_id': 'test',
            'status': 'watching',
            'direction': 'long',
            'conditions': {'rules': [], 'logic': 'all'},
            'invalidation': {
                'id': 'inv1',
                'type': 'price_level',
                'params': {'level': 40000, 'relation': 'below'},
                'met': False,
            },
            'expiry': None,
        }
        market_data = {'price': 38000, 'asset_data': {'realtime': {'price': 38000}}, 'all_assets': {}}

        result = monitor.play_evaluate(play, market_data)
        assert result['invalidation_triggered'] is True
        assert result['invalidation']['met'] is True

        new_status = monitor._compute_transition(play, result)
        assert new_status == 'invalidated'

    def test_invalidation_not_met(self, app):
        """When invalidation condition is not met, no transition."""
        from carrac.plays.monitor import PlaysMonitor

        monitor = PlaysMonitor()
        play = {
            'id': 1,
            'user_id': 'test',
            'status': 'watching',
            'direction': 'long',
            'conditions': {'rules': [], 'logic': 'all'},
            'invalidation': {
                'id': 'inv1',
                'type': 'price_level',
                'params': {'level': 40000, 'relation': 'below'},
                'met': False,
            },
            'expiry': None,
        }
        market_data = {'price': 55000, 'asset_data': {'realtime': {'price': 55000}}, 'all_assets': {}}

        result = monitor.play_evaluate(play, market_data)
        assert result['invalidation_triggered'] is False
        new_status = monitor._compute_transition(play, result)
        assert new_status is None

    def test_invalidation_on_acting_play(self, app):
        """Invalidation also works on acting plays."""
        from carrac.plays.monitor import PlaysMonitor

        monitor = PlaysMonitor()
        play = {
            'id': 1,
            'user_id': 'test',
            'status': 'acting',
            'direction': 'long',
            'conditions': {'rules': [], 'logic': 'all'},
            'invalidation': {
                'id': 'inv1',
                'type': 'price_level',
                'params': {'level': 40000, 'relation': 'below'},
                'met': False,
            },
            'expiry': None,
        }
        market_data = {'price': 38000, 'asset_data': {'realtime': {'price': 38000}}, 'all_assets': {}}

        result = monitor.play_evaluate(play, market_data)
        new_status = monitor._compute_transition(play, result)
        assert new_status == 'invalidated'


class TestPlaysMonitorExpiry:
    """Expiry auto-transition."""

    def test_expiry_triggers_expired(self, app):
        """Past expiry date transitions watching play to expired."""
        from carrac.plays.monitor import PlaysMonitor

        monitor = PlaysMonitor()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        play = {
            'id': 1,
            'user_id': 'test',
            'status': 'watching',
            'direction': 'long',
            'conditions': {'rules': [], 'logic': 'all'},
            'invalidation': None,
            'expiry': past,
        }
        market_data = {'price': 50000, 'asset_data': {}, 'all_assets': {}}

        result = monitor.play_evaluate(play, market_data)
        assert result['expired'] is True
        new_status = monitor._compute_transition(play, result)
        assert new_status == 'expired'

    def test_future_expiry_no_transition(self, app):
        """Future expiry date does not trigger transition."""
        from carrac.plays.monitor import PlaysMonitor

        monitor = PlaysMonitor()
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        play = {
            'id': 1,
            'user_id': 'test',
            'status': 'watching',
            'direction': 'long',
            'conditions': {'rules': [], 'logic': 'all'},
            'invalidation': None,
            'expiry': future,
        }
        market_data = {'price': 50000, 'asset_data': {}, 'all_assets': {}}

        result = monitor.play_evaluate(play, market_data)
        assert result['expired'] is False
        new_status = monitor._compute_transition(play, result)
        assert new_status is None

    def test_acting_play_does_not_expire(self, app):
        """Acting plays don't auto-expire."""
        from carrac.plays.monitor import PlaysMonitor

        monitor = PlaysMonitor()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        play = {
            'id': 1,
            'user_id': 'test',
            'status': 'acting',
            'direction': 'long',
            'conditions': {'rules': [], 'logic': 'all'},
            'invalidation': None,
            'expiry': past,
        }
        market_data = {'price': 50000, 'asset_data': {}, 'all_assets': {}}

        result = monitor.play_evaluate(play, market_data)
        assert result['expired'] is True
        new_status = monitor._compute_transition(play, result)
        assert new_status is None  # Acting plays don't auto-expire


class TestPlaysMonitorTransitions:
    """Auto-transition edge cases."""

    def test_no_auto_transition_to_acting(self, app):
        """All conditions met does NOT auto-transition to acting."""
        from carrac.plays.monitor import PlaysMonitor

        monitor = PlaysMonitor()
        play = {
            'id': 1,
            'user_id': 'test',
            'status': 'watching',
            'direction': 'long',
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'price_level', 'params': {'level': 50000, 'relation': 'above'}, 'met': False}],
                'logic': 'all',
            },
            'invalidation': None,
            'expiry': None,
        }
        market_data = {'price': 55000, 'asset_data': {'realtime': {'price': 55000}}, 'all_assets': {}}

        result = monitor.play_evaluate(play, market_data)
        assert result['conditions_result']['all_met'] is True
        new_status = monitor._compute_transition(play, result)
        assert new_status is None  # Manual only

    def test_invalidation_takes_priority_over_expiry(self, app):
        """If both invalidation and expiry fire, invalidation wins."""
        from carrac.plays.monitor import PlaysMonitor

        monitor = PlaysMonitor()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        play = {
            'id': 1,
            'user_id': 'test',
            'status': 'watching',
            'direction': 'long',
            'conditions': {'rules': [], 'logic': 'all'},
            'invalidation': {
                'id': 'inv1',
                'type': 'price_level',
                'params': {'level': 40000, 'relation': 'below'},
                'met': False,
            },
            'expiry': past,
        }
        market_data = {'price': 38000, 'asset_data': {'realtime': {'price': 38000}}, 'all_assets': {}}

        result = monitor.play_evaluate(play, market_data)
        new_status = monitor._compute_transition(play, result)
        assert new_status == 'invalidated'  # Not expired

    def test_build_resolution_invalidated(self, app):
        from carrac.plays.monitor import PlaysMonitor
        monitor = PlaysMonitor()
        result = {
            'invalidation': {'evidence': 'Price $38K below $40K'},
            'conditions_result': {},
        }
        res = monitor._build_resolution('invalidated', result)
        assert 'Auto-invalidated' in res
        assert '$38K' in res

    def test_build_resolution_expired(self, app):
        from carrac.plays.monitor import PlaysMonitor
        monitor = PlaysMonitor()
        result = {'invalidation': None, 'conditions_result': {}}
        res = monitor._build_resolution('expired', result)
        assert 'Expiry' in res
