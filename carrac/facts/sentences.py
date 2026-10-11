"""Deterministic sentence library.

Renders the per-asset facts bundle (``carrac/facts/bundle.py``) into editorial
sentences that *cannot* hallucinate — every slot is a computed field. The report
uses these as its substance; LLM prose shrinks to a thin, grounded garnish.

Design contract (mirrors the bundle's own conventions):
- **Pure & gated.** A builder is ``(bundle, seed) -> Sentence | None``. A missing
  or not-notable field returns ``None`` — the sentence silently doesn't exist
  (never an error, never "N/A" filler).
- **Salience, not a checklist.** Each builder scores how *unusual* its fact is;
  the assembler sorts by salience and the caller takes the top-K, so the report
  leads with what's notable rather than a fixed roll-call.
- **Deterministic variants.** Each builder carries 2+ phrasings, picked by a
  stable seed derived from (symbol, as_of) — varied across assets, reproducible
  for the same asset/day.
- **Posture-linted.** Every rendered sentence is checked against banned advice
  phrasing; a builder physically cannot ship a "should buy".
- **Source-tagged.** Each Sentence carries a provenance tag for the later
  "inspect the source" feature.

This module is the prose layer of the same facts-first posture as the
deterministic blocks in ``morning_engine/report_blocks.py`` / ``ta_cards.py``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from carrac.facts.labels import asset_name, level_label, STRETCHED_PCT, DEPRESSED_PCT


@dataclass
class Sentence:
    text: str
    salience: float  # 0..1 — how notable the underlying fact is
    domain: str      # 'trend' | 'momentum' | 'level' | ...
    source: str      # provenance tag (indicator · TF)


# --------------------------------------------------------------------------- helpers

def _stable_seed(symbol: str, as_of) -> int:
    """A deterministic integer seed from (symbol, as_of) — stable per asset/day."""
    return int(hashlib.md5(f"{symbol}|{as_of}".encode(), usedforsecurity=False).hexdigest(), 16)


def _pick(variants: list[str], seed: int) -> str:
    return variants[seed % len(variants)]


def _ordinal(n: int) -> str:
    """Format an integer as an ordinal string (e.g. 84 → '84th')."""
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    suf = {1: "st", 2: "nd", 3: "rd"}
    return f"{n}{suf.get(n % 10, 'th')}"


# Curated advice phrasing the library must never emit (defense-in-depth; builders
# are written non-advisory by construction). Unioned with the app-wide list when
# importable, but only multi-word entries (bare "buy"/"sell" would false-positive).
_BANNED = (
    "should buy", "should sell", "i recommend", "we recommend", "you should",
    "buy now", "sell now", "time to buy", "time to sell", "i would buy", "i would sell",
)
try:  # pragma: no cover - optional enrichment
    from carrac.ai.copy import BANNED_ADVICE_PHRASES as _EXTRA  # type: ignore
    _BANNED = tuple({*_BANNED, *(p.lower() for p in _EXTRA if " " in p)})
except Exception:  # noqa: BLE001
    pass


def _passes_posture(text: str) -> bool:
    t = text.lower()
    return not any(b in t for b in _BANNED)


# --------------------------------------------------------------------------- builders

def build_ma_stack(bundle: dict, seed: int = 0) -> Sentence | None:
    ms = bundle.get("ma_stack") or {}
    state = ms.get("state")
    if not state or state == "insufficient":
        return None
    name = asset_name(bundle.get("symbol", ""))
    bars = ms.get("bars_held", 0) or 0
    cross = ms.get("cross", "none")
    src = "SMA20/50/200 · daily"

    # A fresh golden/death cross is the most notable structural event.
    if cross in ("golden_cross", "death_cross"):
        word = "golden cross" if cross == "golden_cross" else "death cross"
        dirn = "above" if cross == "golden_cross" else "below"
        variants = [
            f"{name}'s 50-day average crossed {dirn} its 200-day — a {word}.",
            f"A {word} just printed on {name}: the 50-day crossed {dirn} the 200-day.",
        ]
        return Sentence(_pick(variants, seed), 0.85, "trend", src)

    if state == "bullish":
        variants = [
            f"{name} trades in a full bullish stack — above its 20-, 50- and 200-day "
            f"averages, intact for {bars} sessions.",
            f"{name} holds a full bullish stack (above the 20-, 50- and 200-day) for a "
            f"{bars}-session run.",
        ]
        return Sentence(_pick(variants, seed), min(0.5 + bars * 0.01, 0.7), "trend", src)

    if state == "bearish":
        variants = [
            f"{name} sits below all three key averages — a bearish stack {bars} sessions old.",
            f"{name} trades in a full bearish stack (under the 20-, 50- and 200-day) for "
            f"{bars} sessions.",
        ]
        return Sentence(_pick(variants, seed), min(0.5 + bars * 0.01, 0.7), "trend", src)

    if state == "bullish_partial":
        variants = [
            f"{name} is above its 200-day average but not in a full bullish stack yet.",
            f"{name} holds above the 200-day, though the shorter averages aren't fully aligned.",
        ]
        return Sentence(_pick(variants, seed), 0.30, "trend", src)

    if state == "bearish_partial":
        variants = [
            f"{name} is below its 200-day average — structure still bearish-leaning.",
            f"{name} trades under the 200-day, with the shorter averages not yet aligned.",
        ]
        return Sentence(_pick(variants, seed), 0.30, "trend", src)

    return None


def build_rsi(bundle: dict, seed: int = 0) -> Sentence | None:
    rsi = bundle.get("rsi") or {}
    val = rsi.get("value")
    if val is None:
        return None
    zone = rsi.get("zone")
    div = (rsi.get("divergence") or {}).get("divergence_type")
    extreme = zone in ("overbought", "oversold")
    # A neutral RSI with no divergence is not worth a sentence.
    if not extreme and not div:
        return None

    name = asset_name(bundle.get("symbol", ""))
    src = "RSI(14) · daily"
    vtxt = f"{val:.0f}"
    sal = min(abs(val - 50) / 50.0, 1.0) * 0.6
    if div:
        sal = min(sal + 0.30, 1.0)

    if extreme and div:
        dword = "bearish" if "bearish" in div else "bullish"
        variants = [
            f"{name}'s daily RSI at {vtxt} is {zone}, with a {dword} divergence building "
            f"against price.",
            f"{name} prints RSI {vtxt} ({zone}) and a {dword} divergence — momentum "
            f"diverging from price.",
        ]
        return Sentence(_pick(variants, seed), sal, "momentum", src)

    if extreme:
        variants = [
            f"{name}'s daily RSI at {vtxt} is {zone}.",
            f"{name} reads RSI {vtxt} on the daily — {zone}.",
        ]
        return Sentence(_pick(variants, seed), sal, "momentum", src)

    # divergence only (neutral zone)
    dword = div.replace("_", " ")
    variants = [
        f"{name} shows a {dword} RSI divergence on the daily — momentum diverging from price.",
        f"A {dword} RSI divergence is building on {name} (daily).",
    ]
    return Sentence(_pick(variants, seed), sal, "momentum", src)


def build_streak(bundle: dict, seed: int = 0) -> Sentence | None:
    """Render the most-persistent key-level streak (most bars held)."""
    streaks = bundle.get("streaks") or []
    if not streaks:
        return None
    entry = max(streaks, key=lambda x: x.get("bars", 0))
    bars = entry.get("bars", 0)
    if not bars:
        return None
    name = asset_name(bundle.get("symbol", ""))
    price = entry.get("price", 0)
    lbl = level_label(entry.get("level", ""))
    type_ = entry.get("type", "defending")
    price_fmt = f"${price:,.0f}"
    src = "key_levels · daily"

    _VERBS = {
        "defending": ("defended", "held above"),
        "testing": ("tested", "pushed against"),
        "rejecting": ("rejected at", "turned back from"),
        "capped": ("traded capped under", "been capped under"),
    }
    v1, v2 = _VERBS.get(type_, ("held", "held at"))

    variants = [
        f"{name} has {v1} its {lbl} ({price_fmt}) for {bars} straight sessions.",
        f"{name} has {v2} its {lbl} ({price_fmt}) — {bars} sessions running.",
    ]
    salience = min(bars / 10.0, 0.8)
    return Sentence(_pick(variants, seed), salience, "level", src)


def build_decisive_close(bundle: dict, seed: int = 0) -> Sentence | None:
    """Render the freshest decisive level close (smallest bars_ago)."""
    closes = bundle.get("decisive_close") or []
    if not closes:
        return None
    entry = min(closes, key=lambda x: x.get("bars_ago", 999))
    lbl = level_label(entry.get("level", ""))
    direction = entry.get("direction", "above")
    confirmed = entry.get("confirmed", False)
    bars_ago = entry.get("bars_ago", 0)
    name = asset_name(bundle.get("symbol", ""))
    src = "key_levels · daily"

    conf_phrase = " — a confirmed break" if confirmed else ""
    tense = "closed" if bars_ago > 0 else "closed"  # always past

    variants = [
        f"{name} {tense} decisively {direction} its {lbl}{conf_phrase}.",
        f"{name} posted a decisive close {direction} the {lbl}{conf_phrase}.",
    ]
    # Base ~0.7; bonus for freshness (bars_ago=0 → 0.9, fades with age)
    salience = min(0.9 / (1.0 + bars_ago * 0.2), 0.9)
    return Sentence(_pick(variants, seed), salience, "level", src)


def build_funding(bundle: dict, seed: int = 0) -> Sentence | None:
    """Render the funding-rate streak for crypto assets."""
    funding = bundle.get("funding") or {}
    streak = funding.get("streak_days")
    if streak is None:
        return None
    sign = funding.get("sign", "positive")
    pct = funding.get("percentile", 50)
    name = asset_name(bundle.get("symbol", ""))
    src = "funding · perp"

    variants = [
        f"{name} funding has stayed {sign} for {streak} periods — "
        f"{_ordinal(pct)} percentile.",
        f"{name}'s funding rate has been {sign} for {streak} straight periods "
        f"({_ordinal(pct)} percentile of its history).",
    ]
    # Salience: extremity of percentile + streak length contribution
    pct_component = abs(pct - 50) / 50.0 * 0.6
    streak_component = min(streak, 20) / 20.0 * 0.3
    salience = min(pct_component + streak_component, 0.9)
    return Sentence(_pick(variants, seed), salience, "derivatives", src)


def build_stretch(bundle: dict, seed: int = 0) -> Sentence | None:
    """Render the Mayer Multiple (BTC 200-week stretch) when present."""
    stretch = bundle.get("stretch")
    if not stretch:
        return None
    mayer = stretch.get("mayer")
    mayer_pct = stretch.get("mayer_pct")
    if mayer is None or mayer_pct is None:
        return None
    name = asset_name(bundle.get("symbol", ""))
    src = "mayer · weekly"

    if mayer_pct >= STRETCHED_PCT:
        band = "stretched territory"
    elif mayer_pct <= DEPRESSED_PCT:
        band = "historically depressed territory"
    else:
        band = "its historical mid-range"

    mayer_fmt = f"{mayer:.2f}"
    variants = [
        f"{name} trades at {mayer_fmt}× its 200-week average — "
        f"the {_ordinal(mayer_pct)} percentile of its history.",
        f"At {mayer_fmt}× the 200-week average, {name} sits in {band} "
        f"({_ordinal(mayer_pct)} percentile).",
    ]
    salience = min(abs(mayer_pct - 50) / 50.0, 1.0) * 0.7
    return Sentence(_pick(variants, seed), salience, "trend", src)


def build_candle(bundle: dict, seed: int = 0) -> Sentence | None:
    """Render the daily candle pattern when one is recognised (label only, never predictive)."""
    candle = bundle.get("candle") or {}
    label = candle.get("pattern_label")
    if not label:
        return None
    name = asset_name(bundle.get("symbol", ""))
    src = "candle · daily"

    # Marubozu / reversal wicks are more decisive than a doji.
    if label in ("marubozu", "hammer", "inverted_hammer"):
        salience = 0.50
    else:  # doji
        salience = 0.35

    variants = [
        f"{name} printed a {label} on the daily.",
        f"The daily candle on {name} formed a {label}.",
    ]
    return Sentence(_pick(variants, seed), salience, "structure", src)


def build_range(bundle: dict, seed: int = 0) -> Sentence | None:
    """Render the price-within-range percentile when present."""
    rp = bundle.get("range_pctile") or {}
    pct = rp.get("pct")
    if pct is None:
        return None
    window = rp.get("window_days", 90)
    name = asset_name(bundle.get("symbol", ""))
    src = "range · daily"

    variants = [
        f"{name} sits at the {_ordinal(pct)} percentile of its {window}-day range.",
        f"Within its {window}-day range, {name} is at the {_ordinal(pct)} percentile.",
    ]
    salience = min(abs(pct - 50) / 50.0, 1.0) * 0.6
    return Sentence(_pick(variants, seed), salience, "structure", src)


# The registry — append builders here as they're added (mirrors indicator-registry).
BUILDERS = [
    build_ma_stack,
    build_rsi,
    build_streak,
    build_decisive_close,
    build_funding,
    build_stretch,
    build_candle,
    build_range,
]


# --------------------------------------------------------------------------- assembler

def asset_sentences(bundle: dict, *, k: int | None = None, seed: int | None = None) -> list[Sentence]:
    """Render the salience-ranked sentences for one asset's facts bundle.

    Runs every builder, drops ``None`` (graceful absence) and any posture-lint
    failure, sorts by salience descending, and returns the top-``k`` (all if
    ``k`` is None). Deterministic for a given (symbol, as_of).
    """
    if seed is None:
        seed = _stable_seed(bundle.get("symbol", ""), bundle.get("as_of", ""))

    out: list[Sentence] = []
    for builder in BUILDERS:
        try:
            s = builder(bundle, seed)
        except Exception:  # noqa: BLE001 - a broken builder must never sink the report
            s = None
        if s is None or not _passes_posture(s.text):
            continue
        out.append(s)

    out.sort(key=lambda s: s.salience, reverse=True)
    return out[:k] if k else out


def asset_lede(bundle: dict, *, seed: int | None = None) -> str | None:
    """Return the single highest-salience sentence text for an asset, or None if nothing notable.

    This is the one-line read the card/board uses as its headline fact.
    """
    sentences = asset_sentences(bundle, k=1, seed=seed)
    return sentences[0].text if sentences else None
