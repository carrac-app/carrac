"""A failed regime_history insert must not take the sentiment row down with it.

Production runs PostgreSQL, where one failed statement aborts the whole
transaction until it is rolled back.  SQLite cannot reproduce that, so these
tests drive the function through a fake connection that models the abort.
"""

import contextlib
import logging
from unittest.mock import patch

import pytest

from carrac import precompute


class AbortingConn:
    """Models PostgreSQL: after a failed statement everything fails until
    ROLLBACK TO SAVEPOINT; a plain rollback discards all uncommitted rows."""

    def __init__(self):
        self.pending = []
        self.committed = []
        self.aborted = False
        self.savepoints = []

    def execute(self, sql, params=None):
        stripped = sql.strip().upper()
        if stripped.startswith('SAVEPOINT'):
            self.savepoints.append(stripped.split()[1])
            return None
        if stripped.startswith('ROLLBACK TO SAVEPOINT'):
            self.aborted = False
            return None
        if stripped.startswith('RELEASE SAVEPOINT'):
            return None
        if self.aborted:
            raise RuntimeError('current transaction is aborted')
        if 'REGIME_HISTORY' in stripped:
            self.aborted = True
            raise RuntimeError('regime insert failed')
        self.pending.append(sql)
        return None

    def commit(self):
        if self.aborted:
            self.pending = []  # PostgreSQL turns COMMIT into ROLLBACK
        else:
            self.committed.extend(self.pending)
            self.pending = []


@pytest.fixture
def conn():
    return AbortingConn()


def _run(conn):
    @contextlib.contextmanager
    def fake_get_conn():
        yield conn
        conn.commit()

    fake_db = type('DB', (), {
        'get_funding_rate': lambda self, s: 0.01,
        'fetch_open_interest': lambda self, s: {'value_usd': 1, 'change_pct': 2},
        'fetch_mvrv_zscore': lambda self, *a: {'zscore': 1.0},
        'calculate_alt_season_index': lambda self: {'index': 40},
        'fetch_long_short_ratio': lambda self, s: {'BTC': {'longAccount': 0.5}},
    })()
    services = type('S', (), {'db': fake_db})()
    regime = {'regime': 'risk_on', 'score': 1, 'components': {}}
    with patch('carrac.db_pool.get_conn', fake_get_conn), \
         patch('carrac.analysis.fetch_fear_greed_index', return_value={'value': 50}), \
         patch.object(precompute, 'get_services', return_value=services), \
         patch('carrac.sentiment.regime.gather_regime_inputs', return_value={}), \
         patch('carrac.sentiment.regime.compute_market_regime', return_value=regime):
        precompute._record_sentiment_snapshot()


def test_sentiment_row_survives_failed_regime_insert(conn, caplog):
    with caplog.at_level(logging.INFO, logger=precompute.logger.name):
        _run(conn)
    assert any('sentiment_history' in s for s in conn.committed)
    assert not any('regime_history' in s.lower() for s in conn.committed)
    messages = [r.getMessage() for r in caplog.records]
    assert any('regime_history: snapshot not recorded' in m for m in messages)
    # The success line must not claim a regime row that never landed.
    assert not any(m == 'Recorded sentiment snapshot' for m in messages)
    assert any(m == 'Recorded sentiment snapshot (without regime row)' for m in messages)


def test_success_log_when_both_rows_land(conn, caplog):
    conn.execute = lambda sql, params=None: conn.pending.append(sql) if not sql.strip().upper().split()[0] in ('SAVEPOINT', 'ROLLBACK', 'RELEASE') else None
    with caplog.at_level(logging.INFO, logger=precompute.logger.name):
        _run(conn)
    assert any('regime_history' in s.lower() for s in conn.committed)
    assert any(r.getMessage() == 'Recorded sentiment snapshot' for r in caplog.records)
