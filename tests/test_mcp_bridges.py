"""Tests for bridge tools (drawing alerts, formula→play, alert→play)."""

import pytest

from carrac.mcp import tools  # noqa
from carrac.mcp.registry import TOOL_REGISTRY
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools.bridges import (
    _alert_to_draft, _extract_trendline_points, list_formula_alerts,
)


@pytest.fixture(autouse=True)
def _bind_user(bind_mcp_user):
    """Every test in this file calls tool handlers directly; bind a per-user id
    so the shared-key guard in get_mcp_user_id() doesn't fire."""
    return bind_mcp_user


class TestExtractTrendlinePoints:
    def test_returns_none_when_no_metadata(self):
        assert _extract_trendline_points({}) is None

    def test_returns_none_when_metadata_lacks_points(self):
        assert _extract_trendline_points({'metadata': {'foo': 'bar'}}) is None

    def test_extracts_from_dict_metadata(self):
        d = {'metadata': {'points': [
            {'ts': 1700000000000, 'val': 50000},
            {'ts': 1700100000000, 'val': 51000},
        ]}}
        out = _extract_trendline_points(d)
        assert out == (1700000000000.0, 50000.0, 1700100000000.0, 51000.0)

    def test_extracts_from_json_string_metadata(self):
        d = {'metadata': '{"points":[{"timestamp":1,"price":10},{"timestamp":2,"price":20}]}'}
        out = _extract_trendline_points(d)
        assert out == (1.0, 10.0, 2.0, 20.0)


class TestAlertToDraft:
    def test_price_alert(self):
        a = {'id': 1, 'alert_type': 'price', 'symbol': 'BTC',
             'condition': 'above', 'price': 50000}
        d = _alert_to_draft(a)
        assert d['has_supported_rules'] is True
        assert d['conditions']['rules'][0]['type'] == 'price_level'
        assert d['conditions']['rules'][0]['params']['level'] == 50000

    def test_formula_alert(self):
        a = {'id': 2, 'alert_type': 'formula', 'symbol': 'ETH',
             'expression': 'RSI(14) < 30', 'timeframe': '4h'}
        d = _alert_to_draft(a)
        assert d['has_supported_rules'] is True
        assert d['conditions']['rules'][0]['type'] == 'formula_expression'

    def test_indicator_alert(self):
        a = {'id': 3, 'alert_type': 'indicator', 'symbol': 'SOL',
             'indicator': 'rsi', 'target_state': 'oversold', 'timeframe': '1h'}
        d = _alert_to_draft(a)
        assert d['conditions']['rules'][0]['type'] == 'indicator_state'

    def test_derivatives_regime_alert(self):
        a = {'id': 4, 'alert_type': 'derivatives_regime',
             'to_regime': 'distribution_topping'}
        d = _alert_to_draft(a)
        assert d['conditions']['rules'][0]['type'] == 'derivatives_regime'

    def test_unsupported_alert_returns_empty(self):
        a = {'id': 5, 'alert_type': 'chain_strength_breach', 'chain': 'risk_appetite'}
        d = _alert_to_draft(a)
        assert d['has_supported_rules'] is False
        assert d['conditions']['rules'] == []


