"""Pydantic models for the drawings write surface.

POST/PUT/DELETE ``/api/drawings`` and sub-paths.

⚠️ HIGH RISK note (Agent C catalogue): ``PUT /api/drawings/<id>`` passes
the validated data dict via ``**kwargs`` to the DB layer. ``extra="forbid"``
on ``DrawingUpdateRequest`` is the primary defence — any field not in the
model schema is rejected at the Pydantic boundary before it can reach the
DB call. Do NOT relax ``extra="forbid"`` on this model.

Drawing-specific CSRF: ``/api/drawings`` is in ``_SKIP_PREFIXES`` in
``CarracCSRFMiddleware``, so the handlers here do not enforce CSRF.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


_DRAWING_TYPES = Literal[
    "horizontal_line",
    "zone",
    "annotation",
    "trendline",
    "fibonacci",
    "position",
    "long_position",
    "short_position",
    "spot",
    "scenario",
    "liquidation_zone",
]

_LINE_STYLES = Literal["solid", "dashed", "dotted"]

# Hex color pattern: #RGB, #RRGGBB, or #RRGGBBAA
_HEX_COLOR_PATTERN = r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$"


# ---------------------------------------------------------------------------
# Create drawing (POST /api/drawings)
# ---------------------------------------------------------------------------


class DrawingCreateRequest(BaseModel):
    """Body for ``POST /api/drawings``."""

    model_config = ConfigDict(extra="forbid")

    asset: str = Field(min_length=1, max_length=20)
    timeframe: str = Field(min_length=1, max_length=10)
    drawing_type: _DRAWING_TYPES
    price_level: Optional[float] = Field(default=None, ge=0)
    price_level_upper: Optional[float] = Field(default=None, ge=0)
    label: Optional[str] = Field(default=None, max_length=500)
    color: Optional[str] = Field(
        default="#ff0000", pattern=_HEX_COLOR_PATTERN
    )
    line_style: _LINE_STYLES = "solid"
    line_width: int = Field(default=2, ge=1, le=10)
    metadata: Optional[dict] = None
    scenario_id: Optional[int] = Field(default=None, gt=0)


# ---------------------------------------------------------------------------
# Update drawing (PUT /api/drawings/<id>)
# ---------------------------------------------------------------------------


class DrawingUpdateRequest(BaseModel):
    """Body for ``PUT /api/drawings/{drawing_id}``.

    ⚠️ ``extra="forbid"`` is load-bearing: after validation the handler
    passes ``body.model_dump(exclude_unset=True)`` directly as ``**kwargs``
    to ``db.drawing_update()``. Any extra field would propagate to the DB
    layer unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    asset: Optional[str] = Field(default=None, min_length=1, max_length=20)
    timeframe: Optional[str] = Field(default=None, max_length=10)
    drawing_type: Optional[_DRAWING_TYPES] = None
    price_level: Optional[float] = Field(default=None, ge=0)
    price_level_upper: Optional[float] = Field(default=None, ge=0)
    label: Optional[str] = Field(default=None, max_length=500)
    color: Optional[str] = Field(default=None, pattern=_HEX_COLOR_PATTERN)
    line_style: Optional[_LINE_STYLES] = None
    line_width: Optional[int] = Field(default=None, ge=1, le=10)
    metadata: Optional[dict] = None
    # Optimistic concurrency — popped before **data passthrough to DB.
    if_version: Optional[int] = None


# ---------------------------------------------------------------------------
# Delete drawing with optimistic concurrency (DELETE /api/drawings/<id>)
# ---------------------------------------------------------------------------


class DrawingDeleteRequest(BaseModel):
    """Optional body for ``DELETE /api/drawings/{drawing_id}``.

    Optimistic concurrency accepts an ``If-Match`` header OR ``body.if_version``. The header path is handled via ``request.headers`` in the
    handler; this model covers the body path.
    """

    model_config = ConfigDict(extra="forbid")

    if_version: Optional[int] = None


# ---------------------------------------------------------------------------
# Upsert drawing (POST /api/drawings/upsert)
# ---------------------------------------------------------------------------


class DrawingUpsertRequest(BaseModel):
    """Body for ``POST /api/drawings/upsert``.

    Same shape as create. ``metadata.local_id`` is required for matching —
    validated in the handler, not here (it answers 400 with
    ``{"success": False, "error": "metadata.local_id required"}``).
    """

    model_config = ConfigDict(extra="forbid")

    asset: str = Field(min_length=1, max_length=20)
    timeframe: str = Field(min_length=1, max_length=10)
    drawing_type: _DRAWING_TYPES
    price_level: Optional[float] = Field(default=None, ge=0)
    price_level_upper: Optional[float] = Field(default=None, ge=0)
    label: Optional[str] = Field(default=None, max_length=500)
    color: Optional[str] = Field(
        default="#ff0000", pattern=_HEX_COLOR_PATTERN
    )
    line_style: _LINE_STYLES = "solid"
    line_width: int = Field(default=2, ge=1, le=10)
    metadata: Optional[dict] = None
    scenario_id: Optional[int] = Field(default=None, gt=0)


# ---------------------------------------------------------------------------
# Bulk create drawings (POST /api/drawings/bulk)
# ---------------------------------------------------------------------------


class DrawingBulkItem(BaseModel):
    """Single drawing in a bulk-create payload."""

    model_config = ConfigDict(extra="forbid")

    asset: str = Field(min_length=1, max_length=20)
    timeframe: str = Field(min_length=1, max_length=10)
    drawing_type: _DRAWING_TYPES
    price_level: Optional[float] = Field(default=None, ge=0)
    price_level_upper: Optional[float] = Field(default=None, ge=0)
    label: Optional[str] = Field(default=None, max_length=500)
    color: Optional[str] = Field(
        default="#ff0000", pattern=_HEX_COLOR_PATTERN
    )
    line_style: _LINE_STYLES = "solid"
    line_width: int = Field(default=2, ge=1, le=10)
    metadata: Optional[dict] = None


class DrawingBulkRequest(BaseModel):
    """Body for ``POST /api/drawings/bulk``."""

    model_config = ConfigDict(extra="forbid")

    drawings: list[DrawingBulkItem] = Field(max_length=50)
    replace: bool = False


# ---------------------------------------------------------------------------
# Delete by local-id (DELETE /api/drawings/by-local-id)
# ---------------------------------------------------------------------------


class DrawingDeleteByLocalIdRequest(BaseModel):
    """Body for ``DELETE /api/drawings/by-local-id``."""

    model_config = ConfigDict(extra="forbid")

    asset: str
    local_id: str


__all__ = [
    "DrawingCreateRequest",
    "DrawingUpdateRequest",
    "DrawingDeleteRequest",
    "DrawingUpsertRequest",
    "DrawingBulkItem",
    "DrawingBulkRequest",
    "DrawingDeleteByLocalIdRequest",
]
