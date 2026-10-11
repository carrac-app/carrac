"""Journal REST returns ``tags`` and ``mistake_tags`` as lists.

The columns hold JSON text; the API hands back the decoded list so a client
never has to decode it a second time (``JSON.parse`` on an array throws).
"""

import json

import pytest

from carrac.content_db import get_content_db

UID = 70771


@pytest.fixture
def journal_client(client):
    with client.session_transaction() as sess:
        sess['user_id'] = UID
        sess['email'] = 'tags@example.com'
        sess['tier'] = 'member'
    return client


@pytest.fixture
def trade_id():
    db = get_content_db()
    tid = db.log_trade_open(
        user_id=UID, asset='BTC', timeframe='1D', direction='long',
        entry_price=100.0, tags=json.dumps(['src:manual', 'swing']),
    )
    db.trade_update(tid, mistake_tags=json.dumps(['fomo']))
    return tid


def test_get_trade_returns_tag_lists(journal_client, trade_id):
    body = journal_client.get(f'/api/journal/trades/{trade_id}').get_json()
    assert body['tags'] == ['src:manual', 'swing']
    assert body['mistake_tags'] == ['fomo']


def test_trade_list_returns_tag_lists(journal_client, trade_id):
    body = journal_client.get('/api/journal/trades').get_json()
    row = next(t for t in body['open'] if t['id'] == trade_id)
    assert row['tags'] == ['src:manual', 'swing']
    assert row['mistake_tags'] == ['fomo']


def test_trade_list_filters_still_match_on_tags(journal_client, trade_id):
    body = journal_client.get('/api/journal/trades?tags=swing&has_mistakes=true').get_json()
    assert trade_id in [t['id'] for t in body['open']]
    body = journal_client.get('/api/journal/trades?tags=nope').get_json()
    assert body['open'] == []


def test_untagged_trade_returns_null_not_a_string(journal_client):
    tid = get_content_db().log_trade_open(
        user_id=UID, asset='ETH', timeframe='1D', direction='long', entry_price=1.0)
    body = journal_client.get(f'/api/journal/trades/{tid}').get_json()
    assert body['tags'] is None or isinstance(body['tags'], list)
    assert not isinstance(body['mistake_tags'], str)
