"""The alert store: round trip, row-level writes, failures that raise, and the
one-shot import of the legacy price_alerts.json."""
import json
import logging
import sqlite3

import pytest

import push_notifications as pn
from carrac.alerts import store


@pytest.fixture(autouse=True)
def _alert_db(tmp_path, rebind_db, monkeypatch):
    rebind_db(tmp_path / 'db')
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    monkeypatch.setenv('DATA_DIR', str(data_dir))
    monkeypatch.setattr(store, '_import_checked', True)
    return data_dir


def _legacy_file(data_dir, alerts):
    path = data_dir / 'price_alerts.json'
    path.write_text(json.dumps(alerts))
    return path


LEGACY = [
    {'id': 101, 'user_id': '1', 'symbol': 'BTC', 'condition': 'above', 'price': 100.0,
     'note': 'n', 'frequency': 'once_per_bar', 'last_triggered_bar': None,
     'created_at': '2026-09-01T10:00:00', 'triggered': False, 'triggered_at': None,
     'active': True, 'journal_id': 7, 'auto_close': 'tp_hit'},
    {'id': 'a1b2c3d4', 'user_id': '1', 'symbol': 'ETH', 'alert_type': 'formula',
     'expression': 'close < 1', 'timeframe': '1D', 'note': '', 'frequency': 'once',
     'active': True, 'triggered': False, 'created_at': '2026-09-02T10:00:00Z'},
    {'id': 103, 'user_id': 2, 'alert_type': 'conditional_query_match',
     'conditions': ['VIX:gt:2'], 'target': 'BTC', 'horizon': 30, 'last_match': False,
     'frequency': 'recurring', 'active': False, 'triggered': True,
     'triggered_at': '2026-09-03T00:00:00', 'created_at': '2026-09-03T00:00:00'},
]


# ── round trip ──────────────────────────────────────────────────────────────

class TestRoundTrip:
    def test_every_kind_keeps_its_fields(self):
        for raw in LEGACY:
            alert = dict(raw, id=store.new_alert_id(), user_id=str(raw['user_id']))
            store.insert(alert)
        by_type = {a.get('alert_type', 'price'): a for a in store.list_alerts()}

        price = by_type['price']
        assert 'alert_type' not in price, 'a price alert has never carried alert_type'
        assert price['journal_id'] == 7 and price['auto_close'] == 'tp_hit'
        assert price['last_triggered_bar'] is None and price['user_id'] == '1'

        quant = by_type['conditional_query_match']
        assert 'symbol' not in quant
        assert quant['conditions'] == ['VIX:gt:2'] and quant['last_match'] is False
        assert quant['active'] is False and quant['triggered'] is True

    def test_scratch_keys_are_not_stored(self):
        alert = pn.create_alert('1', 'BTC', 'above', 1.0)
        store.modify(alert['id'], lambda a: a.update(_pending_notification={'x': 1}))
        assert '_pending_notification' not in pn.get_user_alerts('1')[0]

    def test_unknown_owner_is_refused(self):
        with pytest.raises(ValueError, match='no user matches'):
            pn.create_alert('not-a-user', 'BTC', 'above', 1.0)
        assert pn.get_user_alerts('not-a-user') == []

    def test_owner_checks(self):
        alert = pn.create_alert('1', 'BTC', 'above', 1.0)
        assert pn.toggle_alert('2', alert['id']) is None
        assert pn.delete_alert('2', alert['id']) is False
        assert pn.delete_alerts_bulk('2', [alert['id'], 999]) == ([], sorted([alert['id'], 999]))
        assert pn.toggle_alert('1', alert['id'])['active'] is False
        assert pn.delete_alerts_bulk('1', [alert['id'], 999]) == ([alert['id']], [999])


# ── row-level writes ────────────────────────────────────────────────────────

class TestSaveChanges:
    def test_writes_only_the_changed_fields(self):
        alert = pn.create_alert('1', 'BTC', 'above', 100.0)
        [read] = store.list_alerts()
        baseline = store.snapshot([read])
        pn.update_alert('1', alert['id'], {'price': 200.0})   # someone else's write

        read['triggered'] = True
        read['triggered_price'] = 101.0
        assert store.save_changes([read], baseline) == 1

        [now] = store.list_alerts()
        assert now['price'] == 200.0, 'a field this run did not change was overwritten'
        assert now['triggered'] is True and now['triggered_price'] == 101.0

    def test_a_removed_key_is_removed(self):
        pn.create_alert('1', 'BTC', 'above', 100.0)
        store.modify(store.list_alerts()[0]['id'], lambda a: a.update(undelivered_at='t'))
        [read] = store.list_alerts()
        baseline = store.snapshot([read])
        read.pop('undelivered_at')
        store.save_changes([read], baseline)
        assert 'undelivered_at' not in store.list_alerts()[0]

    def test_unchanged_alerts_are_not_written(self, monkeypatch):
        pn.create_alert('1', 'BTC', 'above', 100.0)
        alerts = store.list_alerts()
        baseline = store.snapshot(alerts)
        monkeypatch.setattr(store, 'modify', lambda *a, **k: pytest.fail('wrote an unchanged alert'))
        assert store.save_changes(alerts, baseline) == 0


