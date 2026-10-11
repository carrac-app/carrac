"""Pydantic models for admin/billing JSON endpoints."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class FeedbackRequest(BaseModel):
    """POST /api/feedback — bug report or feature request."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., description="'bug' or 'feature'")
    area: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=2000)
    expected: str = Field(default="", max_length=2000)
    actual: str = Field(default="", max_length=2000)
    how_should_work: str = Field(default="", max_length=2000)
    extra: str = Field(default="", max_length=2000)


class AddAssetRequest(BaseModel):
    """POST /api/admin/assets — register a new chart pair."""

    model_config = ConfigDict(extra="forbid")

    pair: str = Field(..., description="'SYMBOL/TF' format, e.g. 'SOL/1D'")
    category: Optional[str] = None
    ratio_eligible: bool = False
