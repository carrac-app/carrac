"""Pydantic models for the journal write surface.

``POST/PATCH/DELETE /api/journal/*``, served by
``carrac/fastapi_app/routers/journal.py``.

The models enforce shape (``extra="forbid"``) and field limits. The routes
add the asset and timeframe patterns, the ``indicators_at_entry`` key
whitelist and ownership checks.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Shared literals
# ---------------------------------------------------------------------------

_EXIT_REASONS = (
    "manual",
    "tp_hit",
    "sl_hit",
    "trailing_stop",
    "time_based",
    "breakeven",
)

# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------


class TradeCreateRequest(BaseModel):
    """Body for ``POST /api/journal/trades``."""

    model_config = ConfigDict(extra="forbid")

    asset: str = Field(min_length=1, max_length=20)
    entry_price: float = Field(gt=0)
    direction: Literal["long", "short"] = "long"
    timeframe: str = Field(default="1D", max_length=5)
    target_price: Optional[float] = Field(default=None, gt=0)
    stop_price: Optional[float] = Field(default=None, gt=0)
    # Keys are whitelisted by the create route
    indicators_at_entry: Optional[dict[str, Any]] = None
    drawing_id: Optional[int] = Field(default=None, gt=0)
    scenario_id: Optional[int] = Field(default=None, gt=0)
    size: Optional[float] = Field(default=None, gt=0)
    fees: Optional[float] = Field(default=None, ge=0)
    confidence: Optional[int] = Field(default=None, ge=1, le=5)
    mood: Optional[int] = Field(default=None, ge=1, le=5)
    account_tag: Optional[str] = Field(default=None, max_length=30)
    setup_type: Optional[str] = Field(default=None, max_length=50)
    notes: Optional[str] = Field(default=None, max_length=10000)
    tags: Optional[list] = None
    checklist_results: Optional[list] = None


class TradeUpdateRequest(BaseModel):
    """Body for ``PATCH /api/journal/trades/{trade_id}``.

    All fields optional — partial update semantics.
    ``ai_review`` and ``trade_grade`` are intentionally absent:
    those are AI-only fields; ``extra="forbid"`` surfaces any attempt to
    set them as a 400.
    """

    model_config = ConfigDict(extra="forbid")

    entry_price: Optional[float] = Field(default=None, gt=0)
    target_price: Optional[float] = Field(default=None, gt=0)
    stop_price: Optional[float] = Field(default=None, gt=0)
    exit_price: Optional[float] = Field(default=None, gt=0)
    size: Optional[float] = Field(default=None, gt=0)
    fees: Optional[float] = Field(default=None, ge=0)
    pnl_currency: Optional[float] = None
    direction: Optional[Literal["long", "short"]] = None
    exit_reason: Optional[Literal[_EXIT_REASONS]] = None  # type: ignore[valid-type]
    timeframe: Optional[str] = Field(default=None, max_length=5)
    setup_type: Optional[str] = Field(default=None, max_length=50)
    account_tag: Optional[str] = Field(default=None, max_length=30)
    notes: Optional[str] = Field(default=None, max_length=10000)
    scenario_id: Optional[int] = Field(default=None, ge=1)
    session_id: Optional[int] = Field(default=None, ge=1)
    checklist_template_id: Optional[int] = Field(default=None, ge=1)
    confidence: Optional[int] = Field(default=None, ge=1, le=5)
    mood: Optional[int] = Field(default=None, ge=1, le=5)
    tags: Optional[list] = None
    mistake_tags: Optional[list] = None
    checklist_results: Optional[list] = None


class TradeCloseRequest(BaseModel):
    """Body for ``POST /api/journal/trades/{trade_id}/close``."""

    model_config = ConfigDict(extra="forbid")

    exit_price: float = Field(gt=0)
    exit_reason: Literal[_EXIT_REASONS] = "manual"  # type: ignore[valid-type]
    notes: Optional[str] = Field(default=None, max_length=10000)
    mistake_tags: Optional[list] = None
    tags: Optional[list] = None
    fees: Optional[float] = Field(default=None, ge=0)
    pnl_currency: Optional[float] = None
    size: Optional[float] = Field(default=None, gt=0)


class BulkTradeRequest(BaseModel):
    """Body for ``POST /api/journal/trades/bulk``."""

    model_config = ConfigDict(extra="forbid")

    trade_ids: list[int] = Field(min_length=1, max_length=100)
    action: Literal["update", "delete"] = "update"
    setup_type: Optional[str] = Field(default=None, max_length=50)
    tags: Optional[list] = None
    mistake_tags: Optional[list] = None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class SessionCreateRequest(BaseModel):
    """Body for ``POST /api/journal/sessions``."""

    model_config = ConfigDict(extra="forbid")

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    session_label: str = Field(default="default", max_length=30)
    pre_notes: Optional[str] = Field(default=None, max_length=10000)
    mood_pre: Optional[int] = Field(default=None, ge=1, le=5)


class SessionUpdateRequest(BaseModel):
    """Body for ``PATCH /api/journal/sessions/{session_id}``.

    All fields optional — partial update semantics.
    """

    model_config = ConfigDict(extra="forbid")

    post_notes: Optional[str] = Field(default=None, max_length=10000)
    pre_notes: Optional[str] = Field(default=None, max_length=10000)
    session_label: Optional[str] = Field(default=None, max_length=30)
    mood_pre: Optional[int] = Field(default=None, ge=1, le=5)
    mood_post: Optional[int] = Field(default=None, ge=1, le=5)
    rules_followed: Optional[list] = None


# ---------------------------------------------------------------------------
# Playbooks
# ---------------------------------------------------------------------------


class PlaybookCreateRequest(BaseModel):
    """Body for ``POST /api/journal/playbooks``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=50)
    setup_type: Optional[str] = Field(default=None, max_length=50)
    description: Optional[str] = Field(default=None, max_length=10000)
    ideal_conditions: Optional[str] = Field(default=None, max_length=10000)
    entry_rules: Optional[str] = Field(default=None, max_length=10000)
    exit_rules: Optional[str] = Field(default=None, max_length=10000)


