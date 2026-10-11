"""End-to-end coverage for GET /api/portfolio/summary and .../history,
through the real strangler ASGI app (``asgi_client``) with a real per-user
API key (``member_key``), so that:

* an unauthenticated request gets a JSON 401, never a 302 redirect to
  /login — the whole reason these routes use ``auth_or_key`` instead of
  ``require_login`` (see the module docstring on
  ``carrac/fastapi_app/routers/portfolio.py``). A 302 would fail the JSON
  parse below, which is the point of asserting it this way rather than just
  checking the status code.
* the documented response keys are actually present on a real response body,
  not just asserted in prose.
* the ``days`` bounds on /history are actually enforced.
* the summary handler narrows its lookback window (Fix 2) rather than
  reconstructing the full history window just to read yesterday's close.
* a trade opened long before the requested window and closed inside it
  still shows up on the curve, through the real DB (Fix 1, full stack).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from carrac.db_pool import adapt_query


@pytest.fixture
def trade_db(app):  # noqa: ARG001 — app sets DATA_DIR before we bind the singleton
    """ContentDatabase singleton bound to THIS test's DATA_DIR.

    Same rebind-the-singleton pattern as tests/fastapi/test_trade_shares_asgi.py.
    """
    import carrac.content_db as cdb

    cdb._db_instance = None
    db = cdb.get_content_db()
    yield db
    cdb._db_instance = None


def _backdate(db, trade_id: int, *, opened_at: str, closed_at: str) -> None:
    """Force opened_at/closed_at to specific timestamps.

    ``log_trade_open``/``log_trade_close`` always stamp CURRENT_TIMESTAMP;
    TRADE_UPDATE_FIELDS (trade_update's allowlist) doesn't include either
    column, so a direct UPDATE is the only way to backdate for a test.
    """
    conn = db._get_conn()
    conn.execute(
        adapt_query('UPDATE trade_journal SET opened_at = ?, closed_at = ? WHERE id = ?'),
        (opened_at, closed_at, trade_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Auth: JSON 401, never a redirect (Fix 3)
# ---------------------------------------------------------------------------

def test_summary_without_auth_is_a_json_401_not_a_redirect(asgi_client):
    resp = asgi_client.get('/api/portfolio/summary')
    assert resp.status_code == 401
    resp.json()  # must parse as JSON — a 302 to /login would not


def test_history_without_auth_is_a_json_401_not_a_redirect(asgi_client):
    resp = asgi_client.get('/api/portfolio/history')
    assert resp.status_code == 401
    resp.json()


# ---------------------------------------------------------------------------
# Response shape (Fix 3)
# ---------------------------------------------------------------------------

def test_summary_response_has_the_documented_shape(asgi_client, member_key, trade_db,
                                                    monkeypatch):
    import carrac.composite.assemblers as asm
    monkeypatch.setattr(asm, '_get_prices', lambda: {})

    headers, _uid = member_key
    resp = asgi_client.get('/api/portfolio/summary', headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in ('book_value', 'cost_basis', 'unrealized_pnl_usd', 'unrealized_pnl_pct',
                'spot_market_value', 'futures_unrealized_pnl', 'position_count',
                'spot_count', 'futures_count', 'positions', 'excluded', 'allocation',
                'day_change_usd', 'day_change_pct', 'truncated'):
        assert key in body, key
    assert body['allocation'].keys() >= {'by_asset', 'by_kind', 'by_direction'}


def test_history_response_has_the_documented_shape(asgi_client, member_key, trade_db,
                                                    monkeypatch):
    import carrac.composite.assemblers as asm
    monkeypatch.setattr(asm, '_get_prices', lambda: {})

    headers, _uid = member_key
    resp = asgi_client.get('/api/portfolio/history', params={'days': 30}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['days'] == 30
    assert isinstance(body['curve'], list)
    assert body['truncated'] is False
    assert len(body['curve']) == 30


# ---------------------------------------------------------------------------
# `days` bounds (Fix 3)
# ---------------------------------------------------------------------------

def test_history_rejects_zero_days(asgi_client, member_key):
    headers, _uid = member_key
    resp = asgi_client.get('/api/portfolio/history', params={'days': 0}, headers=headers)
    # This app remaps FastAPI's default 422 to 400 for validation errors
    # (carrac/fastapi_app/exception_handlers.py — matches Flask's error
    # shape), so 400, not 422, is the correct rejection here.
    assert resp.status_code == 400


def test_history_rejects_days_beyond_the_max(asgi_client, member_key):
    headers, _uid = member_key
    resp = asgi_client.get('/api/portfolio/history', params={'days': 99999}, headers=headers)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Fix 2 — the summary handler must not reconstruct the full history window
# ---------------------------------------------------------------------------

def test_summary_uses_the_narrow_day_change_lookback(asgi_client, member_key, monkeypatch):
    from carrac.fastapi_app.routers import portfolio as portfolio_mod

    captured = {}
    real_load = portfolio_mod._load

    def _spy(user_id, days):
        captured['days'] = days
        return real_load(user_id, days)

    monkeypatch.setattr(portfolio_mod, '_load', _spy)

    headers, _uid = member_key
    resp = asgi_client.get('/api/portfolio/summary', headers=headers)
    assert resp.status_code == 200, resp.text
    assert captured['days'] == portfolio_mod.DAY_CHANGE_LOOKBACK_DAYS
    assert captured['days'] < 30  # materially narrower than a hardcoded 30


# ---------------------------------------------------------------------------
# Postgres hands back datetimes; the response must survive them
# ---------------------------------------------------------------------------

def test_summary_serialises_a_postgres_style_datetime(asgi_client, member_key, monkeypatch):
    """A ``datetime`` in the payload must not 500 the endpoint.

    ``positions[].opened_at`` is a raw ``trade_journal`` column. Postgres
    returns ``timestamp with time zone`` as a ``datetime`` object; SQLite
    returns it as a string. The whole suite runs on SQLite, so no test that
    goes through the real DB can ever produce the value that broke prod —
    the datetime has to be injected, or this test is theatre.

    Hence monkeypatching ``_load``: the unit under test is the handler's
    SERIALISATION, and ``_load`` is the DB collaborator whose driver decides
    the type. Same spy pattern as the Fix 2 test above.
    """
    from carrac.fastapi_app.routers import portfolio as portfolio_mod

    opened = datetime(2026, 8, 1, 14, 30, tzinfo=timezone.utc)

    def _fake_load(user_id, days):
        return {
            'summary': {
                'book_value': 1000.0,
                'cost_basis': 900.0,
                'unrealized_pnl_usd': 100.0,
                'unrealized_pnl_pct': 11.11,
                'spot_market_value': 1000.0,
                'futures_unrealized_pnl': 0.0,
                'position_count': 1,
                'spot_count': 1,
                'futures_count': 0,
                'positions': [{'id': 1, 'asset': 'BTC', 'opened_at': opened}],
                'excluded': [],
                'allocation': {'by_asset': {}, 'by_kind': {}, 'by_direction': {}},
            },
            'curve': [],
            'truncated': False,
        }

    monkeypatch.setattr(portfolio_mod, '_load', _fake_load)

    headers, _uid = member_key
    resp = asgi_client.get('/api/portfolio/summary', headers=headers)

    assert resp.status_code == 200, resp.text
    got = resp.json()['positions'][0]['opened_at']
    # A string, per the OpenAPI contract (carrac/openapi/schemas.py declares
    # opened_at as type=string format=date-time), and parseable back — the
    # frontend does `new Date(t.opened_at)` / `.substring(5, 10)` on it.
    assert isinstance(got, str), f'expected an ISO string, got {type(got).__name__}'
    assert got.startswith('2026-08-01T14:30')


# ---------------------------------------------------------------------------
# Fix 1, full stack — a trade opened long before the window, closed inside it
# ---------------------------------------------------------------------------

def test_history_curve_includes_a_trade_opened_long_before_the_window_but_closed_inside_it(
        asgi_client, member_key, trade_db, monkeypatch):
    headers, uid = member_key

    tid = trade_db.log_trade_open(
        user_id=uid, asset='BTC', timeframe='1D', direction='long',
        entry_price=100.0, stop_price=90.0, target_price=200.0,
    )
    trade_db.trade_update(tid, size=2.0, account_tag='spot')
    trade_db.log_trade_close(journal_id=tid, exit_price=150.0)

    today = date.today()
    opened_at = (today - timedelta(days=400)).isoformat() + 'T00:00:00'
    close_day = today - timedelta(days=10)
    closed_at = close_day.isoformat() + 'T00:00:00'
    _backdate(trade_db, tid, opened_at=opened_at, closed_at=closed_at)

    import carrac.composite.assemblers as asm
    monkeypatch.setattr(asm, '_get_prices', lambda: {})

    def _flat_lookup(symbols, loader=None):
        return lambda symbol, day: 120.0

    monkeypatch.setattr('carrac.portfolio.prices.build_price_lookup', _flat_lookup)

    resp = asgi_client.get('/api/portfolio/history', params={'days': 90}, headers=headers)
    assert resp.status_code == 200, resp.text
    curve = {row['date']: row for row in resp.json()['curve']}

    still_open_day = (close_day - timedelta(days=1)).isoformat()
    assert curve[still_open_day]['book_value'] == pytest.approx(2.0 * 120.0)
