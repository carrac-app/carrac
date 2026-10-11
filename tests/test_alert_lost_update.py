"""check_alerts must never revert a change the user made while it was busy.

check_alerts reads the alerts, marks the ones that fired, then spends seconds
dispatching notifications over the network before it records the delivery
outcome. Anything the user creates, edits or deletes in that window has to
survive the checker's final write: the checker may only touch the alerts it
fired, and only the fields it changed on them.
"""
import pytest

import push_notifications as pn


@pytest.fixture(autouse=True)
def _alert_db(tmp_path, rebind_db):
    rebind_db(tmp_path / 'db')


@pytest.fixture
def quiet_dispatch(monkeypatch):
    """Stub the network boundary; the test supplies what happens during it."""
    monkeypatch.setattr(pn, 'capture_alert_event', lambda *a, **k: None)

    def _install(during_dispatch):
        def _dispatch(triggered):
            during_dispatch()
            return {a['id']: True for a in triggered}
        monkeypatch.setattr(pn, '_dispatch_bundle_or_direct', _dispatch)

    return _install


def _stored(user_id='1'):
    return {a['id']: a for a in pn.get_user_alerts(user_id, active_only=False)}


def test_edits_and_creates_during_dispatch_survive(quiet_dispatch):
    fired_alert = pn.create_alert('1', 'BTC', 'above', 100.0, frequency='once_per_bar')
    other = pn.create_alert('1', 'ETH', 'above', 5000.0)
    created = {}

    def _user_acts():
        pn.update_alert('1', fired_alert['id'], {'note': 'edited mid-dispatch'})
        pn.update_alert('1', other['id'], {'price': 6000.0})
        created['alert'] = pn.create_alert('1', 'SOL', 'below', 10.0)

    quiet_dispatch(_user_acts)
    fired = pn.check_alerts({'BTC': 150.0, 'ETH': 4000.0})

    assert [a['id'] for a in fired] == [fired_alert['id']]
    stored = _stored()
    assert stored[fired_alert['id']]['note'] == 'edited mid-dispatch'
    assert stored[fired_alert['id']]['last_triggered_bar'], \
        'the trigger itself must still be recorded'
    assert stored[other['id']]['price'] == 6000.0
    assert created['alert']['id'] in stored, 'an alert created mid-dispatch was dropped'


def test_a_delete_during_dispatch_stays_deleted(quiet_dispatch):
    fired_alert = pn.create_alert('1', 'BTC', 'above', 100.0, frequency='once')
    bystander = pn.create_alert('1', 'ETH', 'above', 5000.0)

    def _user_deletes():
        pn.delete_alert('1', fired_alert['id'])
        pn.delete_alert('1', bystander['id'])

    quiet_dispatch(_user_deletes)
    pn.check_alerts({'BTC': 150.0})

    assert _stored() == {}, 'the checker resurrected alerts the user deleted'
