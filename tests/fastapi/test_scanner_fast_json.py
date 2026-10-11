"""/api/scanner serialization fast-path.

A single warm-cache GET /api/scanner took ~10-11s in production because the ~12MB
(already JSON-native) payload was run through jsonable_encoder (a redundant
recursive per-node walk, ~9s) and then serialized again by JSONResponse. The
fix serializes ONCE with stdlib json. These tests pin that the fast path
produces output EQUIVALENT to the jsonable_encoder path (behaviour
unchanged) and preserves the heavy per-asset fields the scanner detail rows
render (criteria / confluences).
"""

import json

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from carrac.fastapi_app.routers.scanner import _fast_json_response

_PAYLOAD = {
    "results": [
        {
            "symbol": "BTC",
            "price": 65000.0,
            "best_verdict": "GOOD",
            "long": {
                "total": 8,
                "verdict": "GOOD",
                "criteria": {"trend": {"score": 2, "evidence": "higher highs"}},
                "confluences": [{"level": 64000, "kind": "vp", "note": "value area low"}],
            },
            "short": {"total": 3, "verdict": "WEAK"},
            "long_score": 8,
            "long_verdict": "GOOD",
        }
    ],
    "macro": {"regime": "Risk-On"},
    "computed_at": "2026-07-11T00:00:00Z",
    "event_risk": None,
    "stale": False,
    "stale_age_sec": None,
}


def test_fast_json_roundtrips_to_same_payload():
    resp = _fast_json_response(_PAYLOAD)
    assert resp.media_type == "application/json"
    assert json.loads(resp.body) == _PAYLOAD


def test_fast_json_equivalent_to_old_jsonable_encoder_path():
    # Old path: JSONResponse(jsonable_encoder(payload)). The new path must decode
    # to the SAME JSON — proving the fix changes speed, not output.
    old = json.loads(JSONResponse(content=jsonable_encoder(_PAYLOAD)).body)
    new = json.loads(_fast_json_response(_PAYLOAD).body)
    assert new == old


def test_fast_json_preserves_heavy_detail_fields():
    # Scanner detail rows render criteria + confluences — they must survive.
    r0 = json.loads(_fast_json_response(_PAYLOAD).body)["results"][0]
    assert r0["long"]["criteria"]["trend"]["evidence"] == "higher highs"
    assert r0["long"]["confluences"][0]["level"] == 64000


def test_fast_json_default_str_guards_stray_types():
    # Defensive: a stray non-JSON-native value must not 500 (default=str).
    class Weird:
        def __str__(self):
            return "weird"

    body = json.loads(_fast_json_response({"x": Weird()}).body)
    assert body["x"] == "weird"
