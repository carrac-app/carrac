"""Drawing reads return ISO 8601 timestamps, the same as the PUT response."""
import datetime as dt

import pytest

from carrac.content_db import ContentDatabase

_WHEN = dt.datetime(2026, 10, 10, 6, 26, 39, 981769, tzinfo=dt.timezone.utc)
_ISO = '2026-10-10T06:26:39.981769+00:00'


def _row(user_id):
    return {'id': 7, 'user_id': user_id, 'asset': 'BTCUSDT', 'timeframe': '1D',
            'drawing_type': 'horizontal_line', 'created_at': _WHEN, 'updated_at': _WHEN}


_UID = 5


@pytest.fixture
def signed_in(client):
    with client.session_transaction() as sess:
        sess['user_id'] = _UID
    return client


def test_get_one_returns_iso(signed_in, monkeypatch):
    monkeypatch.setattr(ContentDatabase, 'get_drawing_by_id', lambda self, i, user_id=None: _row(_UID))

    body = signed_in.get('/api/drawings/7').get_json()

    assert body['drawing']['created_at'] == _ISO


def test_list_returns_iso(signed_in, monkeypatch):
    monkeypatch.setattr(ContentDatabase, 'drawing_list', lambda self, **kw: [_row(_UID)])

    body = signed_in.get('/api/drawings').get_json()

    assert body['drawings'][0]['updated_at'] == _ISO
