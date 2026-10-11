"""
Context block registry — self-contained data fetchers for AI prompt assembly.

Each block produces a text chunk for the LLM prompt. Blocks are composable:
users select, configure, and prioritize them via Context Recipes.
"""

import logging
from dataclasses import dataclass
from typing import Dict

logger = logging.getLogger('carrac.ai.context_blocks')

# Token estimation: ~4 chars per token (conservative)
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Rough token count from text length."""
    return max(1, len(text) // _CHARS_PER_TOKEN) if text else 0


@dataclass
class ContextBlock:
    id: str
    name: str
    description: str
    category: str  # 'market' | 'technical' | 'macro' | 'personal' | 'on-chain'
    default_enabled: bool
    config_schema: dict
    token_estimate: int  # rough estimate shown in UI
    tier_minimum: str  # 'free' | 'standard' | 'pro' | 'premium'

    def fetch(self, user_id: int, asset: str, timeframe: str, config: dict) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Block implementations
# ---------------------------------------------------------------------------

class PriceBlock(ContextBlock):
    """Current price, 24h change, and basic info."""

    def __init__(self):
        super().__init__(
            id='price', name='Current Price & Change', category='market',
            description='Latest price, 24h change, and real-time data',
            default_enabled=True, config_schema={}, token_estimate=50,
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        if not asset:
            return ''
        from carrac.precompute import response_cache
        cached = response_cache.get('htf') or response_cache.get('ltf')
        if not cached:
            return ''
        asset_data = _find_asset(cached, asset)
        if not asset_data:
            return ''

        price = asset_data.get('latest_price', 0)
        parts = [f"Price: ${price:,.2f}"]

        rt = asset_data.get('realtime')
        if rt and rt.get('change_pct') is not None:
            parts.append(f"24h change: {rt['change_pct']:+.1f}%")

        fr = asset_data.get('funding_rate')
        if fr is not None:
            sentiment = "longs paying" if fr > 0.01 else ("shorts paying" if fr < -0.01 else "neutral")
            parts.append(f"Funding rate: {fr:.4f}% ({sentiment})")

        return '\n'.join(parts)


class OHLCVBlock(ContextBlock):
    """Recent OHLCV bars with candle changes and range position."""

    def __init__(self):
        super().__init__(
            id='ohlcv', name='Recent OHLCV Bars', category='market',
            description='Recent candle data, range position, and volume trend',
            default_enabled=True, token_estimate=300,
            config_schema={
                'lookback_bars': {'type': 'number', 'default': 20, 'min': 5, 'max': 100,
                                  'label': 'Lookback bars'},
            },
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        if not asset:
            return ''
        # Reuse existing price action builder
        from carrac.ai.context_builder import _build_price_action_context
        return _build_price_action_context(asset, timeframe)


class IndicatorsBlock(ContextBlock):
    """Technical indicators from ResponseCache (Consensio, RSI, EMA, BB, ADX, CPR, etc.)."""

    def __init__(self):
        super().__init__(
            id='indicators', name='Technical Indicators', category='technical',
            description='Consensio, RSI, EMA Ribbon, BB, ADX, volume metrics',
            default_enabled=True, config_schema={}, token_estimate=300,
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        if not asset:
            return ''
        from carrac.precompute import response_cache
        from carrac.ai.context_builder import _detailed_asset_summary
        cached = response_cache.get('htf') or response_cache.get('ltf')
        if not cached:
            return ''
        asset_data = _find_asset(cached, asset)
        if not asset_data:
            return ''
        return _detailed_asset_summary(asset_data, timeframe)


class ConsensioBlock(ContextBlock):
    """Multi-TF Consensio alignment details."""

    def __init__(self):
        super().__init__(
            id='consensio', name='Multi-TF Consensio', category='technical',
            description='SMA alignment signals across 1D, 3D, 1W timeframes',
            default_enabled=False, config_schema={}, token_estimate=150,
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        if not asset:
            return ''
        from carrac.precompute import response_cache
        cached = response_cache.get('htf') or response_cache.get('ltf')
        if not cached:
            return ''
        asset_data = _find_asset(cached, asset)
        if not asset_data:
            return ''

        cons = asset_data.get('consensio', {})
        total = cons.get('total', {})
        lines = [f"Consensio total: {total.get('positions', 0)}/{total.get('max_positions', 9)}"]

        for tf in ('1D', '3D', '1W'):
            tf_data = cons.get(tf, {})
            if tf_data:
                sig = tf_data.get('signal', '?')
                pos = tf_data.get('positions', 0)
                max_pos = tf_data.get('max_positions', 3)
                lines.append(f"  {tf}: signal={sig}, positions={pos}/{max_pos}")

        return '\n'.join(lines)


class StructureBlock(ContextBlock):
    """Market structure (HH/HL/LH/LL swing analysis)."""

    def __init__(self):
        super().__init__(
            id='structure', name='Market Structure', category='technical',
            description='Swing-based trend: HH/HL (bullish) or LH/LL (bearish)',
            default_enabled=True, config_schema={}, token_estimate=100,
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        if not asset:
            return ''
        from carrac.precompute import response_cache
        cached = response_cache.get('htf') or response_cache.get('ltf')
        if not cached:
            return ''
        asset_data = _find_asset(cached, asset)
        if not asset_data:
            return ''

        struct = asset_data.get('structure')
        if not struct:
            return ''

        return (f"Structure: {struct.get('structure_trend', '?')}, "
                f"last pivot={struct.get('last_pivot_type', '?')}, "
                f"last high={struct.get('last_high_type', '?')}, "
                f"last low={struct.get('last_low_type', '?')}")


class CPRBlock(ContextBlock):
    """Monthly Central Pivot Range levels."""

    def __init__(self):
        super().__init__(
            id='cpr', name='CPR Levels', category='technical',
            description='Monthly pivot, TC/BC, S/R levels with distances',
            default_enabled=True, config_schema={}, token_estimate=80,
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        if not asset:
            return ''
        from carrac.precompute import response_cache
        cached = response_cache.get('htf') or response_cache.get('ltf')
        if not cached:
            return ''
        asset_data = _find_asset(cached, asset)
        if not asset_data:
            return ''

        cpr = asset_data.get('cpr')
        if not cpr or not cpr.get('pivot'):
            return ''

        price = asset_data.get('latest_price', 0)
        pivot = cpr['pivot']
        tc = cpr.get('tc', 0)
        bc = cpr.get('bc', 0)
        pivot_dist = (price - pivot) / pivot * 100 if pivot else 0

        lines = [
            f"Monthly CPR: pivot=${pivot:,.2f} ({pivot_dist:+.1f}% from price), "
            f"TC=${tc:,.2f}, BC=${bc:,.2f}, "
            f"position={cpr.get('price_position', '?')}, level={cpr.get('price_level', '?')}"
        ]

        sr_parts = []
        for level_name in ('r1', 'r2', 'r3', 's1', 's2', 's3'):
            val = cpr.get(level_name)
            if val and price:
                dist = (val - price) / price * 100
                sr_parts.append(f"{level_name.upper()}=${val:,.2f}({dist:+.1f}%)")
        if sr_parts:
            lines.append(f"CPR S/R: {', '.join(sr_parts)}")

        return '\n'.join(lines)


class KeyLevelsBlock(ContextBlock):
    """Key support/resistance levels (PDH/PDL, PWH/PWL, etc.)."""

    def __init__(self):
        super().__init__(
            id='key_levels', name='Key S/R Levels', category='technical',
            description='Previous day/week/month highs and lows',
            default_enabled=False, config_schema={}, token_estimate=100,
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        if not asset:
            return ''
        from carrac.precompute import response_cache
        cached = response_cache.get('htf') or response_cache.get('ltf')
        if not cached:
            return ''
        asset_data = _find_asset(cached, asset)
        if not asset_data:
            return ''

        kl = asset_data.get('key_levels')
        if not kl:
            return ''

        price = asset_data.get('latest_price', 0)
        parts = []
        for label, key in [('PDH', 'pdh'), ('PDL', 'pdl'), ('PWH', 'pwh'), ('PWL', 'pwl'),
                           ('PMH', 'pmh'), ('PML', 'pml'), ('MH', 'mh'), ('ML', 'ml')]:
            val = kl.get(key)
            if val and price:
                dist = (val - price) / price * 100
                parts.append(f"{label}=${val:,.2f}({dist:+.1f}%)")

        return f"Key Levels: {', '.join(parts)}" if parts else ''


class DrawingsBlock(ContextBlock):
    """User's chart drawings and annotations."""

    def __init__(self):
        super().__init__(
            id='drawings', name='My Drawings & Annotations', category='personal',
            description='Your trendlines, levels, zones, and position overlays',
            default_enabled=True, config_schema={}, token_estimate=200,
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        if not asset:
            return ''
        try:
            from carrac.content_db import get_content_db
            from carrac.ai.context_builder import _format_drawings
            db = get_content_db()
            drawings = db.get_drawings_with_all(asset=asset, timeframe=timeframe, user_id=user_id)

            # Merge client-side position drawings if provided
            client_drawings = config.get('_client_drawings', [])
            if client_drawings:
                position_types = ('position', 'long_position', 'short_position',
                                  'longPosition', 'shortPosition')
                has_client_positions = any(
                    cd.get('drawing_type', '') in position_types for cd in client_drawings
                )
                if has_client_positions:
                    drawings = [d for d in drawings
                                if d.get('drawing_type', '') not in position_types]
                for cd in client_drawings:
                    drawings.append({
                        'drawing_type': cd.get('drawing_type', 'position'),
                        'label': cd.get('label', ''),
                        'price_level': (cd.get('points', [{}])[0].get('value') if cd.get('points') else None),
                        'metadata': {'points': cd.get('points', [])},
                        'tps': cd.get('tps', []),
                    })

            if not drawings:
                return ''

            # Get current price for enrichment
            from carrac.precompute import response_cache
            cached = response_cache.get('htf') or response_cache.get('ltf')
            current_price = None
            if cached:
                ad = _find_asset(cached, asset)
                if ad:
                    current_price = ad.get('latest_price')

            return _format_drawings(drawings, current_price)
        except Exception as e:
            logger.debug(f"Drawings block error: {e}")
            return ''


class ScenarioBlock(ContextBlock):
    """Active scenario/position with child drawings."""

    def __init__(self):
        super().__init__(
            id='scenario', name='Active Play/Position', category='personal',
            description='Your plays with entry/SL/TP details',
            default_enabled=True, config_schema={}, token_estimate=150,
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        if not asset:
            return ''
        try:
            from carrac.content_db import get_content_db
            from carrac.ai.context_builder import _format_scenarios
            db = get_content_db()
            scenarios = db.drawing_list(
                asset=asset, drawing_type='scenario', user_id=user_id
            )
            if not scenarios:
                return ''

            from carrac.precompute import response_cache
            cached = response_cache.get('htf') or response_cache.get('ltf')
            current_price = None
            if cached:
                ad = _find_asset(cached, asset)
                if ad:
                    current_price = ad.get('latest_price')

            return _format_scenarios(db, scenarios, current_price)
        except Exception:
            return ''


class PlayBlock(ContextBlock):
    """Active plays — market hypotheses with thesis, conditions, and status."""

    def __init__(self):
        super().__init__(
            id='plays', name='Active Plays', category='personal',
            description='Market hypotheses with thesis, conditions, and status',
            default_enabled=True, config_schema={}, token_estimate=200,
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        try:
            from carrac.plays.models import play_get_digest
            data = play_get_digest(user_id)
            plays = data.get('plays', [])
            if not plays:
                return ''

            # Sort: relevant asset first if on chart page
            if asset:
                au = asset.upper()
                relevant = [p for p in plays if
                    au == (p.get('primary_asset') or '').upper() or
                    au == (p.get('trigger_asset') or '').upper() or
                    au in [a.upper() for a in (p.get('related_assets') or [])]]
                other = [p for p in plays if p not in relevant]
                plays = relevant + other

            # Treat user-authored play text as untrusted input.
            # escape_user_text() already wraps the value in <label>…</label>
            # tags and neutralises role-marker / injection patterns, so we
            # don't re-wrap here — just interpolate the escaped string.
            from carrac.grounding import escape_user_text as _esc
            lines = [f"Active Plays ({data['summary']}):"]
            for p in plays[:10]:
                safe_title = _esc(p.get('title', ''), 'play_title', max_chars=200)
                lines.append(f"• {safe_title} ({p['type']}/{p['approach']}) — {p['status']}")
                if p.get('thesis'):
                    safe_thesis = _esc(p['thesis'][:200], 'play_thesis', max_chars=200)
                    lines.append(f"  Thesis: {safe_thesis}")
                lines.append(f"  {p['direction']} {p.get('primary_asset', '?')}, conditions: {p['conditions_met']}")
                if p.get('action_plan'):
                    safe_plan = _esc(p['action_plan'][:150], 'play_action_plan', max_chars=150)
                    lines.append(f"  Plan: {safe_plan}")
            return '\n'.join(lines)
        except Exception:
            logger.exception('PlayBlock.fetch failed for user_id=%s asset=%s', user_id, asset)
            return ''


class ThesisBlock(ContextBlock):
    """User's free-text thesis — what they think and why."""

    def __init__(self):
        super().__init__(
            id='thesis', name='My Thesis', category='personal',
            description='Your current market thesis (free text)',
            default_enabled=True, token_estimate=100,
            config_schema={
                'text': {'type': 'text', 'default': '', 'label': 'Your thesis',
                         'placeholder': 'What do you think will happen and why?'},
            },
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        text = config.get('text', '').strip()
        if not text:
            return ''
        # Escape the free-text thesis before it hits the prompt.
        # escape_user_text already wraps in <trader_thesis>…</trader_thesis>.
        from carrac.grounding import escape_user_text as _esc
        return f"Trader's thesis: {_esc(text[:2000], 'trader_thesis', max_chars=2000)}"


class MacroBlock(ContextBlock):
    """Macro context: FRED data, regime, Fear & Greed."""

    def __init__(self):
        super().__init__(
            id='macro', name='Macro Context (FRED)', category='macro',
            description='M2, DXY, Fed Funds, yield data from FRED',
            default_enabled=True, token_estimate=200,
            config_schema={
                'include': {'type': 'multi_select', 'default': ['m2', 'fed_funds', 'dxy'],
                            'options': ['m2', 'fed_funds', 'dxy', '10y_yield', 'yield_spread'],
                            'label': 'Include indicators'},
            },
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        # Tier 1 always-on context already includes macro data via get_shared_context().
        # Only emit here when used in recipe mode (where Tier 1 may not be included).
        # In standard 3-tier assembly, return empty to avoid duplication.
        return ''


class RegimeBlock(ContextBlock):
    """Market regime classification and Fear & Greed index."""

    def __init__(self):
        super().__init__(
            id='regime', name='Market Regime & F&G', category='macro',
            description='Risk-on/off regime score and Fear & Greed index',
            default_enabled=True, config_schema={}, token_estimate=100,
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        lines = []
        try:
            from carrac.caches import get_cached_regime
            regime = get_cached_regime()
            if regime and regime.get('regime') != 'N/A':
                lines.append(f"Market Regime: {regime['regime']} (score {regime['score']})")
        except Exception:
            pass

        try:
            from carrac.analysis import fetch_fear_greed_index
            fg = fetch_fear_greed_index()
            if fg and fg.get('value'):
                lines.append(f"Fear & Greed: {fg['value']} ({fg['classification']})")
        except Exception:
            pass

        return '\n'.join(lines)


class SentimentBlock(ContextBlock):
    """On-chain sentiment: funding rate, long/short ratio, MVRV Z-score."""

    def __init__(self):
        super().__init__(
            id='sentiment', name='Funding, L/S Ratio, MVRV', category='on-chain',
            description='BTC funding rate, long ratio, MVRV Z-score (3-day)',
            default_enabled=False, config_schema={}, token_estimate=100,
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        try:
            from carrac.content_db import get_content_db
            db = get_content_db()
            summary = db.get_sentiment_daily_summary(days=3)
            if not summary:
                return ''
            latest = summary[-1]
            parts = []
            if latest.get('funding_rate') is not None:
                parts.append(f"BTC funding: {latest['funding_rate']:.4f}%")
            if latest.get('btc_long_pct') is not None:
                parts.append(f"Long ratio: {latest['btc_long_pct']:.0f}%")
            if latest.get('mvrv_zscore') is not None:
                parts.append(f"MVRV Z: {latest['mvrv_zscore']:.2f}")
            return 'Sentiment: ' + ', '.join(parts) if parts else ''
        except Exception:
            return ''


class QuantEngineBlock(ContextBlock):
    """Quant Engine: HMM regime, z-score anomalies, cross-asset correlations, lead-lag signals."""

    def __init__(self):
        super().__init__(
            id='quant_engine', name='Quant Engine Overview', category='macro',
            description='HMM regime, z-score anomalies, cross-asset correlations, lead-lag signals',
            default_enabled=True, config_schema={}, token_estimate=300,
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        try:
            from carrac.db_pool import get_quant_conn as get_conn
        except ImportError:
            return ''

        # Thread focus asset through. Default to BTC when caller
        # didn't supply one (e.g. chat without an active asset).
        focus = (asset or 'BTC').upper()

        lines = []
        try:
            with get_conn() as conn:
                cur = conn.cursor()

                # ── Regime with phase ─────────────────────────
                cur.execute("""
                    SELECT ts, value, metadata FROM quant.signals
                    WHERE source = 'regime_hmm' AND metric = 'REGIME'
                      AND asset = '_GLOBAL_'
                    ORDER BY ts DESC LIMIT 1
                """)
                row = cur.fetchone()
                if row:
                    meta = row['metadata'] or {}
                    label = (meta.get('label') or 'unknown').lower()
                    prob = round((meta.get('probability') or 0) * 100)
                    dur = meta.get('duration_days', 0) or 0
                    since = meta.get('since', '?')
                    # Expected duration from the transition matrix: 1/(1-p_stay)
                    # — the MEAN of a geometric distribution implied by the
                    # CURRENT transition odds, not a historical median (see
                    # carrac/facts/bands.py::_compute_regime_clock for the
                    # genuine historical/median figure; this display string so the two stop sharing wording).
                    expected_run = None
                    try:
                        tm = meta.get('transition_matrix') or []
                        state_idx = int(row['value']) if row['value'] is not None else None
                        if state_idx in (0, 1) and tm and len(tm) > state_idx:
                            p_stay = float(tm[state_idx][state_idx])
                            if 0 < p_stay < 1:
                                expected_run = round(1.0 / (1.0 - p_stay))
                    except (IndexError, TypeError, ValueError):
                        pass
                    phase = ''
                    if expected_run and dur:
                        frac = dur / expected_run
                        if frac < 0.3:
                            phase = 'early'
                        elif frac < 0.7:
                            phase = 'mid'
                        elif frac < 1.0:
                            phase = 'mature'
                        else:
                            phase = 'late-stage'
                    phase_str = (f" — {phase} ({dur}d elapsed vs ~{expected_run}d expected duration, "
                                 f"from current transition odds)"
                                 if phase else f" — {dur} days since {since}")
                    lines.append(f"Regime: {label.upper()} ({prob}% confidence){phase_str}")

                # ── Active anomaly cluster ────────────────────
                cur.execute("""
                    SELECT ts, zscore, metadata FROM quant.anomalies
                    WHERE domain = 'cluster' AND metric = 'MULTI_DOMAIN'
                      AND resolved_at IS NULL
                    ORDER BY ts DESC LIMIT 1
                """)
                crow = cur.fetchone()
                if crow:
                    cmeta = crow['metadata'] or {}
                    extreme_count = int(crow['zscore'])
                    domains = cmeta.get('domains_affected', 0)
                    metrics = cmeta.get('metrics', []) or []
                    metric_parts = []
                    for m in metrics[:5]:
                        if isinstance(m, dict):
                            metric_parts.append(f"{m.get('metric','?')} {float(m.get('zscore',0)):+.1f}σ")
                        else:
                            metric_parts.append(str(m))
                    lines.append(
                        f"Anomaly cluster ACTIVE: {extreme_count} extremes across "
                        f"{domains} domain{'s' if domains != 1 else ''}"
                        + (f" — {', '.join(metric_parts)}" if metric_parts else ''))

                # ── Per-metric extremes (|z|>=2, 90d, dedup latest) ────
                cur.execute("""
                    SELECT z.metric, z.asset, z.zscore
                    FROM quant.zscores z
                    INNER JOIN (
                        SELECT domain, metric, asset, MAX(ts) as max_ts
                        FROM quant.zscores WHERE window_days = 90
                        GROUP BY domain, metric, asset
                    ) latest
                        ON z.ts = latest.max_ts AND z.domain = latest.domain
                        AND z.metric = latest.metric AND z.asset = latest.asset
                    WHERE z.window_days = 90 AND ABS(z.zscore) >= 2.0
                    ORDER BY ABS(z.zscore) DESC LIMIT 5
                """)
                anoms = cur.fetchall()
                if anoms:
                    parts = [f"{r['metric']}{':' + r['asset'] if r['asset'] != '_GLOBAL_' else ''} "
                             f"({float(r['zscore']):+.1f}\u03C3)" for r in anoms]
                    lines.append(f"Anomalies ({len(anoms)} extreme): {', '.join(parts)}")

                # ── Top significant correlations ──────────────
                # Only surface q<0.05; surface q inline for transparency.
                cur.execute("""
                    SELECT metric_a, metric_b, correlation, q_value, significant
                    FROM quant.correlations
                    WHERE (metric_a = %s OR metric_b = %s)
                      AND method = 'spearman' AND window_days = 60
                      AND ts = (SELECT MAX(ts) FROM quant.correlations WHERE method = 'spearman')
                      AND ABS(correlation) >= 0.15
                      AND significant = true
                    ORDER BY ABS(correlation) DESC LIMIT 3
                """, (focus, focus))
                corrs = cur.fetchall()
                if corrs:
                    parts = []
                    for r in corrs:
                        other = r['metric_b'] if r['metric_a'] == focus else r['metric_a']
                        q = r.get('q_value')
                        q_str = f" q={float(q):.3f}" if q is not None else ''
                        parts.append(f"{other} {float(r['correlation']):+.2f}{q_str}")
                    lines.append(f"Top {focus} correlations (60d, significant): {', '.join(parts)}")

                # ── Lead-lag with projected move + CI ─────────
                cur.execute("""
                    SELECT metric_a, window_days AS lag, correlation,
                           ci_low, ci_high
                    FROM quant.correlations
                    WHERE metric_b = %s AND method = 'leadlag'
                      AND ts = (SELECT MAX(ts) FROM quant.correlations WHERE method = 'leadlag')
                    ORDER BY metric_a, ABS(correlation) DESC
                """, (focus,))
                ll_rows = cur.fetchall()
                from collections import defaultdict
                by_leader = defaultdict(list)
                for r in ll_rows:
                    by_leader[r['metric_a']].append(r)
                if by_leader:
                    leader_keys = list(by_leader.keys())
                    cur.execute("""
                        SELECT DISTINCT ON (metric) metric, zscore
                        FROM quant.zscores
                        WHERE metric = ANY(%s) AND window_days = 90
                        ORDER BY metric, ts DESC
                    """, (leader_keys,))
                    leader_z = {zr['metric']: float(zr['zscore']) for zr in cur.fetchall()}

                    entries = []
                    for leader, rows in by_leader.items():
                        best = max(rows, key=lambda r: abs(float(r['correlation'])))
                        r_val = float(best['correlation'])
                        if abs(r_val) < 0.15:
                            continue
                        lag = int(best['lag'])
                        lz = leader_z.get(leader)
                        parts = [f"r={r_val:+.2f}"]
                        if lz is not None:
                            proj = r_val * lz
                            parts.append(f"leader {lz:+.2f}σ → projected {proj:+.2f}σ")
                            if best.get('ci_low') is not None and best.get('ci_high') is not None:
                                lo, hi = sorted([float(best['ci_low']) * lz, float(best['ci_high']) * lz])
                                parts.append(f"95% CI [{lo:+.2f}σ, {hi:+.2f}σ]")
                        if lag > 0:
                            head = f"{leader} leads {focus} by {lag}d"
                        elif lag < 0:
                            head = f"{focus} leads {leader} by {abs(lag)}d"
                        else:
                            head = f"{leader} ~ {focus} (contemporaneous)"
                        entries.append(f"{head} ({', '.join(parts)})")
                        if len(entries) >= 3:
                            break
                    if entries:
                        lines.append("Lead-lag: " + '; '.join(entries))

                # ── Transmission chains ───────────────────────
                cur.execute("""
                    SELECT metric, value, metadata
                    FROM quant.signals
                    WHERE source = 'chain_strength' AND asset = '_GLOBAL_'
                      AND ts = (SELECT MAX(ts) FROM quant.signals WHERE source = 'chain_strength')
                """)
                chain_rows = cur.fetchall()
                if chain_rows:
                    import json as _json
                    chain_parts = []
                    for cr in chain_rows:
                        cmeta = cr['metadata'] or {}
                        if isinstance(cmeta, str):
                            try:
                                cmeta = _json.loads(cmeta)
                            except ValueError:
                                cmeta = {}
                        wk = cmeta.get('weakest_link') or {}
                        strength = float(cr['value'])
                        label = str(cr['metric']).upper()
                        suffix = ''
                        if wk.get('from') and wk.get('to') and wk.get('r') is not None:
                            suffix = f" (weakest {wk['from']}→{wk['to']} r={float(wk['r']):+.2f})"
                        chain_parts.append(f"{label} {strength:.2f}{suffix}")
                    lines.append("Chains: " + '; '.join(chain_parts))

        except Exception as e:
            logger.warning("QuantEngineBlock fetch failed: %s", e)
            return ''

        return '\n'.join(lines) if lines else ''


class BreadthBlock(ContextBlock):
    """Market breadth: bullish/bearish asset counts from Consensio."""

    def __init__(self):
        super().__init__(
            id='breadth', name='Market Breadth', category='macro',
            description='How many assets are bullish vs bearish',
            default_enabled=False, config_schema={}, token_estimate=80,
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        from carrac.precompute import response_cache
        cached = response_cache.get('htf')
        if not cached:
            return ''
        assets = cached.get('assets', [])
        if not assets:
            return ''

        bullish = sum(1 for a in assets if a.get('consensio', {}).get('total', {}).get('positions', 0) >= 4)
        bearish = sum(1 for a in assets if a.get('consensio', {}).get('total', {}).get('positions', 0) <= 1)
        total = len(assets)
        return f"Market breadth: {bullish}/{total} bullish, {bearish}/{total} bearish"


class ProfileBlock(ContextBlock):
    """Trader profile + journal reality check stats."""

    def __init__(self):
        super().__init__(
            id='profile', name='Trader Profile & Journal Stats', category='personal',
            description='Your stated trading style and journal performance stats',
            default_enabled=True, config_schema={}, token_estimate=200,
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        from carrac.ai.context_builder import build_trader_profile_context
        return build_trader_profile_context(user_id, feature=config.get('_feature', 'chat'))


class MorningReportBlock(ContextBlock):
    """Latest morning/afternoon analysis report."""

    def __init__(self):
        super().__init__(
            id='morning_report', name='Latest Morning Analysis', category='macro',
            description='Asset-specific + macro lines from morning report',
            default_enabled=False, config_schema={}, token_estimate=400,
            tier_minimum='member',
        )

    def fetch(self, user_id, asset, timeframe, config):
        if not asset:
            return ''
        from carrac.ai.context_builder import _build_report_context
        return _build_report_context(asset)


class FullReportBlock(ContextBlock):
    """Full morning report text for report follow-up conversations."""

    def __init__(self):
        super().__init__(
            id='full_report', name='Full Morning Report', category='macro',
            description='Complete morning report text for interactive Q&A',
            default_enabled=False, config_schema={}, token_estimate=1500,
            tier_minimum='member',
        )

    def fetch(self, user_id, asset, timeframe, config):
        try:
            from carrac.content_db import get_content_db
            db = get_content_db()

            report = db.get_latest_report('morning')
            if not report or not report.get('content'):
                return ''

            parts = []
            date_str = (report.get('created_at') or '')[:10]
            parts.append(f"MORNING ANALYSIS ({date_str})")

            content = report['content'].strip()
            # Safety cap at ~6000 chars (~1500 tokens)
            if len(content) > 6000:
                content = content[:6000] + '\n[... report truncated]'
            parts.append(content)

            # Personal section for subscribed users
            if user_id:
                personal = db.get_personal_section(report['id'], user_id)
                if personal and personal.get('content'):
                    p_content = personal['content'].strip()
                    if len(p_content) > 2000:
                        p_content = p_content[:2000] + '\n[... personal section truncated]'
                    parts.append(f"--- PERSONAL SECTION ---\n{p_content}")

            return '\n\n'.join(parts)

        except Exception as e:
            logger.debug(f"Full report block error: {e}")
            return ''


class MultiTFBlock(ContextBlock):
    """Multi-timeframe OHLCV data (1D + 3D + 1W)."""

    def __init__(self):
        super().__init__(
            id='multi_tf', name='Multi-TF Data (1D+3D+1W)', category='technical',
            description='Price action across daily, 3-day, and weekly timeframes',
            default_enabled=False, config_schema={}, token_estimate=500,
            tier_minimum='member',
        )

    def fetch(self, user_id, asset, timeframe, config):
        if not asset:
            return ''
        from carrac.ai.context_builder import _build_price_action_context
        parts = []
        for tf in ('1D', '3D', '1W'):
            text = _build_price_action_context(asset, tf)
            if text:
                parts.append(f"--- {tf} ---\n{text}")
        return '\n\n'.join(parts)


class VolumeProfileBlock(ContextBlock):
    """Volume profile key levels (POC, VAH, VAL)."""

    def __init__(self):
        super().__init__(
            id='volume_profile', name='Volume Profile Levels', category='technical',
            description='Point of Control, Value Area High/Low',
            default_enabled=False, config_schema={}, token_estimate=150,
            tier_minimum='premium',
        )

    def fetch(self, user_id, asset, timeframe, config):
        if not asset:
            return ''
        try:
            from carrac.precompute import response_cache
            cached = response_cache.get('htf') or response_cache.get('ltf')
            if not cached:
                return ''
            asset_data = _find_asset(cached, asset)
            if not asset_data:
                return ''

            vp = asset_data.get('volume_profile')
            if not vp:
                return ''

            price = asset_data.get('latest_price', 0)
            parts = []
            for key, label in [('poc', 'POC'), ('vah', 'VAH'), ('val', 'VAL')]:
                val = vp.get(key)
                if val and price:
                    dist = (val - price) / price * 100
                    parts.append(f"{label}=${val:,.2f}({dist:+.1f}%)")
            return f"Volume Profile: {', '.join(parts)}" if parts else ''
        except Exception:
            return ''


class CustomFormulasBlock(ContextBlock):
    """Values from user's custom indicator formulas."""

    def __init__(self):
        super().__init__(
            id='custom_formulas', name='Custom Indicator Values', category='technical',
            description='Current values of your custom formulas',
            default_enabled=False, config_schema={}, token_estimate=200,
            tier_minimum='premium',
        )

    def fetch(self, user_id, asset, timeframe, config):
        try:
            from user_db import user_db
            formulas = user_db.get_custom_formulas(user_id)
            if not formulas:
                return ''
            lines = [f"Custom formulas ({len(formulas)}):"]
            for f in formulas[:10]:
                lines.append(f"- {f['name']}: {f['formula'][:80]}")
            return '\n'.join(lines)
        except Exception:
            return ''


class JournalHistoryBlock(ContextBlock):
    """Recent trade journal entries."""

    def __init__(self):
        super().__init__(
            id='journal_history', name='Recent Journal Entries', category='personal',
            description='Your last 5 closed trades with outcomes',
            default_enabled=False, config_schema={}, token_estimate=300,
            tier_minimum='premium',
        )

    def fetch(self, user_id, asset, timeframe, config):
        try:
            from carrac.content_db import get_content_db
            db = get_content_db()
            history = db.get_trade_history(user_id, days=90, limit=5, asset=asset if asset else None)
            if not history:
                return ''
            lines = [f"Recent trades ({len(history)}):"]
            for t in history:
                direction = t.get('direction', '?').upper()
                a = t.get('asset', '?')
                entry = t.get('entry_price', 0)
                exit_p = t.get('exit_price', 0)
                rr = t.get('rr_actual')
                reason = t.get('exit_reason', '?')
                parts = [f"- {direction} {a} entry=${entry:,.2f}"]
                if exit_p:
                    parts.append(f"exit=${exit_p:,.2f}")
                if rr is not None:
                    parts.append(f"R={rr:+.1f}")
                parts.append(f"({reason})")
                lines.append(' '.join(parts))
            return '\n'.join(lines)
        except Exception:
            return ''


class CorrelatedAssetsBlock(ContextBlock):
    """State of correlated assets (BTC↔ETH, SPX, GOLD)."""

    def __init__(self):
        super().__init__(
            id='correlated_assets', name='Correlated Asset State', category='market',
            description='Price and signal state of related assets',
            default_enabled=False, config_schema={}, token_estimate=200,
            tier_minimum='premium',
        )

    # Default correlation groups
    CORRELATION_MAP = {
        'BTC': ['ETH', 'SPX', 'GOLD'],
        'ETH': ['BTC', 'SPX'],
        'SOL': ['BTC', 'ETH'],
        'SPX': ['BTC', 'GOLD'],
        'GOLD': ['SPX', 'BTC'],
    }

    def fetch(self, user_id, asset, timeframe, config):
        if not asset:
            return ''
        from carrac.precompute import response_cache
        from carrac.ai.context_builder import _summarize_asset
        cached = response_cache.get('htf')
        if not cached:
            return ''

        related = self.CORRELATION_MAP.get(asset, ['BTC', 'ETH', 'SPX'])
        related = [r for r in related if r != asset]

        lines = []
        for sym in related[:4]:
            ad = _find_asset(cached, sym)
            if ad:
                lines.append(_summarize_asset(ad))

        return '\n'.join(lines) if lines else ''


class AlertsBlock(ContextBlock):
    """Active price alerts for the current asset."""

    def __init__(self):
        super().__init__(
            id='alerts', name='Active Alerts', category='personal',
            description='Your active price alerts for this asset',
            default_enabled=False, config_schema={}, token_estimate=80,
            tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        if not asset:
            return ''
        try:
            from push_notifications import get_user_alerts
            from carrac.ai.context_builder import _format_alerts
            alerts = get_user_alerts(str(user_id), active_only=True)
            asset_alerts = [a for a in alerts if a.get('asset', '').upper() == asset.upper()]
            if not asset_alerts:
                return ''
            return _format_alerts(asset_alerts)
        except Exception:
            return ''


class ScorerBlock(ContextBlock):
    """Deterministic 0-12 score for the current asset with setup classification."""

    def __init__(self):
        super().__init__(
            id='scorer', name='Asset Score & Setup', category='technical',
            description='6-criteria deterministic score (0-12), setup type, mechanical reference levels',
            default_enabled=False, config_schema={
                'direction': {'type': 'select', 'default': 'auto',
                              'options': ['auto', 'long', 'short'],
                              'label': 'Direction'},
            },
            token_estimate=400, tier_minimum='free',
        )

    def fetch(self, user_id, asset, timeframe, config):
        if not asset:
            return ''
        try:
            dual = config.get('_dual', False)
            if dual:
                # Trade Design: score BOTH directions with full detail
                from carrac.scanner.scorer import score_asset, _build_macro_data
                try:
                    from carrac.precompute import response_cache
                except ImportError:
                    return ''
                cached = response_cache.get('htf')
                if not cached:
                    return ''
                asset_data = None
                for a in cached.get('assets', []):
                    if a.get('symbol') == asset:
                        asset_data = a
                        break
                if not asset_data:
                    return ''
                macro_data = _build_macro_data()
                long_result = score_asset(asset_data, macro_data, 'long')
                long_result['symbol'] = asset
                long_result['direction'] = 'long'
                short_result = score_asset(asset_data, macro_data, 'short')
                short_result['symbol'] = asset
                short_result['direction'] = 'short'
                return _format_dual_score_for_prompt(long_result, short_result)
            else:
                from carrac.scanner.scorer import get_quick_score
                direction = config.get('direction', 'auto')
                result = get_quick_score(asset, direction if direction != 'auto' else None)
                if not result:
                    return ''
                return _format_score_for_prompt(result)
        except Exception as e:
            logger.debug(f"Scorer block error: {e}")
            return ''


def _format_score_for_prompt(score_data: dict) -> str:
    """Format scorer output as structured text for LLM consumption."""
    lines = [
        f"CONDITIONS ALIGNED: {score_data['symbol']} {score_data['direction'].upper()} "
        f"= {score_data['total']}/12 conditions ({score_data['verdict']} tier)"
    ]

    for name in ('trend', 'momentum', 'rsi', 'structure', 'level', 'macro'):
        c = score_data.get('criteria', {}).get(name, {})
        lines.append(f"  {name.title()}: {c.get('score', '?')}/2 — {c.get('evidence', 'N/A')}")

    if score_data.get('vetoes'):
        lines.append(f"  Vetoes: {'; '.join(score_data['vetoes'])}")

    setup = score_data.get('setup')
    if setup:
        conf = setup.get('confidence', 0)
        conf_str = f"{conf:.0%}" if isinstance(conf, float) else str(conf)
        lines.append(f"  Setup: {setup.get('type', '?')} ({setup.get('readiness', '?')}) "
                     f"confidence={conf_str}")

    plan = score_data.get('trade_plan')
    if plan and plan.get('entry'):
        parts = [f"Entry=${plan['entry']:,.2f}"]
        if plan.get('sl'):
            sl_pct = plan.get('sl_pct', 0)
            parts.append(f"SL=${plan['sl']:,.2f} ({sl_pct:.1f}%)")
        if plan.get('tp1'):
            parts.append(f"TP1=${plan['tp1']:,.2f} (R:R {plan.get('rr1', 0):.1f})")
        if plan.get('tp2'):
            parts.append(f"TP2=${plan['tp2']:,.2f} (R:R {plan.get('rr2', 0):.1f})")
        lines.append(f"  Mechanical reference levels (scanner rule, not a recommendation): {' | '.join(parts)}")

    opp = score_data.get('opposite')
    if opp:
        lines.append(f"  Opposite ({opp['direction']}): {opp['total']}/12 conditions ({opp['verdict']} tier)")

    return '\n'.join(lines)


def _format_dual_score_for_prompt(long_data: dict, short_data: dict) -> str:
    """Format dual-direction scorer output for Trade Design prompt."""
    lines = [
        f"DUAL-DIRECTION CONDITIONS ALIGNED: {long_data['symbol']}",
        f"  LONG = {long_data['total']}/12 conditions ({long_data['verdict']} tier) | "
        f"SHORT = {short_data['total']}/12 conditions ({short_data['verdict']} tier)",
        "",
    ]

    for direction_label, data in [("LONG", long_data), ("SHORT", short_data)]:
        lines.append(f"{direction_label} BREAKDOWN:")
        for name in ('trend', 'momentum', 'rsi', 'structure', 'level', 'macro'):
            c = data.get('criteria', {}).get(name, {})
            lines.append(f"  {name.title()}: {c.get('score', '?')}/2 — {c.get('evidence', 'N/A')}")
        if data.get('vetoes'):
            lines.append(f"  Vetoes: {'; '.join(data['vetoes'])}")

        setup = data.get('setup')
        if setup:
            conf = setup.get('confidence', 0)
            conf_str = f"{conf:.0%}" if isinstance(conf, float) else str(conf)
            lines.append(f"  Setup: {setup.get('type', '?')} ({setup.get('readiness', '?')}) "
                         f"confidence={conf_str}")

        plan = data.get('trade_plan')
        if plan and plan.get('entry'):
            parts = [f"Entry=${plan['entry']:,.2f}"]
            if plan.get('sl'):
                parts.append(f"SL=${plan['sl']:,.2f} ({plan.get('sl_pct', 0):.1f}%)")
            if plan.get('tp1'):
                parts.append(f"TP1=${plan['tp1']:,.2f} (R:R {plan.get('rr1', 0):.1f})")
            if plan.get('tp2'):
                parts.append(f"TP2=${plan['tp2']:,.2f} (R:R {plan.get('rr2', 0):.1f})")
            lines.append(f"  Mechanical reference levels (scanner rule, not a recommendation): {' | '.join(parts)}")

        lines.append("")

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_asset(cached_data: dict, symbol: str):
    """Find an asset in cached response data."""
    for a in cached_data.get('assets', []):
        if a.get('symbol') == symbol:
            return a
    return None


# ---------------------------------------------------------------------------
# Block Registry
# ---------------------------------------------------------------------------

BLOCK_REGISTRY: Dict[str, ContextBlock] = {}


def _register_blocks():
    """Populate the global block registry."""
    blocks = [
        PriceBlock(),
        OHLCVBlock(),
        IndicatorsBlock(),
        ConsensioBlock(),
        StructureBlock(),
        CPRBlock(),
        KeyLevelsBlock(),
        DrawingsBlock(),
        ScenarioBlock(),
        PlayBlock(),
        ThesisBlock(),
        MacroBlock(),
        RegimeBlock(),
        QuantEngineBlock(),
        SentimentBlock(),
        BreadthBlock(),
        ProfileBlock(),
        MorningReportBlock(),
        FullReportBlock(),
        MultiTFBlock(),
        VolumeProfileBlock(),
        CustomFormulasBlock(),
        JournalHistoryBlock(),
        CorrelatedAssetsBlock(),
        AlertsBlock(),
        ScorerBlock(),
    ]
    for b in blocks:
        BLOCK_REGISTRY[b.id] = b


_register_blocks()


def get_available_blocks(user_tier: str = 'free') -> list:
    """Return block metadata for the UI (respects tier gating)."""
    TIER_ORDER = {'free': 0, 'member': 1, 'tester': 1, 'premium': 2, 'admin': 99}
    user_level = TIER_ORDER.get(user_tier, 0)

    result = []
    for block in BLOCK_REGISTRY.values():
        required_level = TIER_ORDER.get(block.tier_minimum, 0)
        result.append({
            'id': block.id,
            'name': block.name,
            'description': block.description,
            'category': block.category,
            'default_enabled': block.default_enabled,
            'config_schema': block.config_schema,
            'token_estimate': block.token_estimate,
            'tier_minimum': block.tier_minimum,
            'locked': user_level < required_level,
        })
    return result
