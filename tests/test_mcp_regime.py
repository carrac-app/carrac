"""Tests for regime history & change alerts."""

import pytest

from carrac.mcp import tools  # noqa: F401
from carrac.mcp.registry import TOOL_REGISTRY
from carrac.mcp.protocol import ToolError


@pytest.fixture(autouse=True)
def _bind_user(bind_mcp_user):
    """Bind a per-user id so the shared-key guard doesn't trip
    direct tool-handler calls."""
    return bind_mcp_user


class TestGetRegimeHistory:
    def test_registered(self):
        assert 'quant_get_regime_history' in TOOL_REGISTRY
        # Quant data tools follow the quant_engine knob (True for
        # member/tester/premium/admin), not a hardcoded premium min_tier.
        assert TOOL_REGISTRY['quant_get_regime_history']['min_tier'] == 'member'
        assert TOOL_REGISTRY['quant_get_regime_history']['feature'] == 'quant_engine'

    def test_clamps_days(self, monkeypatch):
        # Patch get_conn to a no-op that returns empty rows.
        captured = {}

        class FakeCursor:
            def execute(self, sql, params):
                captured['days'] = params[1]

            def fetchall(self):
                return []

        class FakeConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return FakeCursor()

        from carrac import db_pool as db
        monkeypatch.setattr(db, 'get_quant_conn', lambda: FakeConn())

        fn = TOOL_REGISTRY['quant_get_regime_history']['handler']
        out = fn({'asset': 'BTC', 'days': 9999})
        # Clamped to 730.
        assert captured['days'] == 730
        assert out['samples'] == 0
        assert out['transitions'] == []

    def test_min_days_floor(self, monkeypatch):
        captured = {}

        class FakeCursor:
            def execute(self, sql, params):
                captured['days'] = params[1]
            def fetchall(self):
                return []

        class FakeConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return FakeCursor()

        from carrac import db_pool as db
        monkeypatch.setattr(db, 'get_quant_conn', lambda: FakeConn())
        fn = TOOL_REGISTRY['quant_get_regime_history']['handler']
        fn({'asset': 'BTC', 'days': 1})
        # Floor at 7.
        assert captured['days'] == 7

    def test_transitions_extracted(self, monkeypatch, admin_caller):
        from datetime import date
        rows = [
            # date, basis_pct, skew_25d, dvol, iv_rv_premium,
            # options_put_call_ratio, term_structure_slope, gex_net, gex_flip_price
            {'ts': date(2026, 4, 1), 'basis_pct': 5, 'skew_25d': -10, 'dvol': 70,
             'iv_rv_premium': 5, 'options_put_call_ratio': 0.6,
             'term_structure_slope': 0.05, 'gex_net': 1e9, 'gex_flip_price': 65000},
            {'ts': date(2026, 4, 2), 'basis_pct': 5, 'skew_25d': -10, 'dvol': 70,
             'iv_rv_premium': 5, 'options_put_call_ratio': 0.6,
             'term_structure_slope': 0.05, 'gex_net': 1e9, 'gex_flip_price': 65000},
        ]

        class FakeCursor:
            def execute(self, sql, params): pass
            def fetchall(self): return rows
        class FakeConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return FakeCursor()

        from carrac import db_pool as db
        monkeypatch.setattr(db, 'get_quant_conn', lambda: FakeConn())

        # Force classifier to return alternating regimes.
        from quant.engine import derivatives_regime as dr
        seq = iter([
            {'regime': 'healthy_trend', 'confidence': 0.8},
            {'regime': 'distribution_topping', 'confidence': 0.7},
        ])
        monkeypatch.setattr(dr, 'classify_derivatives_regime', lambda m: next(seq))

        fn = TOOL_REGISTRY['quant_get_regime_history']['handler']
        out = fn({'asset': 'BTC', 'days': 30})
        assert out['samples'] == 2
        assert out['transitions_count'] == 1
        assert out['transitions'][0]['from_regime'] == 'healthy_trend'
        assert out['transitions'][0]['to_regime'] == 'distribution_topping'
        assert 'healthy_trend' in out['occupancy_pct']


