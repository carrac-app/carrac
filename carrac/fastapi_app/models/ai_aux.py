"""Pydantic models for the AI auxiliary endpoints.

Covers ``POST /api/ai/weekly-review`` (SSE) — the AI-powered weekly
trade review feature (Premium-only).

The shared AI/chat body models live in ``carrac/fastapi_app/models/ai.py``;
this module holds the models specific to the AI auxiliary endpoints.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class WeeklyReviewRequest(BaseModel):
    """Body for ``POST /api/ai/weekly-review``.

    The endpoint reads no request body — the
    review is generated from the last 7 days of the user's journal data
    stored server-side.  An empty (or absent) body is valid; we define
    the model so FastAPI's OpenAPI schema is accurate and ``extra="forbid"``
    rejects stray fields with 400.
    """

    model_config = ConfigDict(extra="forbid")
