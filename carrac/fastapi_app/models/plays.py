"""Pydantic models for the plays write surface.

POST/PUT/DELETE ``/api/plays``. The Pydantic shell enforces
shape (``extra="forbid"``) and basic types. The deep semantic validation
(condition logic, asset regex, expiry parsing) stays in
``carrac.plays.routes._validate_play_input`` — both runtimes call it.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# Per ``carrac/plays/models.py``. Mirror the literals inline so the
# Pydantic schema is self-documenting and `extra="forbid"` shape errors
# surface in 400 responses with the canonical error envelope.
_PLAY_TYPES = ("directional", "macro", "event")
_PLAY_APPROACHES = ("single", "scaled", "defensive")
_PLAY_DIRECTIONS = ("long", "short", "neutral", "conditional")
_PLAY_TIMEFRAMES = ("swing", "position", "until_event", "open_ended")


class PlayCreateRequest(BaseModel):
    """Body for ``POST /api/plays``. Required: ``title``."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=200)
    thesis: Optional[str] = Field(default=None, max_length=5000)
    type: Optional[str] = Field(default=None, description=f"One of {_PLAY_TYPES}")
    approach: Optional[str] = Field(
        default=None, description=f"One of {_PLAY_APPROACHES}"
    )
    direction: Optional[str] = Field(
        default=None, description=f"One of {_PLAY_DIRECTIONS}"
    )
    primary_asset: Optional[str] = Field(default=None, max_length=20)
    related_assets: Optional[list[str]] = None
    trigger_asset: Optional[str] = Field(default=None, max_length=20)
    conditions: Optional[dict[str, Any]] = None
    invalidation: Optional[dict[str, Any]] = None
    timeframe: Optional[str] = Field(
        default=None, description=f"One of {_PLAY_TIMEFRAMES} (or empty)"
    )
    expiry: Optional[str] = None  # ISO 8601 string, validated server-side
    action_plan: Optional[str] = Field(default=None, max_length=5000)
    linked_drawing_ids: Optional[list[int]] = Field(default=None, max_length=20)
    report_tracking: Optional[bool] = None
    scorer_snapshot: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None


class PlayUpdateRequest(BaseModel):
    """Body for ``PUT /api/plays/{play_id}``. Every field optional —
    a partial update.
    """

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    thesis: Optional[str] = Field(default=None, max_length=5000)
    type: Optional[str] = None
    approach: Optional[str] = None
    direction: Optional[str] = None
    primary_asset: Optional[str] = Field(default=None, max_length=20)
    related_assets: Optional[list[str]] = None
    trigger_asset: Optional[str] = Field(default=None, max_length=20)
    conditions: Optional[dict[str, Any]] = None
    invalidation: Optional[dict[str, Any]] = None
    timeframe: Optional[str] = None
    expiry: Optional[str] = None
    action_plan: Optional[str] = Field(default=None, max_length=5000)
    linked_drawing_ids: Optional[list[int]] = Field(default=None, max_length=20)
    report_tracking: Optional[bool] = None
    metadata: Optional[dict[str, Any]] = None


class PlayResponse(BaseModel):
    """Wrapper returned by create/update: ``{"play": {...}}``."""

    model_config = ConfigDict(extra="allow")  # pass-through DB row

    play: dict[str, Any]


class DeleteOk(BaseModel):
    ok: bool = True


# Subroutes (status / link-drawing / evaluate / backtest).
# `_PLAY_STATUSES` mirrors ``carrac/plays/models.py::PLAY_STATUSES``.
_PLAY_STATUSES = (
    "draft",
    "watching",
    "acting",
    "resolved_won",
    "resolved_lost",
    "resolved_partial",
    "abandoned",
    "expired",
    "invalidated",
)


class PlayStatusRequest(BaseModel):
    """Body for ``PUT /api/plays/{play_id}/status``."""

    model_config = ConfigDict(extra="forbid")

    status: str
    resolution: Optional[str] = Field(default=None, max_length=5000)


class PlayLinkDrawingRequest(BaseModel):
    """Body for ``POST /api/plays/{play_id}/link-drawing`` and the unlink twin."""

    model_config = ConfigDict(extra="forbid")

    drawing_id: int = Field(..., ge=1)


class PlayBacktestRequest(BaseModel):
    """Body for ``POST /api/plays/{play_id}/backtest`` (premium tier).

    Server clamps to (50..2000) lookback and (1..60) hold so loose Pydantic
    bounds at the model layer are fine.
    """

    model_config = ConfigDict(extra="forbid")

    lookback_bars: Optional[int] = Field(default=None, ge=1)
    hold_bars: Optional[int] = Field(default=None, ge=1)


class PlayShareRequest(BaseModel):
    """Body for ``POST`` / ``PUT`` ``/api/plays/{play_id}/share``.

    Field-visibility toggles for the public share. All optional: an empty POST
    body mints with the defaults (thesis/conditions/plan ON, outcome OFF);
    explicit toggles on POST apply even when an active share already exists.
    PUT updates only the supplied toggles and re-freezes the snapshot from the
    play's current state (an empty PUT body = refresh-snapshot).
    """

    model_config = ConfigDict(extra="forbid")

    include_thesis: Optional[bool] = None
    include_conditions: Optional[bool] = None
    include_plan: Optional[bool] = None
    include_outcome: Optional[bool] = None


__all__ = [
    "PlayCreateRequest",
    "PlayUpdateRequest",
    "PlayResponse",
    "DeleteOk",
    "PlayStatusRequest",
    "PlayLinkDrawingRequest",
    "PlayBacktestRequest",
    "PlayShareRequest",
]
