"""Tests for alert_manage — bb_position and bb_params support."""

import pytest

from carrac.mcp import tools  # noqa: F401 — ensures registration
from carrac.mcp.registry import TOOL_REGISTRY
from carrac.mcp.protocol import ToolError


MANAGE_FN = lambda: TOOL_REGISTRY['alert_manage']['handler']  # noqa: E731


@pytest.fixture(autouse=True)
def _bind_user(bind_mcp_user):
    """Bind a per-user id so the shared-key guard doesn't fire."""
    return bind_mcp_user


def _patch_notifications(monkeypatch):
    """Pretend the bound user has a notification channel configured."""
    import push_notifications as pn

    monkeypatch.setattr(pn, 'user_has_notification_channel', lambda u: True)


def _stored_alerts():
    """The bound user's alerts as stored (``app`` gives each test its own DB)."""
    import push_notifications as pn
    return pn.get_user_alerts('42', active_only=False)


class TestManageAlertsRegistered:
    def test_registered(self):
        assert 'alert_manage' in TOOL_REGISTRY

    def test_min_tier_is_member(self):
        assert TOOL_REGISTRY['alert_manage']['min_tier'] == 'member'


class TestNoNotificationChannelMessage:
    """The 'no notification channel' error must name the MCP
    tool that fixes it in-session (notification_configure), not just point
    at the web UI Settings page."""

    def test_price_create_message_names_mcp_tool(self, app, monkeypatch):
        import push_notifications as pn
        monkeypatch.setattr(pn, 'user_has_notification_channel', lambda u: False)

        fn = MANAGE_FN()
        with app.app_context():
            with pytest.raises(ToolError) as exc:
                fn({
                    'action': 'create', 'alert_type': 'price',
                    'symbol': 'BTC', 'condition': 'above', 'price': 50000,
                })
        msg = str(exc.value)
        assert 'No notification channel configured' in msg
        assert 'notification_configure' in msg


class TestBbPositionCreate:
    def test_create_bb_position_default_params(self, app, monkeypatch):
        """Creating a bb_position alert without bb_params succeeds."""
        _patch_notifications(monkeypatch)

        fn = MANAGE_FN()
        with app.app_context():
            out = fn({
                'action': 'create',
                'alert_type': 'indicator',
                'symbol': 'BTC',
                'indicator': 'bb_position',
                'target_state': 'above_upper',
                'timeframe': '1h',
            })

        assert out['status'] == 'created'
        alert = out['alert']
        assert alert['indicator'] == 'bb_position'
        assert alert['target_state'] == 'above_upper'
        assert alert['symbol'] == 'BTC'
        assert alert['timeframe'] == '1h'
        assert alert['alert_type'] == 'indicator'
        assert alert['active'] is True
        # No bb_params field when not supplied.
        assert 'bb_params' not in alert

    def test_create_bb_position_custom_params(self, app, monkeypatch):
        """bb_params are snapshotted into the alert dict when provided."""
        _patch_notifications(monkeypatch)

        fn = MANAGE_FN()
        with app.app_context():
            out = fn({
                'action': 'create',
                'alert_type': 'indicator',
                'symbol': 'ETH',
                'indicator': 'bb_position',
                'target_state': 'below_lower',
                'timeframe': '4h',
                'bb_params': {'period': 14, 'std_dev': 2.5, 'offset': 5},
            })

        assert out['status'] == 'created'
        alert = out['alert']
        assert 'bb_params' in alert
        assert alert['bb_params']['period'] == 14
        assert alert['bb_params']['std_dev'] == 2.5
        assert alert['bb_params']['offset'] == 5

    def test_create_bb_position_stores_in_list(self, app, monkeypatch):
        """Alert is actually appended to the store after creation."""
        _patch_notifications(monkeypatch)

        fn = MANAGE_FN()
        with app.app_context():
            fn({
                'action': 'create',
                'alert_type': 'indicator',
                'symbol': 'BTC',
                'indicator': 'bb_position',
                'target_state': 'inside',
            })

        store = _stored_alerts()
        assert len(store) == 1
        assert store[0]['indicator'] == 'bb_position'
        assert store[0]['target_state'] == 'inside'