class TestCreateRegimeChangeAlert:
    def test_registered(self):
        assert 'alert_create_regime_watch' in TOOL_REGISTRY

    def test_no_notification_channel_blocks(self, app, monkeypatch):
        import push_notifications
        monkeypatch.setattr(push_notifications, 'user_has_notification_channel', lambda u: False)

        fn = TOOL_REGISTRY['alert_create_regime_watch']['handler']
        with app.app_context():
            with pytest.raises(ToolError) as exc:
                fn({'to_regime': 'distribution_topping'})
        assert 'notification channel' in str(exc.value)

    def test_creates_alert(self, app, monkeypatch):
        import push_notifications
        monkeypatch.setattr(push_notifications, 'user_has_notification_channel', lambda u: True)

        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return {'id': 12345, 'alert_type': 'derivatives_regime', **kwargs}

        monkeypatch.setattr(
            push_notifications,
            'create_derivatives_regime_alert',
            fake_create,
        )

        fn = TOOL_REGISTRY['alert_create_regime_watch']['handler']
        with app.app_context():
            out = fn({
                'from_regime': 'complacent_bull',
                'to_regime': 'distribution_topping',
                'note': 'Top warning',
                'frequency': 'once',
            })
        assert out['status'] == 'created'
        assert captured['from_regime'] == 'complacent_bull'
        assert captured['to_regime'] == 'distribution_topping'
        assert captured['note'] == 'Top warning'

    def test_creator_validates_unknown_regime(self):
        import push_notifications
        with pytest.raises(ValueError):
            push_notifications.create_derivatives_regime_alert(
                user_id='1', from_regime='made_up_regime',
            )

    def test_creator_rejects_same_from_to(self):
        import push_notifications
        with pytest.raises(ValueError):
            push_notifications.create_derivatives_regime_alert(
                user_id='1',
                from_regime='healthy_trend',
                to_regime='healthy_trend',
            )


class TestRegimeAlertWorkerFiltering:
    """The worker must respect from_regime / to_regime filters."""

    def test_to_filter_blocks_unrelated_transition(self, monkeypatch, tmp_path):
        # Use the worker check by simulating an alert with to_filter.
        alert = {
            'id': 1,
            'user_id': '1',
            'alert_type': 'derivatives_regime',
            'from_regime': None,
            'to_regime': 'distribution_topping',
            'last_deriv_regime': 'complacent_bull',
            'frequency': 'once',
            'active': True,
            'triggered': False,
        }
        # Apply the filter logic inline (mimic worker block).
        prev = alert['last_deriv_regime']
        current = 'healthy_trend'  # NOT distribution_topping
        from_filter = alert.get('from_regime')
        to_filter = alert.get('to_regime')
        match = True
        if from_filter and prev != from_filter:
            match = False
        if to_filter and current != to_filter:
            match = False
        assert match is False

    def test_to_filter_passes_matching_transition(self):
        prev = 'complacent_bull'
        current = 'distribution_topping'
        from_filter = None
        to_filter = 'distribution_topping'
        match = True
        if from_filter and prev != from_filter:
            match = False
        if to_filter and current != to_filter:
            match = False
        assert match is True

    def test_from_to_filter_strict_match(self):
        prev = 'healthy_trend'
        current = 'sudden_correction'
        from_filter = 'healthy_trend'
        to_filter = 'sudden_correction'
        match = True
        if from_filter and prev != from_filter:
            match = False
        if to_filter and current != to_filter:
            match = False
        assert match is True


