"""Shared confluence level metadata — single source of truth for weights,
plain-English names, and source families.

Used by carrac/scanner/confluence.py (engine), the /api/confluence endpoint,
and the MCP scanner_get_confluence_zones tool. The frontend receives friendly names
already expanded in the API response, so it needs no duplicate of this map.

Weights are HAND-TUNED starting values with timeframe baked in
(monthly > weekly > daily). They are calibratable later from scanner_history
forward returns (deferred).
"""

# Per-level weight. Top tier (3.0) = biggest magnets the operator named:
# Point of Control, the long MAs, and the monthly CPR pivot.
LEVEL_WEIGHTS = {
    'POC': 3.0, 'VAH': 2.0, 'VAL': 2.0,
    'SMA200': 3.0, 'EMA200': 3.0, 'EMA21': 2.0, 'EMA50': 1.5,
    'EMA30': 1.0, 'EMA60': 1.0,
    'PIVOT': 3.0, 'TC': 2.0, 'BC': 2.0,
    'S1': 1.0, 'S2': 1.0, 'S3': 1.0, 'R1': 1.0, 'R2': 1.0, 'R3': 1.0,
    'BB_U': 1.5, 'BB_L': 1.5, 'BB_M': 1.0,
    'SW_H': 2.0, 'SW_L': 2.0, 'SW_PH': 1.5, 'SW_PL': 1.5,
    'PMH': 2.0, 'PML': 2.0, 'MH': 2.0, 'ML': 2.0,
    'PWH': 1.5, 'PWL': 1.5, 'PDH': 1.0, 'PDL': 1.0,
}
DEFAULT_LEVEL_WEIGHT = 1.0

# Code → plain-English name. The decoder ring, killed at the root.
LABEL_NAMES = {
    'POC': 'Point of Control', 'VAH': 'Value Area High', 'VAL': 'Value Area Low',
    'SMA200': 'SMA 200', 'EMA200': 'EMA 200', 'EMA50': 'EMA 50', 'EMA21': 'EMA 21',
    'EMA30': 'EMA 30', 'EMA60': 'EMA 60',
    'PIVOT': 'Monthly Pivot', 'TC': 'Top Central', 'BC': 'Bottom Central',
    'S1': 'Support 1', 'S2': 'Support 2', 'S3': 'Support 3',
    'R1': 'Resistance 1', 'R2': 'Resistance 2', 'R3': 'Resistance 3',
    'BB_U': 'BB Upper', 'BB_M': 'BB Middle', 'BB_L': 'BB Lower',
    'SW_H': 'Swing High', 'SW_L': 'Swing Low',
    'SW_PH': 'Prev Swing High', 'SW_PL': 'Prev Swing Low',
    'PMH': 'Prev Month High', 'PML': 'Prev Month Low',
    'MH': 'Month High', 'ML': 'Month Low',
    'PWH': 'Prev Week High', 'PWL': 'Prev Week Low',
    'PDH': 'Prev Day High', 'PDL': 'Prev Day Low',
}

# Code → source family. Values MATCH the legacy source-type strings the engine
# emits in zone['sources'] (mcp/tools/scanner.py, scanner/routes.py and
# clients read these), so the field stays backward-compatible. The diversity
# bonus counts distinct families.
LEVEL_FAMILY = {
    'POC': 'vp', 'VAH': 'vp', 'VAL': 'vp',
    'SMA200': 'ma_levels', 'EMA200': 'ma_levels', 'EMA50': 'ma_levels', 'EMA21': 'ma_levels',
    'EMA30': 'ema_ribbon', 'EMA60': 'ema_ribbon',
    'PIVOT': 'cpr', 'TC': 'cpr', 'BC': 'cpr',
    'S1': 'cpr', 'S2': 'cpr', 'S3': 'cpr', 'R1': 'cpr', 'R2': 'cpr', 'R3': 'cpr',
    'BB_U': 'bb', 'BB_M': 'bb', 'BB_L': 'bb',
    'SW_H': 'structure', 'SW_L': 'structure', 'SW_PH': 'structure', 'SW_PL': 'structure',
    'PMH': 'key_levels', 'PML': 'key_levels', 'MH': 'key_levels', 'ML': 'key_levels',
    'PWH': 'key_levels', 'PWL': 'key_levels', 'PDH': 'key_levels', 'PDL': 'key_levels',
}
DEFAULT_FAMILY = 'other'

# Discrete display tiers derived from weighted zone strength.
TIER_STRONG_MIN = 5.0
TIER_MEDIUM_MIN = 2.5
# A single-source zone is only worth showing solo if its one level is >= this.
SOLO_MIN_WEIGHT = 2.0
# Strength multiplier: +10% per distinct family beyond the first.
DIVERSITY_BONUS = 0.10


def friendly(code):
    """Plain-English name for a level code (falls back to the code itself)."""
    return LABEL_NAMES.get(code, code)


def tier_for_strength(strength):
    """Discrete display tier from a weighted strength value."""
    if strength >= TIER_STRONG_MIN:
        return 'strong'
    if strength >= TIER_MEDIUM_MIN:
        return 'medium'
    return 'faint'
