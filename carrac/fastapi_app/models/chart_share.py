"""Pydantic model for the chart-share write surface.

``POST /api/chart/share``. The client sends the
symbol + timeframe (+ optional active-indicator descriptors and visible
drawing ids). Candles are NEVER client-supplied — the server fetches them via
``build_chart_payload``. ``extra="forbid"`` rejects unexpected fields.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ChartShareViewport(BaseModel):
    """Sharer's visible bar range, in EPOCH SECONDS (candle `time` units)."""

    model_config = ConfigDict(extra="forbid")

    from_ts: int = Field(ge=0)
    to_ts: int = Field(ge=0)


class ChartShareRequest(BaseModel):
    """Body for ``POST /api/chart/share``."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=30)
    timeframe: str = Field(min_length=1, max_length=5)
    # Client-captured chart image as a data URL — PNG ("data:image/png;
    # base64,…") or JPEG ("data:image/jpeg;base64,…"). Both are accepted
    # (generalized this from PNG-only, in anticipation of
    # a smaller-body JPEG re-encode path on the client). When present, the
    # card is this exact rendering (every drawing + indicator the user sees)
    # composited into the brand frame — full fidelity, no server re-render.
    # Validated + re-encoded server-side (the gate, see
    # decode_chart_capture's format->magic-bytes map). When absent (MCP /
    # headless), the server falls back to a candle render.
    # ~6M chars ≈ a ~4.4 MB PNG; the endpoint enforces a 4 MB decoded cap too.
    image: Optional[str] = Field(default=None, max_length=6_000_000)
    # Active-indicator descriptors (registry keys, optionally with params).
    # Stored in the snapshot for a future overlay phase of the candle path.
    # Sanitised server-side (carrac/plays/shares._sanitize_chart_indicators).
    indicators: Optional[list[Any]] = Field(default=None, max_length=40)
    # Visible drawing ids to freeze into the snapshot via _freeze_drawings.
    drawing_ids: Optional[list[int]] = Field(default=None, max_length=100)
    # Chart theme id at share time (display-options.js CHART_THEMES key) —
    # the interactive viewer restores it; unknown ids fall back to dark.
    theme: Optional[str] = Field(default=None, max_length=20)
    # Visible range at share time; the freezer windows candles around it.
    viewport: Optional[ChartShareViewport] = None


__all__ = ["ChartShareRequest", "ChartShareViewport"]