class PlaybookUpdateRequest(BaseModel):
    """Body for ``PATCH /api/journal/playbooks/{playbook_id}``.

    All fields optional — partial update semantics.
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, max_length=50)
    setup_type: Optional[str] = Field(default=None, max_length=50)
    description: Optional[str] = Field(default=None, max_length=10000)
    ideal_conditions: Optional[str] = Field(default=None, max_length=10000)
    entry_rules: Optional[str] = Field(default=None, max_length=10000)
    exit_rules: Optional[str] = Field(default=None, max_length=10000)
    example_trade_ids: Optional[list] = None
    screenshot_ids: Optional[list] = None


# ---------------------------------------------------------------------------
# Checklists
# ---------------------------------------------------------------------------


class ChecklistRequest(BaseModel):
    """Body for ``POST /api/journal/checklists`` (upsert)."""

    model_config = ConfigDict(extra="forbid")

    setup_type: str = Field(min_length=1, max_length=50)
    # max_length=30 per catalogue; each item truncated to 200 chars server-side
    checklist_items: list[str] = Field(min_length=1, max_length=30)
    name: Optional[str] = Field(default=None, max_length=50)


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------


class GoalRequest(BaseModel):
    """Body for ``POST /api/journal/goals``."""

    model_config = ConfigDict(extra="forbid")

    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    goals: dict  # free-form goals object, upserted as-is


# ---------------------------------------------------------------------------
# Trade shares (kind='trade' on the share rail)
# ---------------------------------------------------------------------------


class TradeShareRequest(BaseModel):
    """Body for ``POST`` / ``PATCH`` ``/api/journal/trades/{id}/share``.

    Field-visibility toggles for the public trade-result share. All optional:
    an empty POST body mints with the defaults (levels ON, note OFF);
    explicit toggles on POST apply even when an active share already exists.
    PATCH updates only the supplied toggles and re-freezes the snapshot (an
    empty PATCH body = refresh-snapshot). PATCH — not PUT — because the
    strangler's ``/api/journal/trades/`` prefix routes {POST, PATCH, DELETE}.
    """

    model_config = ConfigDict(extra="forbid")

    include_levels: Optional[bool] = None
    include_note: Optional[bool] = None


__all__ = [
    "TradeCreateRequest",
    "TradeUpdateRequest",
    "TradeCloseRequest",
    "TradeShareRequest",
    "BulkTradeRequest",
    "SessionCreateRequest",
    "SessionUpdateRequest",
    "PlaybookCreateRequest",
    "PlaybookUpdateRequest",
    "ChecklistRequest",
    "GoalRequest",
]
