"""``jsonify_iso``: Flask JSON responses with ISO 8601 dates.

It is shared by several blueprints, so it lives in its own module rather than
inside one of them.
"""

import ast
import json
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_dates_are_iso_8601(app):
    from carrac.json_response import jsonify_iso

    with app.app_context():
        resp = jsonify_iso({'at': datetime(2026, 10, 10, 9, 30, 5), 'on': date(2026, 10, 10)})
    assert resp.mimetype == 'application/json'
    assert json.loads(resp.get_data(as_text=True)) == {
        'at': '2026-10-10T09:30:05', 'on': '2026-10-10'}


def test_other_types_fall_back_to_flask(app):
    import uuid
    from carrac.json_response import jsonify_iso

    u = uuid.UUID('12345678-1234-5678-1234-567812345678')
    with app.app_context():
        resp = jsonify_iso({'id': u})
    assert json.loads(resp.get_data(as_text=True)) == {'id': str(u)}


def _imports(path):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.update(f'{node.module}.{a.name}' for a in node.names)
    return out


def test_blueprints_take_it_from_the_shared_module():
    for path in ('carrac/drawings/routes.py', 'carrac/ai/routes.py', 'carrac/journal/routes.py'):
        names = _imports(path)
        assert 'carrac.json_response.jsonify_iso' in names, path
        assert 'carrac.journal.routes.jsonify_iso' not in names, path
