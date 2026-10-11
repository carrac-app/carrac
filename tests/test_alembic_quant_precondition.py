"""The quant-schema precondition in scripts/alembic_stamp_if_fresh.py.

The quant schema is created only by `python -m quant.schema`, never by alembic,
yet two migrations ALTER quant tables while guarding only their columns. Without
this precondition a database reset that forgets the manual apply produces a bare
UndefinedTable partway through `alembic upgrade head`, or — worse — a database
that migrates fine but 500s on every quant endpoint until a smoke test catches
it. These tests pin the check that turns that into an early, explicit failure.
"""
import importlib.util
import os

import pytest

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'scripts', 'alembic_stamp_if_fresh.py',
)


def _load_module():
    spec = importlib.util.spec_from_file_location('alembic_stamp_if_fresh', _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCursor:
    """Minimal psycopg2-cursor stand-in.

    Answers the schema-existence probe with `schema_exists`, and the table
    listing with `tables`. Deliberately dumb — it asserts nothing about SQL
    text beyond which of the two queries is being asked.
    """

    def __init__(self, schema_exists, tables):
        self._schema_exists = schema_exists
        self._tables = tables
        self._last = None

    def execute(self, sql, *args):
        self._last = 'schemata' if 'information_schema.schemata' in sql else 'tables'

    def fetchone(self):
        assert self._last == 'schemata'
        return (self._schema_exists,)

    def fetchall(self):
        assert self._last == 'tables'
        return [(t,) for t in self._tables]


@pytest.fixture(scope='module')
def mod():
    return _load_module()


def test_healthy_quant_schema_reports_no_problem(mod):
    cur = FakeCursor(True, list(mod._QUANT_REQUIRED_TABLES) + ['price_daily', 'macro'])
    assert mod._check_quant_schema(cur) is None


def test_missing_schema_entirely_is_reported(mod):
    cur = FakeCursor(False, [])
    problem = mod._check_quant_schema(cur)
    assert problem is not None
    assert 'does not exist' in problem


def test_schema_present_but_tables_missing_is_reported(mod):
    """A reset database where CREATE SCHEMA ran, the tables did not."""
    cur = FakeCursor(True, ['price_daily'])
    problem = mod._check_quant_schema(cur)
    assert problem is not None
    for table in mod._QUANT_REQUIRED_TABLES:
        assert table in problem


def test_partial_tables_names_only_the_missing_ones(mod):
    cur = FakeCursor(True, ['correlations', 'sentiment', 'price_daily'])
    problem = mod._check_quant_schema(cur)
    assert 'signals' in problem and 'zscores' in problem
    # Present tables must not be listed as missing — the message is the whole
    # point of this check, so a misleading one is a failure.
    assert 'correlations' not in problem and 'sentiment' not in problem


def test_required_tables_match_what_the_migrations_actually_touch(mod):
    """Guard against the list drifting away from the migrations it protects.

    If a future migration alters another quant table, this list must grow or
    the precondition silently stops covering it.
    """
    versions_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'alembic', 'versions'
    )
    referenced = set()
    for name in os.listdir(versions_dir):
        if not name.endswith('.py'):
            continue
        with open(os.path.join(versions_dir, name)) as fh:
            body = fh.read()
        for token in ('ALTER TABLE quant.', 'ON quant.'):
            start = 0
            while (idx := body.find(token, start)) != -1:
                tail = body[idx + len(token):]
                table = ''
                for ch in tail:
                    if ch.isalnum() or ch == '_':
                        table += ch
                    else:
                        break
                if table:
                    referenced.add(table)
                start = idx + len(token)

    missing = referenced - set(mod._QUANT_REQUIRED_TABLES)
    assert not missing, (
        f"migrations touch quant tables not covered by the precondition: {sorted(missing)}. "
        f"Add them to _QUANT_REQUIRED_TABLES in scripts/alembic_stamp_if_fresh.py."
    )
