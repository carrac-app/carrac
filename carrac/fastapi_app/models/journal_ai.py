"""Pydantic models for the journal AI endpoints.

Covers the 4 journal AI subroutes ported to FastAPI:
- ``POST /api/journal/trades/{trade_id}/ai-review``  — non-SSE
- ``POST /api/journal/sessions/{session_id}/ai-summary`` — non-SSE
- ``POST /api/journal/ai-review/weekly`` — non-SSE
- ``POST /api/journal/ai-query`` — SSE (streaming NL query)

The shared AI/chat body models live in ``carrac/fastapi_app/models/ai.py``;
this module holds the models specific to the journal AI endpoints.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class JournalAiQueryRequest(BaseModel):
    """Body for ``POST /api/journal/ai-query`` (streaming NL query).

    ``question`` is mandatory; an empty string is rejected
    with 400.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., min_length=1, max_length=500)


class JournalWeeklyReviewRequest(BaseModel):
    """Body for ``POST /api/journal/ai-review/weekly``.

    ``days`` controls the look-back window.  The default is 7 and the
    range is 7–90, enforced with
    Pydantic so invalid values are rejected before the handler runs.
    """

    model_config = ConfigDict(extra="forbid")

    days: Optional[int] = Field(default=7, ge=7, le=90)


class JournalAiReviewBody(BaseModel):
    """Empty body for path-parameterised AI endpoints.

    ``POST /api/journal/trades/<id>/ai-review`` and
    ``POST /api/journal/sessions/<id>/ai-summary`` carry no body
    (all context is resolved from path params + server-side DB).
    ``extra="forbid"`` rejects stray fields with 400.
    """

    model_config = ConfigDict(extra="forbid")