class TestCreateDrawingAlert:
    def test_registered(self):
        assert 'alert_create_from_drawing' in TOOL_REGISTRY

    def test_missing_drawing_id_raises(self):
        fn = TOOL_REGISTRY['alert_create_from_drawing']['handler']
        with pytest.raises(ToolError):
            fn({})

    def test_no_notification_channel_blocks(self, app, monkeypatch):
        """A valid drawing with no channel configured must still
        raise the channel error — the reorder only changes what a BAD id
        reports, not the channel requirement itself."""
        import push_notifications
        from carrac import content_db as cdb_mod

        monkeypatch.setattr(push_notifications, 'user_has_notification_channel', lambda u: False)

        class FakeDB:
            def get_drawing_by_id(self, did, user_id=None):
                if user_id is not None and int(user_id) != 42:
                    return None
                return {
                    'id': did, 'asset': 'BTC', 'drawing_type': 'horizontal_line',
                    'price_level': 50000, 'metadata': None, 'user_id': 42,
                }

        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: FakeDB())

        fn = TOOL_REGISTRY['alert_create_from_drawing']['handler']
        with app.app_context():
            with pytest.raises(ToolError) as exc:
                fn({'drawing_id': 1})
        assert 'notification channel' in str(exc.value)

    def test_no_notification_channel_message_names_mcp_tool(self, app, monkeypatch):
        """The channel error must name notification_configure so
        an agent can fix it in-session instead of only pointing at the web UI."""
        import push_notifications
        from carrac import content_db as cdb_mod

        monkeypatch.setattr(push_notifications, 'user_has_notification_channel', lambda u: False)

        class FakeDB:
            def get_drawing_by_id(self, did, user_id=None):
                return {
                    'id': did, 'asset': 'BTC', 'drawing_type': 'horizontal_line',
                    'price_level': 50000, 'metadata': None, 'user_id': 42,
                }

        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: FakeDB())

        fn = TOOL_REGISTRY['alert_create_from_drawing']['handler']
        with app.app_context():
            with pytest.raises(ToolError) as exc:
                fn({'drawing_id': 1})
        assert 'notification_configure' in str(exc.value)

    def test_bad_drawing_id_reports_missing_drawing_not_channel(self, app, monkeypatch):
        """alert_create_from_drawing(
        drawing_id=999999) must not say 'No notification channel configured'
        when the real problem is a bad id. It must report the
        drawing miss, and the channel state must not even be consulted."""
        import push_notifications
        from carrac import content_db as cdb_mod

        channel_checked = []
        monkeypatch.setattr(
            push_notifications, 'user_has_notification_channel',
            lambda u: channel_checked.append(u) or False,
        )

        class FakeDB:
            def get_drawing_by_id(self, did, user_id=None):
                return None  # nothing exists for any id

        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: FakeDB())

        fn = TOOL_REGISTRY['alert_create_from_drawing']['handler']
        with app.app_context():
            with pytest.raises(ToolError) as exc:
                fn({'drawing_id': 999999})

        msg = str(exc.value)
        assert 'not found' in msg.lower()
        assert 'channel' not in msg.lower()
        assert channel_checked == []  # the channel check never ran

    def test_horizontal_line_creates_two_alerts(self, app, monkeypatch):
        import push_notifications
        from carrac import content_db as cdb_mod

        monkeypatch.setattr(push_notifications, 'user_has_notification_channel', lambda u: True)

        # The signature and the owner filter both mirror the real
        # get_drawing_by_id(drawing_id, user_id=None). A fake that took
        # only `did` and returned a row for anybody would let a missing
        # ownership check pass this suite.
        class FakeDB:
            def get_drawing_by_id(self, did, user_id=None):
                if user_id is not None and int(user_id) != 42:
                    return None
                return {
                    'id': did, 'asset': 'BTC', 'drawing_type': 'horizontal_line',
                    'price_level': 50000, 'metadata': None, 'user_id': 42,
                }

        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: FakeDB())

        created = []
        monkeypatch.setattr(
            push_notifications, 'create_alert',
            lambda **kw: created.append(kw) or {'id': len(created), **kw},
        )

        fn = TOOL_REGISTRY['alert_create_from_drawing']['handler']
        with app.app_context():
            out = fn({'drawing_id': 42})
        assert out['count'] == 2
        assert {c['condition'] for c in created} == {'above', 'below'}

    def test_unsupported_type_raises(self, app, monkeypatch):
        import push_notifications
        from carrac import content_db as cdb_mod

        monkeypatch.setattr(push_notifications, 'user_has_notification_channel', lambda u: True)

        class FakeDB:  # Signature mirrors the real helper
            def get_drawing_by_id(self, did, user_id=None):
                if user_id is not None and int(user_id) != 42:
                    return None
                return {'id': did, 'asset': 'BTC', 'drawing_type': 'annotation',
                        'price_level': None, 'metadata': None, 'user_id': 42}

        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: FakeDB())

        fn = TOOL_REGISTRY['alert_create_from_drawing']['handler']
        with app.app_context():
            with pytest.raises(ToolError) as exc:
                fn({'drawing_id': 99})
        assert 'not alertable' in str(exc.value)


class TestListFormulaAlerts:
    """list_formula_alerts is a plain helper now, absorbed into
    alert_manage (action=list, family=formula) — no longer a standalone
    registry entry, so it's called directly here instead of via TOOL_REGISTRY."""

    def test_no_longer_a_standalone_tool(self):
        assert 'list_formula_alerts' not in TOOL_REGISTRY

    def test_absorbed_into_alert_manage(self):
        assert 'alert_manage' in TOOL_REGISTRY

    def test_filters_by_type(self, app, monkeypatch):
        import push_notifications
        monkeypatch.setattr(
            push_notifications, 'get_user_alerts',
            lambda u, active_only=True: [
                {'id': 1, 'alert_type': 'price', 'symbol': 'BTC'},
                {'id': 2, 'alert_type': 'formula', 'symbol': 'ETH',
                 'expression': 'RSI(14) < 30'},
                {'id': 3, 'alert_type': 'formula', 'symbol': 'SOL',
                 'expression': 'EMA(close, 9)'},
            ],
        )
        with app.app_context():
            out = list_formula_alerts({})
        assert out['count'] == 2
        assert {a['id'] for a in out['alerts']} == {2, 3}

    def test_reachable_via_alert_manage_family_filter(self, app, monkeypatch):
        import push_notifications
        monkeypatch.setattr(
            push_notifications, 'get_user_alerts',
            lambda u, active_only=True: [
                {'id': 1, 'alert_type': 'price', 'symbol': 'BTC'},
                {'id': 2, 'alert_type': 'formula', 'symbol': 'ETH',
                 'expression': 'RSI(14) < 30'},
            ],
        )
        fn = TOOL_REGISTRY['alert_manage']['handler']
        with app.app_context():
            out = fn({'action': 'list', 'family': 'formula'})
        assert out['count'] == 1
        assert out['alerts'][0]['id'] == 2