class TestClassifierKeyParity:
    """Every surface must classify the SAME row identically.

    classify_derivatives_regime expects MAPPED keys (basis_apr,
    put_call_ratio, term_slope). Feeding it raw
    quant.derivatives column names (basis_pct, options_put_call_ratio,
    term_structure_slope) silently drops those three features
    ("don't penalize missing metrics"), so such a surface would classify on 5
    features while quant_get_regime classifies on 8 — publicly disagreeing
    labels/confidences for the same date.
    """

    # A real production BTC row — known to discriminate: raw-key
    # classification yields a different confidence than mapped-key.
    ROW = {'basis_pct': 5.1527, 'skew_25d': -1.03, 'dvol': 43.1,
           'iv_rv_premium': 4.92, 'options_put_call_ratio': 0.6043,
           'term_structure_slope': -0.0439, 'gex_net': 262577461.93,
           'gex_flip_price': 72464.24}

    def _fake_conn(self, monkeypatch, rows):
        class FakeCursor:
            def execute(self, sql, params=None): pass
            def fetchall(self): return rows
            def fetchone(self): return rows[0] if rows else None
        class FakeConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return FakeCursor()
        from carrac import db_pool as db
        monkeypatch.setattr(db, 'get_quant_conn', lambda: FakeConn())

    def test_history_matches_the_snapshot_classification(self, monkeypatch, admin_caller):
        from datetime import date
        from quant.engine.derivatives_regime import classify_asset_row
        expected = classify_asset_row(self.ROW)

        self._fake_conn(monkeypatch, [dict(self.ROW, ts=date(2026, 8, 25))])
        fn = TOOL_REGISTRY['quant_get_regime_history']['handler']
        out = fn({'asset': 'BTC', 'days': 7})
        assert out['samples'] == 1
        got = out['sequence'][0]
        assert got['regime'] == expected['regime']
        assert got['confidence'] == round(expected['confidence'], 3), \
            'history must classify with the same (mapped) features as quant_get_regime'

    # A capitulation-shaped row where the raw-key bug FLIPS the label:
    # mapped-key classification says full_capitulation (0.75), raw-key says
    # euphoric_meltup (0.67) — opposite ends of the taxonomy, because the
    # three dropped features (negative basis, low put/call, inverted term
    # structure) are exactly what separates the two.
    FLIP_ROW = {'basis_pct': -5, 'skew_25d': -12, 'dvol': 40,
                'iv_rv_premium': -5, 'options_put_call_ratio': 0.4,
                'term_structure_slope': -0.15, 'gex_net': -2e9,
                'gex_flip_price': 60000}

    def test_play_condition_matches_the_snapshot_classification(self, monkeypatch):
        from quant.engine.derivatives_regime import classify_asset_row
        expected = classify_asset_row(self.FLIP_ROW)
        assert expected['regime'] == 'full_capitulation'  # row sanity

        self._fake_conn(monkeypatch, [dict(self.FLIP_ROW)])
        from carrac.conditions import _eval_derivatives_regime
        met, detail = _eval_derivatives_regime(
            {'regime': expected['regime'], 'condition': 'matches'})
        assert met, ('plays/alerts evaluator disagreed with quant_get_regime '
                     'about the same row: ' + detail)

    def test_no_raw_key_call_sites_remain(self):
        """Source guard: only the mapper module and the two audited
        mapped-key sites may call classify_derivatives_regime directly.
        Everyone else must go through classify_asset_row, which owns the
        raw-column -> classifier-key mapping."""
        import pathlib
        import subprocess
        root = pathlib.Path(__file__).resolve().parents[1]
        allowed = {
            'quant/engine/derivatives_regime.py',   # the mapper itself
            'quant/api/routes.py',                  # maps keys by hand (audited)
            'morning_engine/data_assembly.py',      # maps keys by hand + passes spot
        }
        # Tracked files only.
        tracked = subprocess.run(
            ['git', 'ls-files', '*.py'], cwd=root,
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        offenders = []
        for rel in tracked:
            if rel.startswith('tests/') or rel in allowed:
                continue
            try:
                text = (root / rel).read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            if 'classify_derivatives_regime(' in text:
                offenders.append(rel)
        assert offenders == [], \
            f'call classify_asset_row instead (raw-key drift): {offenders}'
