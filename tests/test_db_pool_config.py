"""Postgres without a DATABASE_URL must fail loudly, not guess a credential."""
import pytest

from carrac import db_pool


@pytest.mark.parametrize('maker', [
    lambda: db_pool._make_pool(1, 2),
    lambda: db_pool.init_quant_pool(force=True),
    lambda: db_pool.init_reports_pool(force=True),
])
def test_postgres_pool_without_database_url_raises_naming_the_variable(monkeypatch, maker):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setattr(db_pool, '_quant_pool', None)
    monkeypatch.setattr(db_pool, '_reports_pool', None)
    with pytest.raises(RuntimeError, match='DATABASE_URL'):
        maker()
