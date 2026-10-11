"""The README's alert example must be a request the server accepts.

The SDK passes alert fields straight through, so drift shows up as a README
call the server would reject.  When the server source sits next to the SDK (a full
checkout) the example is validated against the server's own request model;
the field list below is the fallback and is checked against it too.
"""

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

from carrac import Client

README = Path(__file__).resolve().parents[1] / "README.md"
SERVER_MODEL = Path(__file__).resolve().parents[2] / "carrac" / "fastapi_app" / "models" / "alerts.py"
ALERT_CREATE_FIELDS = {
    "symbol", "condition", "price", "note", "frequency", "journal_id", "auto_close",
}


def _readme_kwargs():
    m = re.search(r"carrac\.alerts\.create\((.*)\)", README.read_text(encoding="utf-8"))
    assert m, "README quick-start has no alerts.create example"
    call = ast.parse(f"f({m.group(1)})", mode="eval").body
    return {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}


def test_readme_example_uses_known_fields():
    assert set(_readme_kwargs()) <= ALERT_CREATE_FIELDS


def test_readme_example_is_sent_unchanged():
    kwargs = _readme_kwargs()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(201, json={"alert": {"id": 1}})

    c = Client(api_key="k", base_url="http://localhost:5002")
    c._http = httpx.Client(base_url=c.base_url, transport=httpx.MockTransport(handler))
    c.alerts.create(**kwargs)
    assert json.loads(seen["body"]) == kwargs


def test_field_list_and_example_match_the_server_model():
    # Loaded by path: the server's top-level package is also called "carrac",
    # so importing it by name would collide with this SDK.
    pytest.importorskip("pydantic")
    if not SERVER_MODEL.exists():
        pytest.skip("server source not present (SDK checked out on its own)")
    spec = importlib.util.spec_from_file_location("_server_alert_models", SERVER_MODEL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # pydantic resolves annotations through it
    spec.loader.exec_module(module)
    request = module.AlertCreateRequest
    assert ALERT_CREATE_FIELDS == set(request.model_fields) - {"asset", "direction"}
    assert request(**_readme_kwargs())
