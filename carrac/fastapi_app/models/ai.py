"""Pydantic body models for the AI/SSE endpoints.

Shared by /api/ai/chat and the /api/journal/ai-* and other SSE endpoints.
Import from here so the FastAPI routers have one source of truth for
request shape validation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    """Body for POST /api/ai/chat (SSE)."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., max_length=1000)
    feature: Optional[str] = "chat"
    asset: Optional[str] = Field(default="", max_length=20)
    timeframe: Optional[str] = "1D"
    page_context: Optional[str] = "chart"
    conversation_id: Optional[int] = None
    client_drawings: Optional[List[Dict[str, Any]]] = None
    recipe_id: Optional[int] = None
    thesis_text: Optional[str] = Field(default="", max_length=2000)
    news_context: Optional[str] = Field(default="", max_length=1000)


class WeeklyReviewRequest(BaseModel):
    """Body for POST /api/ai/weekly-review (SSE).

    Flask handler in carrac/alerts/routes.py::test_notification_channel reads no body fields — the model
    exists only to enforce extra="forbid" so unknown fields are rejected.
    """

    model_config = ConfigDict(extra="forbid")


class JournalAiQueryRequest(BaseModel):
    """Body for POST /api/journal/ai-query (SSE)."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., max_length=500)


class JournalWeeklyReviewRequest(BaseModel):
    """Body for POST /api/journal/ai-review/weekly (non-streaming).

    Flask handler in carrac/alerts/routes.py::apply_notification_settings reads ``days`` with min 7 / max 90.
    """

    model_config = ConfigDict(extra="forbid")

    days: Optional[int] = Field(default=7, ge=7, le=90)


class JournalAiReviewBody(BaseModel):
    """Empty body sentinel for:
    - POST /api/journal/trades/<id>/ai-review
    - POST /api/journal/sessions/<id>/ai-summary

    Both Flask handlers take no body fields; model enforces extra="forbid"
    so clients sending unknown keys get a 400 rather than silent ignore.
    """

    model_config = ConfigDict(extra="forbid")


class ExecuteActionRequest(BaseModel):
    """Body for POST /api/ai/execute-action.

    Tool execution endpoint paired with the ``action`` SSE events emitted by
    /api/ai/chat. The Flask handler reads ``tool``, ``arguments``,
    optionally ``action_id``, ``asset``, ``conversation_id`` (the last
    three are accepted but unused server-side; clients pass them for
    correlation/telemetry).
    """

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(..., max_length=80)
    arguments: Dict[str, Any] = Field(default_factory=dict)
    action_id: Optional[str] = Field(default=None, max_length=80)
    asset: Optional[str] = Field(default=None, max_length=20)
    conversation_id: Optional[int] = None


class ContextPreviewRequest(BaseModel):
    """Body for POST /api/ai/context-preview.

    Deterministic preview of the assembled prompt for a recipe — no LLM
    call, no billing. Flask handler reads ``recipe_id``, ``asset``,
    ``timeframe``, ``thesis_text``. Other ChatRequest-shaped fields are
    accepted (extra="ignore" via field defaults) but unused by the
    Flask handler today; we keep the schema tight (extra="forbid").
    """

    model_config = ConfigDict(extra="forbid")

    recipe_id: Optional[int] = None
    asset: Optional[str] = Field(default="", max_length=20)
    timeframe: Optional[str] = Field(default="1D", max_length=10)
    thesis_text: Optional[str] = Field(default="", max_length=2000)