class TestBbPositionValidation:
    def test_invalid_target_state_rejected(self, app, monkeypatch):
        """target_state='wrong_value' raises ToolError with valid states listed."""
        _patch_notifications(monkeypatch)

        fn = MANAGE_FN()
        with app.app_context():
            with pytest.raises(ToolError) as exc:
                fn({
                    'action': 'create',
                    'alert_type': 'indicator',
                    'symbol': 'BTC',
                    'indicator': 'bb_position',
                    'target_state': 'wrong_value',
                })

        msg = str(exc.value)
        # Error must mention at least one valid state so the caller can self-correct.
        assert any(s in msg for s in ('inside', 'above_upper', 'below_lower')), \
            f"Expected valid states in error message, got: {msg}"

    def test_invalid_indicator_rejected(self, app, monkeypatch):
        """Invalid indicator name returns ToolError listing valid indicators."""
        _patch_notifications(monkeypatch)

        fn = MANAGE_FN()
        with app.app_context():
            with pytest.raises(ToolError) as exc:
                fn({
                    'action': 'create',
                    'alert_type': 'indicator',
                    'symbol': 'BTC',
                    'indicator': 'not_a_real_indicator',
                    'target_state': 'above_upper',
                })

        msg = str(exc.value)
        assert 'bb_position' in msg or 'Invalid indicator' in msg


class TestBbParamsValidation:
    def test_period_out_of_range_rejected(self, app, monkeypatch):
        """bb_params.period outside 5–200 is rejected with a clear error."""
        _patch_notifications(monkeypatch)

        fn = MANAGE_FN()
        with app.app_context():
            with pytest.raises(ToolError) as exc:
                fn({
                    'action': 'create',
                    'alert_type': 'indicator',
                    'symbol': 'BTC',
                    'indicator': 'bb_position',
                    'target_state': 'above_upper',
                    'bb_params': {'period': 300, 'std_dev': 2.0, 'offset': 0},
                })
        assert 'period' in str(exc.value).lower()

    def test_std_dev_out_of_range_rejected(self, app, monkeypatch):
        """bb_params.std_dev outside 0.5–5.0 is rejected."""
        _patch_notifications(monkeypatch)

        fn = MANAGE_FN()
        with app.app_context():
            with pytest.raises(ToolError) as exc:
                fn({
                    'action': 'create',
                    'alert_type': 'indicator',
                    'symbol': 'BTC',
                    'indicator': 'bb_position',
                    'target_state': 'above_upper',
                    'bb_params': {'period': 20, 'std_dev': 10.0, 'offset': 0},
                })
        assert 'std_dev' in str(exc.value).lower()

    def test_offset_out_of_range_rejected(self, app, monkeypatch):
        """bb_params.offset outside -20..20 is rejected."""
        _patch_notifications(monkeypatch)

        fn = MANAGE_FN()
        with app.app_context():
            with pytest.raises(ToolError) as exc:
                fn({
                    'action': 'create',
                    'alert_type': 'indicator',
                    'symbol': 'BTC',
                    'indicator': 'bb_position',
                    'target_state': 'above_upper',
                    'bb_params': {'period': 20, 'std_dev': 2.0, 'offset': 99},
                })
        assert 'offset' in str(exc.value).lower()

    def test_bb_params_on_non_bb_position_silently_ignored(self, app, monkeypatch):
        """bb_params provided for a non-bb_position indicator is ignored — not an error."""
        _patch_notifications(monkeypatch)

        fn = MANAGE_FN()
        with app.app_context():
            out = fn({
                'action': 'create',
                'alert_type': 'indicator',
                'symbol': 'BTC',
                'indicator': 'bb_squeeze',
                'target_state': 'squeezed',
                'bb_params': {'period': 14, 'std_dev': 2.0, 'offset': 0},
            })

        assert out['status'] == 'created'
        # bb_params should NOT be attached to a bb_squeeze alert.
        assert 'bb_params' not in out['alert']
        assert len(_stored_alerts()) == 1

    def test_bb_params_boundary_values_accepted(self, app, monkeypatch):
        """Boundary values (period=5, std_dev=0.5, offset=-20) are valid."""
        _patch_notifications(monkeypatch)

        fn = MANAGE_FN()
        with app.app_context():
            out = fn({
                'action': 'create',
                'alert_type': 'indicator',
                'symbol': 'SOL',
                'indicator': 'bb_position',
                'target_state': 'inside',
                'bb_params': {'period': 5, 'std_dev': 0.5, 'offset': -20},
            })

        assert out['status'] == 'created'
        bp = out['alert']['bb_params']
        assert bp['period'] == 5
        assert bp['std_dev'] == 0.5
        assert bp['offset'] == -20
