"""Log levels for the worker's Redis writes in carrac.precompute."""
import logging
from unittest.mock import MagicMock, patch

from carrac import precompute


def _boom_redis():
    r = MagicMock()
    r.set.side_effect = ConnectionError('redis down')
    return r


def test_heartbeat_write_failure_is_an_error(caplog):
    with patch('carrac.caches._get_redis', return_value=_boom_redis()), \
         caplog.at_level(logging.DEBUG, logger=precompute.logger.name):
        precompute._write_heartbeat()
    levels = [r.levelno for r in caplog.records if 'heartbeat' in r.getMessage()]
    assert levels == [logging.ERROR]


def test_ltf_cache_write_failure_is_a_warning_naming_the_key(caplog):
    with caplog.at_level(logging.DEBUG, logger=precompute.logger.name):
        precompute._write_ltf_cache(_boom_redis(), 'mcd:ltf_asset:BTC', {'a': 1})
    recs = [r for r in caplog.records if 'mcd:ltf_asset:BTC' in r.getMessage()]
    assert [r.levelno for r in recs] == [logging.WARNING]


def test_ltf_cache_write_success_stores_json():
    r = MagicMock()
    precompute._write_ltf_cache(r, 'mcd:ltf_asset:BTC', {'a': 1})
    r.set.assert_called_once()
    assert r.set.call_args.args[0] == 'mcd:ltf_asset:BTC'
