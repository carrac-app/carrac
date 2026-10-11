"""
Unified AI pricing — single source of truth for all provider rates.

Used by:
- carrac/ai/billing.py (web AI queries, reserve/settle)
- morning_engine/anthropic_client.py (morning report generation)
- morning_engine/grok_client.py (X search, TTS)

Rates are per MTok (million tokens) unless otherwise noted.
Update this file when providers change pricing.
"""

# Anthropic Claude — per MTok, standard context (≤200K)
ANTHROPIC_RATES = {
    'claude-opus-4-6':   {'input': 5.0,  'cache_write': 6.25,  'cache_read': 0.50,  'output': 25.0},
    'claude-sonnet-4-6': {'input': 3.0,  'cache_write': 3.75,  'cache_read': 0.375, 'output': 15.0},
    'claude-haiku-4-5':  {'input': 1.0,  'cache_write': 1.25,  'cache_read': 0.10,  'output': 5.0},
}

# Aliases for older model IDs
ANTHROPIC_RATES['claude-opus-4-5'] = ANTHROPIC_RATES['claude-opus-4-6']
ANTHROPIC_RATES['claude-sonnet-4-5'] = ANTHROPIC_RATES['claude-sonnet-4-6']
ANTHROPIC_RATES['claude-haiku-4-5-20251001'] = ANTHROPIC_RATES['claude-haiku-4-5']

# Default fallback
ANTHROPIC_DEFAULT = 'claude-opus-4-6'

# xAI Grok — per MTok for chat, per 1M chars for TTS
GROK_RATES = {
    'chat': {'input': 0.20, 'output': 0.50},
    'tts':  {'per_1m_chars': 4.20},
}
