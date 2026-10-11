"""Drawings writes routing tests.

Exercises the strangler dispatcher to confirm POST /api/drawings,
POST /api/drawings/upsert, POST /api/drawings/bulk, and
DELETE /api/drawings/by-local-id flip to FastAPI, and that
PUT/DELETE /api/drawings/<id> are covered by the method-filtered prefix.

CSRF note: drawings are in ``_SKIP_PREFIXES`` of
``CarracCSRFMiddleware`` — no CSRF is enforced here, matching Flask's
``csrf.exempt`` on ``drawings_bp``.

Pydantic ``extra="forbid"`` shape errors surface as 400 with the canonical
error envelope. The ⚠️ HIGH RISK ``PUT /api/drawings/<id>`` path that passes
``**data`` to the DB layer is covered by an extra-field rejection test.
"""

from __future__ import annotations

import pytest

from carrac.asgi import (
    MIGRATED_WRITE_PATHS,
    MIGRATED_WRITE_PREFIXES,
    ExactPath,
    PrefixPath,
)


# ---------------------------------------------------------------------------
# Dispatcher shim unit tests — no network, no app context.
# ---------------------------------------------------------------------------


def test_post_drawings_exact_path_matches_post_only():
    ep = ExactPath("/api/drawings", app=None, methods=frozenset({"POST"}))
    base = {"type": "http", "path": "/api/drawings"}

    post_match, _ = ep.matches({**base, "method": "POST"})
    get_match, _ = ep.matches({**base, "method": "GET"})

    assert post_match.value > 0, "POST /api/drawings should match"
    assert get_match.value == 0, "GET /api/drawings should stay on Flask"


def test_drawings_id_prefix_matches_put_delete_only():
    pp = PrefixPath("/api/drawings/", app=None, methods=frozenset({"PUT", "DELETE"}))
    base = {"type": "http", "path": "/api/drawings/123"}

    put_match, _ = pp.matches({**base, "method": "PUT"})
    del_match, _ = pp.matches({**base, "method": "DELETE"})
    get_match, _ = pp.matches({**base, "method": "GET"})

    assert put_match.value > 0, "PUT /api/drawings/123 should match"
    assert del_match.value > 0, "DELETE /api/drawings/123 should match"
    assert get_match.value == 0, "GET /api/drawings/123 should stay on Flask"


def test_drawings_upsert_post_not_captured_by_prefix_put_delete():
    """POST /api/drawings/upsert must NOT be captured by the PUT/DELETE prefix."""
    pp = PrefixPath("/api/drawings/", app=None, methods=frozenset({"PUT", "DELETE"}))
    scope = {"type": "http", "path": "/api/drawings/upsert", "method": "POST"}
    match, _ = pp.matches(scope)
    assert match.value == 0, "POST /api/drawings/upsert must not match PUT/DELETE prefix"


def test_drawings_bulk_post_not_captured_by_prefix_put_delete():
    pp = PrefixPath("/api/drawings/", app=None, methods=frozenset({"PUT", "DELETE"}))
    scope = {"type": "http", "path": "/api/drawings/bulk", "method": "POST"}
    match, _ = pp.matches(scope)
    assert match.value == 0


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/drawings", "POST"),
        ("/api/drawings/upsert", "POST"),
        ("/api/drawings/bulk", "POST"),
        ("/api/drawings/by-local-id", "DELETE"),
    ],
)
def test_drawings_paths_listed_in_migrated_write_paths(path, method):
    entries = {(p, m) for p, m in MIGRATED_WRITE_PATHS}
    assert (path, frozenset({method})) in entries, (
        f"({path}, {method!r}) not in MIGRATED_WRITE_PATHS"
    )


def test_drawings_prefix_listed_in_migrated_write_prefixes():
    assert ("/api/drawings/", frozenset({"PUT", "DELETE"})) in MIGRATED_WRITE_PREFIXES


def test_drawings_by_local_id_delete_matches():
    ep = ExactPath(
        "/api/drawings/by-local-id", app=None, methods=frozenset({"DELETE"})
    )
    match, _ = ep.matches(
        {"type": "http", "path": "/api/drawings/by-local-id", "method": "DELETE"}
    )
    assert match.value > 0


# ---------------------------------------------------------------------------
# Pydantic model shape tests — extra="forbid" 400 cases.
# ---------------------------------------------------------------------------


def test_drawing_create_request_rejects_unknown_fields():
    """extra='forbid' is the primary guard against **data injection into the DB."""
    from carrac.fastapi_app.models.drawings import DrawingCreateRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DrawingCreateRequest(
            asset="BTC",
            timeframe="1D",
            drawing_type="horizontal_line",
            evil_column="drop table drawings",  # injection attempt
        )


def test_drawing_update_request_rejects_unknown_fields():
    """⚠️ HIGH RISK: PUT /api/drawings/<id> passes **data to db.drawing_update.
    extra='forbid' MUST block unknown columns from reaching the DB layer.
    """
    from carrac.fastapi_app.models.drawings import DrawingUpdateRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DrawingUpdateRequest(
            label="support",
            unknown_db_column="injected_value",
        )


def test_drawing_create_request_valid_case():
    from carrac.fastapi_app.models.drawings import DrawingCreateRequest

    req = DrawingCreateRequest(
        asset="BTCUSDT",
        timeframe="1D",
        drawing_type="horizontal_line",
        price_level=94000.0,
        color="#26a69a",
    )
    assert req.asset == "BTCUSDT"
    assert req.line_style == "solid"
    assert req.line_width == 2


def test_drawing_update_request_if_version_allowed():
    """if_version must be accepted on DrawingUpdateRequest (optimistic concurrency)."""
    from carrac.fastapi_app.models.drawings import DrawingUpdateRequest

    req = DrawingUpdateRequest(label="updated label", if_version=5)
    assert req.if_version == 5


def test_drawing_bulk_request_max_50():
    from carrac.fastapi_app.models.drawings import DrawingBulkItem, DrawingBulkRequest
    from pydantic import ValidationError

    drawings = [
        DrawingBulkItem(
            asset="BTC",
            timeframe="1D",
            drawing_type="horizontal_line",
        )
    ] * 51

    with pytest.raises(ValidationError):
        DrawingBulkRequest(drawings=drawings)


def test_drawing_delete_by_local_id_request_extra_forbidden():
    from carrac.fastapi_app.models.drawings import DrawingDeleteByLocalIdRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DrawingDeleteByLocalIdRequest(
            asset="BTC", local_id="overlay_123", unexpected="field"
        )


def test_drawing_upsert_request_valid():
    from carrac.fastapi_app.models.drawings import DrawingUpsertRequest

    req = DrawingUpsertRequest(
        asset="ETHUSDT",
        timeframe="4H",
        drawing_type="zone",
        price_level=2500.0,
        price_level_upper=2800.0,
        metadata={"local_id": "zone_abc"},
    )
    assert req.metadata["local_id"] == "zone_abc"


def test_drawing_create_invalid_color_rejected():
    from carrac.fastapi_app.models.drawings import DrawingCreateRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DrawingCreateRequest(
            asset="BTC",
            timeframe="1D",
            drawing_type="trendline",
            color="red",  # not a hex color
        )


def test_drawing_create_invalid_drawing_type_rejected():
    from carrac.fastapi_app.models.drawings import DrawingCreateRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DrawingCreateRequest(
            asset="BTC",
            timeframe="1D",
            drawing_type="box",  # not in ALLOWED_DRAWING_TYPES
        )
