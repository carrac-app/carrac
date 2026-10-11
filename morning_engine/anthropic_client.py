"""Morning-engine Anthropic client factory — applies env defaults to the shared provider."""

from __future__ import annotations

from typing import Optional

from carrac.llm.morning_anthropic import MorningAnthropicProvider
from .config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL


def MorningAnthropicClient(api_key: Optional[str] = None, model: Optional[str] = None) -> MorningAnthropicProvider:
    return MorningAnthropicProvider(
        api_key=api_key or ANTHROPIC_API_KEY,
        model=model or ANTHROPIC_MODEL,
    )
