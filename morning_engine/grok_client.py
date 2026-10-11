"""Morning-engine Grok client factory — applies env defaults to the shared provider."""

from __future__ import annotations

from typing import Optional

from carrac.llm.grok import GrokProvider
from .config import XAI_API_KEY, XAI_BASE_URL, GROK_MODEL


def GrokClient(api_key: Optional[str] = None,
               base_url: Optional[str] = None,
               model: Optional[str] = None) -> GrokProvider:
    return GrokProvider(
        api_key=api_key or XAI_API_KEY,
        base_url=base_url or XAI_BASE_URL,
        model=model or GROK_MODEL,
    )
