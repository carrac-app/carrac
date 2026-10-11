"""The below-the-cut write sites no longer whisper.

Three sites, each guarding something a human would want to know about:

  * ``csv_cache.CSVCache.save`` — the OHLCV source of truth. A failed write
    must log at ERROR, like the sibling refusals in the same function.
  * ``ContentDatabase.save_report`` — the Deribit barred-prose gate. The
    check THROWING is not the check passing; a failure must be logged,
    not swallowed.
  * MFE/MAE after a trade close — two call sites (the HTTP close route and
    MCP ``trade_close``). No retry exists, so a throw is a permanent hole.

ERROR is what reaches Bugsink through the Sentry logging integration, so
these tests pin the LEVEL, with the real dependency made to throw — not a
mock sitting above the site under test.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# csv_cache.save — a failed atomic write is an ERROR, and the save returns
# ---------------------------------------------------------------------------

class TestCsvCacheWriteFailsLoud:
    def _cache(self, tmp_path):
        from csv_cache import CSVCache
        return CSVCache(cache_dir=str(tmp_path))

    def _df(self):
        idx = pd.date_range('2026-01-01', periods=5, freq='D')
        return pd.DataFrame(
            {'open': 1.0, 'high': 2.0, 'low': 0.5, 'close': 1.5, 'volume': 10.0}, index=idx,
        )

    def test_write_failure_logs_error_with_symbol_and_path(self, tmp_path, monkeypatch, caplog):
        cache = self._cache(tmp_path)

        def _boom(path, df):
            raise OSError("disk full (simulated)")
        monkeypatch.setattr(cache, '_atomic_write_csv', _boom)

        with caplog.at_level(logging.ERROR, logger='csv_cache'):
            cache.save('BTC', self._df(), timeframe='daily', is_crypto=True)

        errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errs, "a failed OHLCV write must be an ERROR (Bugsink), not a warning"
        msg = errs[0].getMessage()
        assert 'BTC' in msg and 'daily' in msg and 'disk full' in msg
        assert errs[0].exc_info is not None

    def test_healthy_write_is_silent(self, tmp_path, caplog):
        cache = self._cache(tmp_path)
        with caplog.at_level(logging.WARNING, logger='csv_cache'):
            cache.save('BTC', self._df(), timeframe='daily', is_crypto=True)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert cache.get_path('BTC', 'daily').exists()


# ---------------------------------------------------------------------------
# save_report — the barred-prose gate crashing is an ERROR, save still lands
# ---------------------------------------------------------------------------

@pytest.fixture
def content_db(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_TYPE', 'sqlite')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    import carrac.db_pool as _dp
    _dp._db_type = None
    if hasattr(_dp._sqlite_local, 'conn') and _dp._sqlite_local.conn is not None:
        try:
            _dp._sqlite_local.conn.close()
        except Exception:
            pass
        _dp._sqlite_local.conn = None
    from carrac.content_db import ContentDatabase
    db = ContentDatabase(db_path=str(tmp_path / 'drawings.db'))
    yield db
    if hasattr(_dp._sqlite_local, 'conn') and _dp._sqlite_local.conn is not None:
        try:
            _dp._sqlite_local.conn.close()
        except Exception:
            pass
        _dp._sqlite_local.conn = None


class TestBarredProseGateCrashIsLoud:
    def test_gate_crash_logs_error_and_report_still_saves(self, content_db, monkeypatch, caplog):
        import carrac.compliance.deribit_gate as gate

        def _boom(content, context=None):
            raise RuntimeError("gate regressed (simulated)")
        monkeypatch.setattr(gate, 'alert_if_barred_prose', _boom)

        with caplog.at_level(logging.ERROR, logger='carrac.content_db'):
            report_id = content_db.save_report(
                content='## Story\nBTC steady.\n', assets_included=['BTC'],
                generation_time_ms=1, report_type='morning',
            )
        errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errs, "the compliance gate CRASHING must be an ERROR (Bugsink)"
        assert 'morning' in errs[0].getMessage() and 'did not run' in errs[0].getMessage()
        assert errs[0].exc_info is not None
        assert content_db.get_report_by_id(report_id) is not None  # save still lands

    def test_healthy_gate_is_silent(self, content_db, caplog):
        with caplog.at_level(logging.WARNING, logger='carrac.content_db'):
            content_db.save_report(
                content='## Story\nBTC steady.\n', assets_included=['BTC'],
                generation_time_ms=1, report_type='morning',
            )
        assert not [r for r in caplog.records
                    if r.levelno >= logging.WARNING and 'barred-prose' in r.getMessage()]


# ---------------------------------------------------------------------------
# MFE/MAE after close — all three copies agree (sibling sweep as a guard)
# ---------------------------------------------------------------------------

_MFE_SITES = {
    'carrac/fastapi_app/routers/journal.py': 1,   # POST /api/journal/trades/<id>/close
    'carrac/mcp/tools/journal.py': 1,              # MCP trade_close
}
_CALL = re.compile(r"calculate_and_store_mfe_mae\(db, (?:trade_id|tid)\)\s*\n\s*except Exception as (?:e|exc):\s*\n(.*?)\n\s*\n", re.S)


@pytest.mark.parametrize('rel, expected', sorted(_MFE_SITES.items()))
def test_every_post_close_mfe_site_logs_at_error(rel, expected):
    src = (ROOT / rel).read_text()
    blocks = _CALL.findall(src)
    assert len(blocks) == expected, f"{rel}: expected {expected} post-close MFE/MAE catch block(s), found {len(blocks)}"
    for body in blocks:
        assert 'logger.error(' in body, f"{rel}: the post-close MFE/MAE catch must log at ERROR, not warning:\n{body}"
        assert 'exc_info=True' in body, f"{rel}: keep the traceback — Bugsink needs it"
        assert 'logger.warning(' not in body