class TestCreateFormulaPlay:
    def test_registered(self):
        assert 'play_create_from_formula_alert' in TOOL_REGISTRY

    def test_missing_alert_id_raises(self):
        fn = TOOL_REGISTRY['play_create_from_formula_alert']['handler']
        with pytest.raises(ToolError):
            fn({})

    def test_unknown_alert_raises(self, app, monkeypatch):
        import push_notifications
        monkeypatch.setattr(
            push_notifications, 'get_user_alerts',
            lambda u, active_only=False: [],
        )
        fn = TOOL_REGISTRY['play_create_from_formula_alert']['handler']
        with app.app_context():
            with pytest.raises(ToolError):
                fn({'alert_id': 99})

    def test_creates_play_with_formula_condition(self, app, monkeypatch):
        import push_notifications
        from carrac.plays import models as plays_mod
        import carrac.mcp.runtime as rt
        import carrac.auth.decorators as dec

        monkeypatch.setattr(
            push_notifications, 'get_user_alerts',
            lambda u, active_only=False: [
                {'id': 7, 'alert_type': 'formula', 'symbol': 'BTC',
                 'expression': 'RSI(14) < 30', 'timeframe': '4h'},
            ],
        )
        # Stub the quota check so this test focuses on the creation logic, not limits
        monkeypatch.setattr(plays_mod, 'count_active_plays', lambda uid: 0)
        monkeypatch.setattr(rt, 'get_mcp_caller_tier', lambda: 'premium')
        monkeypatch.setattr(dec, 'get_tier_limits_for_user', lambda uid, t: {'max_plays': 25})

        captured = {}
        monkeypatch.setattr(
            plays_mod, 'play_create',
            lambda **kw: captured.update(kw) or {'id': 1234, **kw},
        )
        fn = TOOL_REGISTRY['play_create_from_formula_alert']['handler']
        with app.app_context():
            out = fn({'alert_id': 7, 'direction': 'short'})
        assert out['source_alert_id'] == 7
        assert captured['primary_asset'] == 'BTC'
        assert captured['direction'] == 'short'
        rules = captured['conditions']['rules']
        assert rules[0]['type'] == 'formula_expression'
        assert rules[0]['params']['expression'] == 'RSI(14) < 30'


class TestTriggeredAlertsToPlays:
    def test_registered(self):
        assert 'play_draft_from_triggered_alerts' in TOOL_REGISTRY
        assert TOOL_REGISTRY['play_draft_from_triggered_alerts']['min_tier'] == 'premium'

    def test_only_triggered_returned(self, app, monkeypatch):
        import push_notifications
        monkeypatch.setattr(
            push_notifications, 'get_user_alerts',
            lambda u, active_only=False: [
                {'id': 1, 'alert_type': 'price', 'symbol': 'BTC',
                 'condition': 'above', 'price': 50000, 'triggered': True,
                 'triggered_at': '2026-04-18T10:00:00'},
                {'id': 2, 'alert_type': 'price', 'symbol': 'ETH',
                 'condition': 'below', 'price': 3000, 'triggered': False},
            ],
        )
        fn = TOOL_REGISTRY['play_draft_from_triggered_alerts']['handler']
        with app.app_context():
            out = fn({})
        assert out['count'] == 1
        assert out['drafts'][0]['source_alert_id'] == 1

    def test_id_filter_works(self, app, monkeypatch):
        import push_notifications
        monkeypatch.setattr(
            push_notifications, 'get_user_alerts',
            lambda u, active_only=False: [
                {'id': 1, 'alert_type': 'price', 'symbol': 'BTC',
                 'condition': 'above', 'price': 50000, 'triggered': True},
                {'id': 2, 'alert_type': 'price', 'symbol': 'ETH',
                 'condition': 'below', 'price': 3000, 'triggered': True},
            ],
        )
        fn = TOOL_REGISTRY['play_draft_from_triggered_alerts']['handler']
        with app.app_context():
            out = fn({'alert_ids': '2'})
        assert out['count'] == 1
        assert out['drafts'][0]['source_alert_id'] == 2

    def test_invalid_id_filter_raises(self, app):
        fn = TOOL_REGISTRY['play_draft_from_triggered_alerts']['handler']
        with app.app_context():
            with pytest.raises(ToolError):
                fn({'alert_ids': 'not,a,number'})

    def test_skips_unsupported_alert_types(self, app, monkeypatch):
        import push_notifications
        monkeypatch.setattr(
            push_notifications, 'get_user_alerts',
            lambda u, active_only=False: [
                {'id': 1, 'alert_type': 'chain_strength_breach', 'triggered': True},
            ],
        )
        fn = TOOL_REGISTRY['play_draft_from_triggered_alerts']['handler']
        with app.app_context():
            out = fn({})
        assert out['count'] == 0