# ── a failed write raises ───────────────────────────────────────────────────

def _drop_table():
    from user_db import user_db
    with user_db.transaction() as conn:
        conn.cursor().execute(f'DROP TABLE {store.TABLE}')


class TestAFailedWriteRaises:
    def test_create(self):
        _drop_table()
        with pytest.raises(sqlite3.OperationalError):
            pn.create_alert('1', 'BTC', 'above', 1.0)

    def test_the_checker_trigger_write(self, monkeypatch):
        pn.create_alert('1', 'BTC', 'above', 100.0, frequency='once')
        # The table disappears after check_alerts has read it, before it writes.
        monkeypatch.setattr(pn, 'capture_alert_event', lambda *a, **k: _drop_table())
        monkeypatch.setattr(pn, '_dispatch_bundle_or_direct',
                            lambda t: pytest.fail('dispatched an alert whose trigger was not stored'))
        with pytest.raises(sqlite3.OperationalError):
            pn.check_alerts({'BTC': 150.0})


# ── one-shot import of price_alerts.json ────────────────────────────────────

class TestLegacyImport:
    def test_imports_renames_and_logs_once(self, _alert_db, caplog):
        path = _legacy_file(_alert_db, LEGACY)
        with caplog.at_level(logging.INFO, logger='carrac.alerts.store'):
            assert store.import_legacy_json() == 3

        assert not path.exists()
        assert (_alert_db / 'price_alerts.json.imported').exists()
        lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert lines == [f'Imported 3 alerts from {path} into user_alerts']

        stored = {a['id']: a for a in store.list_alerts()}
        assert set(stored) == {101, int('a1b2c3d4', 16), 103}
        assert stored[101]['journal_id'] == 7
        assert stored[103]['user_id'] == '2' and stored[103]['active'] is False
        assert [a['id'] for a in store.list_alerts()] == [101, int('a1b2c3d4', 16), 103], \
            'creation order must survive the import'

    def test_runs_on_first_use_of_the_store(self, _alert_db, monkeypatch):
        _legacy_file(_alert_db, LEGACY[:1])
        monkeypatch.setattr(store, '_import_checked', False)
        assert [a['id'] for a in pn.get_user_alerts('1')] == [101]

    def test_is_idempotent(self, _alert_db):
        _legacy_file(_alert_db, LEGACY)
        store.import_legacy_json()
        assert store.import_legacy_json() is None
        assert len(store.list_alerts()) == 3

    def test_leaves_the_file_when_the_table_has_alerts(self, _alert_db, caplog):
        pn.create_alert('1', 'SOL', 'below', 1.0)
        path = _legacy_file(_alert_db, LEGACY)
        assert store.import_legacy_json() is None
        assert path.exists()
        assert len(store.list_alerts()) == 1
        assert 'was not imported' in caplog.text

    def test_admin_resolves_and_unknown_owners_are_reported(self, _alert_db, monkeypatch, caplog):
        from user_db import user_db
        monkeypatch.setenv('ADMIN_EMAIL', 'owner@test.dev')
        admin_id = user_db.create_user('owner@test.dev', 'StrongPass!123', tos_accepted=True)
        _legacy_file(_alert_db, [dict(LEGACY[0], user_id='admin'),
                                 dict(LEGACY[0], id=102, user_id='ghost')])

        assert store.import_legacy_json() == 1
        assert store.list_alerts()[0]['user_id'] == str(admin_id)
        assert 'Skipped 1 legacy alerts' in caplog.text and '102' in caplog.text

    def test_a_failed_import_writes_nothing_and_keeps_the_file(self, _alert_db):
        path = _legacy_file(_alert_db, [LEGACY[0], dict(LEGACY[0], id='not hex!', price='x')])
        _drop_table()
        with pytest.raises(sqlite3.OperationalError):
            store.import_legacy_json()
        assert path.exists()


# ── two writers on one alert ────────────────────────────────────────────────

def test_concurrent_modifies_of_one_alert_both_land():
    """A second writer waits for the first instead of writing back a stale row."""
    import threading

    alert = pn.create_alert('1', 'BTC', 'above', 100.0)
    holding = threading.Event()

    def _slow_note(a):
        a['note'] = 'from A'
        holding.set()
        threading.Event().wait(0.5)

    a = threading.Thread(target=store.modify, args=(alert['id'], _slow_note))
    a.start()
    holding.wait(5)
    store.modify(alert['id'], lambda row: row.update(price=200.0))
    a.join()

    [now] = store.list_alerts()
    assert (now['note'], now['price']) == ('from A', 200.0)
