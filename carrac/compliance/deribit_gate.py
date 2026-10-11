"""The single place that decides who may see Deribit data.

Deribit's ToS bars publishing or processing its market and derived data
without written approval. The Derivatives Desk was switched off
(``DERIVATIVES_DESK_ENABLED=0``) and ``carrac/facts/bands.py`` stopped
publishing these columns in report facts — but the collector kept running and
several *other* doors were left open, each of which redistributes the same
data to anyone with an account:

* the MCP server (``quant_get_regime`` and the report tools returned the full
  options complex, and the tool DESCRIPTIONS advertised the field names in
  the public tool catalogue)
* the formula/DSL engine — ``dvol`` and nine per-asset ``DERIV_VARS`` were
  usable in any user's formula, alert or backtest
* ``quant/bridge.py``'s series catalogue, which backs both the formula engine
  and ``GET /api/quant/chart-series``

Keeping the data for the operator's own analysis is a materially different
posture from redistributing it, so the rule this module enforces is:
**collect freely, expose to admin only.**

Everything that could surface a Deribit-derived value imports from here, so
the answer lives in exactly one place and a new surface has to opt in
deliberately. ``tests/test_deribit_gate.py`` enforces that: it walks every
module naming a barred column and fails unless the module is registered there
with the reason it is safe.

The registration test must actually exist and run: a docstring that merely
claims it is no guard. Without it, surfaces go ungated unnoticed — the report
pipeline, the notification body, play-condition evidence, and the AI shared
context, which would hand a PREMIUM customer the options complex if a comment
read "Premium / admin only" and the code honoured only the first half.

Permitted derivatives data (Binance/Hyperliquid) is deliberately
NOT gated: funding rate, open interest and long/short ratio stay available
to everyone. This is a licensing boundary, not a paywall.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ── The canonical lists ────────────────────────────────────────────────────

#: Columns in ``quant.derivatives`` written by ``quant/collectors/deribit_options.py``.
#: Mirrors the exclusion documented in ``carrac/facts/bands.py``.
DERIBIT_COLUMNS = frozenset({
    "dvol",
    "iv_7d",
    "iv_30d",
    "iv_90d",
    "term_structure_slope",
    "skew_25d",
    "iv_rv_premium",
    "basis_pct",
    "gex_net",
    "gex_flip_price",
    "options_max_pain",
    "options_put_call_ratio",
    # quant/collectors/deribit_options.py writes this column; if it is missing
    # here OPTIONS_IV_<asset> is never barred and reaches the public /brief
    # page. Every column that collector writes must be listed.
    "options_iv_atm",
})

#: Columns from Binance/Hyperliquid — licence-permitted. Listed so the
#: distinction is explicit rather than inferred from absence.
PERMITTED_DERIVATIVES_COLUMNS = frozenset({
    "funding_rate_avg",
    "oi_usd",
    "long_short_ratio",
    # "funding_oi_ratio" was listed here for months but is NOT a column —
    # nothing ever wrote it. tests/test_permitted_derivatives.py
    # now pins this set to the schema DDL so a phantom cannot be re-listed.
})

#: ``quant/bridge.py`` SERIES_CATALOG keys backed by a Deribit column.
DERIBIT_SERIES_KEYS = frozenset({
    "DVOL",
    "SKEW_25D",
    "GEX",
    "IV_RV_PREMIUM",
    "BASIS_APR",
})

#: Formula/DSL variable names (``QUANT_VARS`` + ``DERIV_VARS``) that resolve
#: to a Deribit column. ``funding`` is absent on purpose — Binance-sourced.
DERIBIT_FORMULA_VARS = frozenset({
    "dvol",
    "basis_apr",
    "skew_25d",
    "iv_7d",
    "iv_30d",
    "iv_90d",
    "term_slope",
    "gex_net",
    "gex_flip_price",
    "iv_rv_premium",
})


# ── The gate ───────────────────────────────────────────────────────────────

def deribit_visible() -> bool:
    """True only for the operator. Fails CLOSED.

    Resolves the caller through whichever runtime is active — MCP contextvar
    first, then the Flask request tier. Any exception, unknown tier, or
    absent request context resolves to False: an unresolvable caller is not
    the operator.

    Note the MCP default. ``get_mcp_caller_tier()`` returns ``'free'`` when no
    caller has been set, so a tool invoked outside a request (a test, a
    background job) does NOT get Deribit data by accident.
    """
    try:
        from carrac.mcp.runtime import get_mcp_caller_tier
        if get_mcp_caller_tier() == "admin":
            return True
    except Exception:  # noqa: BLE001 - no MCP context is a normal case
        pass

    try:
        from carrac.auth.decorators import get_request_tier
        return get_request_tier() == "admin"
    except Exception:  # noqa: BLE001 - no request context is a normal case
        return False


def user_is_admin(user_id) -> bool:
    """True when this stored USER ID is an operator account. Fails CLOSED.

    ``deribit_visible()`` asks "is the CALLER the operator", resolved from an
    MCP contextvar or a Flask request. Neither exists on the paths this
    function serves: alerts are delivered from a background loop and plays are
    evaluated by a monitor thread, both of which know only a stored user_id.
    Same licensing question, different identity source.

Every failure mode — no id, unknown id, unreadable user store —
    resolves to False, because an unresolvable user is not the operator.
    """
    if user_id is None or user_id == "":
        return False
    try:
        # The module exposes a SINGLETON, and the class is `UserDatabase`.
        # Import the real name: a wrong one (e.g. `UserDB`) raises ImportError,
        # falls into the except below, and the gate fails closed forever — no
        # leak, but the operator is never recognised. Unit tests must not
        # inject a fake `user_db` module built around the same wrong name.
        from user_db import user_db as _users
        user = _users.get_user_by_id(int(user_id))
        return bool(user and user.get("is_admin"))
    except Exception:  # noqa: BLE001 - a broken lookup is not an operator
        logger.warning("user_is_admin(%r) could not resolve — treating as "
                       "non-admin", user_id, exc_info=True)
        return False


def condition_metric_visible(metric: str, user_id=None) -> bool:
    """May a play/alert condition on ``metric`` be created or evaluated?

    Permitted metrics (funding_rate_avg) are always visible — this is a
    licensing boundary, not a paywall. Deribit-backed ones are operator-only,
    matching this module's stated posture: collect freely, expose to admin.

    The authority is ``DERIBIT_COLUMNS`` itself rather than a second
    hand-written list, so a newly barred column is covered here the moment it
    is added there. That is the drift that let ``options_iv_atm`` reach /brief.

    ``user_id`` is the user who owns the stored condition, used on the background
    evaluation paths. Omit it and the question falls back to the live caller
    (``deribit_visible()``), which is what the creation/validation path wants.
    """
    if metric not in DERIBIT_COLUMNS:
        return True
    if user_id is not None:
        return user_is_admin(user_id)
    return deribit_visible()


def strip_deribit_columns(row):
    """Drop every Deribit column from a mapping unless the caller is admin.

    Returns a NEW dict; the input is never mutated. ``None`` passes through so
    callers can hand a possibly-absent DB row straight in.

    Keys are removed rather than nulled: a ``"dvol": null`` in a payload still
    tells the reader the field exists and invites a support question about why
    it is empty.
    """
    if row is None:
        return None
    if deribit_visible():
        return dict(row)
    return {k: v for k, v in row.items() if k not in DERIBIT_COLUMNS}


def allowed_series_keys(keys):
    """Filter bridge series keys to those the caller may see."""
    if deribit_visible():
        return list(keys)
    return [k for k in keys if k not in DERIBIT_SERIES_KEYS]


def is_gated_series(key: str) -> bool:
    """True when this bridge series key is Deribit-backed and hidden."""
    return key in DERIBIT_SERIES_KEYS and not deribit_visible()


def allowed_formula_vars(mapping: dict) -> dict:
    """Filter a formula variable map (name → series/column) for this caller."""
    if deribit_visible():
        return dict(mapping)
    return {k: v for k, v in mapping.items() if k not in DERIBIT_FORMULA_VARS}

def deribit_leadlag_metrics() -> frozenset:
    """Lead-lag metric names whose underlying column is Deribit-sourced.

    Derived from ``quant/engine/config.py``'s registry rather than hardcoded,
    so adding a metric on a Deribit column cannot quietly escape the gate.
    The lead-lag engine publishes CORRELATIONS computed on these — still
    derived data, and the ``derivatives_leverage`` transmission chain is built
    almost entirely from them.
    """
    # Deliberately NOT wrapped in a bare except. An unreadable registry here
    # would return an empty set, i.e. "nothing is gated" — failing OPEN on a
    # licensing control. If quant.engine.config cannot be imported that is a
    # broken install and should be loud. (It returned an empty set silently on
    # the first cut of this function, because the registry is called
    # CANONICAL_METRICS, not METRICS.)
    from quant.engine.config import CANONICAL_METRICS

    return frozenset(
        name for name, spec in CANONICAL_METRICS.items()
        if isinstance(spec, dict) and spec.get("column") in DERIBIT_COLUMNS
    )


def leadlag_metric_allowed(name: str) -> bool:
    """False when this lead-lag metric is Deribit-backed and caller is not admin."""
    if deribit_visible():
        return True
    return name not in deribit_leadlag_metrics()


# ── Anomaly gating ──────────────────────
#
# A rule that only gated an anomaly row on `leadlag_metric_allowed(metric)`
# plus `'derivatives_options' not in domain`. Both halves under-matched, and
# the public /brief page carried the result:
#
#   * `deribit_leadlag_metrics()` is derived from CANONICAL_METRICS, which
#     registers only BTC variants — it returns exactly six names, all
#     _BTC-suffixed. Every ETH equivalent (SKEW_25D_ETH, GEX_ETH,
#     OPTIONS_IV_ETH) was therefore "allowed". The lead-lag registry is the
#     wrong authority for a per-asset anomalies table: it answers "is this a
#     lead-lag metric we gate", not "is this value Deribit-derived".
#   * Only ONE of the two Deribit-backed domains was named, so the whole of
#     `derivatives_advanced` (SKEW_25D, GEX, BASIS, IV_RV) walked through.
#
# The authority is quant/normalize.py's DOMAIN_QUERIES, which states in SQL
# which column each metric prefix is selected from. Two of its derivatives
# domains read exclusively from Deribit columns and two read exclusively from
# permitted Binance/Bybit ones — so the domain IS the answer, and the prefix
# set is the belt-and-braces for a row whose domain is missing or renamed.
#
# tests/test_deribit_report_gate.py parses DOMAIN_QUERIES and fails if either
# set drifts from it — a new Deribit-backed metric prefix, or a domain moving
# between the barred and permitted lists. Without that the guard could not see
# what it is guarding.

#: Anomaly domains reading exclusively from Deribit columns.
DERIBIT_ANOMALY_DOMAINS = frozenset({
    'derivatives_advanced',   # skew_25d, basis_pct, gex_net, iv_rv_premium
    'derivatives_options',    # options_put_call_ratio, options_iv_atm, dvol
})

#: Anomaly domains reading exclusively from licence-permitted columns.
#: Listed so the distinction is asserted rather than inferred from absence —
#: over-gating these would silently drop Binance data we are allowed to show.
PERMITTED_ANOMALY_DOMAINS = frozenset({
    'derivatives_funding',    # funding_rate_avg
    'derivatives_ls',         # long_short_ratio
})

#: Metric-name prefixes (the `'PREFIX_' || asset` forms in DOMAIN_QUERIES)
#: whose source column is Deribit-barred. Asset-agnostic on purpose: the bug
#: this replaces was a BTC-only allow-list.
DERIBIT_METRIC_PREFIXES = frozenset({
    'SKEW_25D',    # skew_25d
    'BASIS',       # basis_pct
    'GEX',         # gex_net
    'IV_RV',       # iv_rv_premium
    'DVOL',        # dvol
    'OPTIONS_IV',  # options_iv_atm
    'OPTIONS_PC',  # options_put_call_ratio
})


def is_deribit_anomaly(entry) -> bool:
    """True when this anomaly/cluster row is Deribit-derived.

    Caller-agnostic: answers "is this row barred data", not "may this caller
    see it". Callers apply ``deribit_visible()`` themselves, so the same rule
    serves the request path and the report-generation path — the latter
    authenticates as admin and must still not bake barred data into a
    published artifact.

    Over-matches by design. Dropping one anomaly row costs a line of a
    "notable moves" list; leaking one costs the licence.
    """
    if not isinstance(entry, dict):
        return False
    domain = (entry.get('domain') or '')
    if domain in DERIBIT_ANOMALY_DOMAINS:
        return True
    metric = (entry.get('metric') or '')
    if metric in DERIBIT_COLUMNS or metric.lower() in DERIBIT_COLUMNS:
        return True
    if metric in deribit_leadlag_metrics():
        return True
    # Strip the trailing _<ASSET> and test the prefix. FUNDING_BNB -> FUNDING
    # and LS_RATIO_AVAX -> LS_RATIO are correctly absent from the set.
    stem = metric.rsplit('_', 1)[0] if '_' in metric else metric
    return stem.upper() in DERIBIT_METRIC_PREFIXES


def strip_gated_anomalies(entries):
    """Drop every Deribit-derived anomaly row. Unconditional — caller decides."""
    if not entries:
        return entries
    return [e for e in entries if not is_deribit_anomaly(e)]


def strip_gated_anomaly_cluster(cluster):
    """Filter an anomaly-cluster payload and RECOMPUTE its headline counts.

    `extreme_count` and `domains_affected` are derived from the rows, so
    leaving them at their pre-filter values would advertise exactly the rows
    being withheld — the same reasoning that already ties `anomaly_count` to
    the filtered anomalies list. Returns a NEW dict; input is never mutated.
    """
    if not isinstance(cluster, dict):
        return cluster
    metrics = cluster.get('metrics')
    if not metrics:
        return cluster
    kept = [m for m in metrics if not is_deribit_anomaly(m)]
    out = dict(cluster)
    out['metrics'] = kept
    if 'extreme_count' in out:
        out['extreme_count'] = len(kept)
    if 'domains_affected' in out:
        out['domains_affected'] = len({(m.get('domain') or '') for m in kept})
    return out


# ── Prose scrubbing ───────────────────────────────────────────
#
# Everything above gates STRUCTURED data by caller. Report prose is LLM text,
# so there is no column to drop — only sentences.
#
# Two rules:
#
#   * The scrub is UNCONDITIONAL. `deribit_visible()` answers "may this CALLER
#     see it"; a published artifact needs "may this ARTIFACT contain it". The
#     report engine authenticates with X-Internal-Key, which resolves to admin,
#     so a caller-gated scrub would be a no-op on the report pipeline, and
#     `/brief/<date>` is public whoever requests it.
#   * Every gated column needs a pattern for its NARRATED form, and
#     tests/test_deribit_prose_scrub.py fails if one is missing.
#
# Over-matching is the acceptable direction: dropping a benign sentence costs a
# line of commentary, leaking a licensed figure costs the licence. Same posture
# as `_POSITION_LINE_RE` in carrac/reports/sanitize.py.

#: How each gated column is SPOKEN in report prose. Keyed by column so the
#: coverage test can compare against DERIBIT_COLUMNS directly.
DERIBIT_PROSE_PATTERNS = {
    "dvol": r"\bDVOL\b",
    "iv_7d": r"\b7[-\s]?d(?:ay)?\s+IV\b",
    # Carries the generic implied-vol vocabulary: 30d is the headline tenor and
    # unqualified "IV" in a market report is always this.
    "iv_30d": r"\b30[-\s]?d(?:ay)?\s+IV\b|\bimplied vol(?:atility)?\b|\bIV\b",
    "iv_90d": r"\b90[-\s]?d(?:ay)?\s+IV\b",
    # "term structure" needs a qualifier check: chart prose says "shift the
    # short-term structure" (market structure) constantly — measured
    # against the full 350-report archive, 17 of 115 matches were that idiom
    # and the scrub was deleting real SuperTrend/PSAR sentences from rendered
    # afternoon reports. The one genuine options reading among the 17
    # ("backwardated near-term structure (7d=53% vs 90d=62%)") stays covered:
    # its sentence also carries DVOL and max pain, which match on their own.
    # Each lookbehind is fixed width on its own, as Python's re requires.
    "term_structure_slope": (
        r"(?<!short-)(?<!short )(?<!long-)(?<!long )(?<!near-)(?<!near )"
        r"(?<!medium-)(?<!medium )\bterm structure\b"
    ),
    # "skew" only — never "skewed"/"skewness", which are ordinary prose.
    #
    # Measured against every occurrence in the stored archive
    # (325). 322 are options skew; THREE
    # are not, and the bare \bskew\b pattern deleted the whole sentence around
    # each of them — report 414 lost "…the median 30-day move was +2.4% at a
    # 68% hit rate — historically a slight positive skew even in risk-off
    # setups…", which is an analog-study finding with no Deribit content at all.
    #
    # Two narrower rules were tried and MEASURED against the same archive
    # before settling here, because both looked obviously right and were not:
    #
    #   "require a derivatives keyword nearby"  -> would stop scrubbing 24 real
    #       options readings ("By Friday, skew had normalized back to +8.4%")
    #   "exclude <direction> skew"              -> would stop scrubbing 20 more
    #       ("P/C ratio 0.729 — mild bullish skew", "the positive skew (+19.1%)")
    #
    # Both fail in the expensive direction for a compliance gate. What actually
    # separates the senses is the HEDGED DEGREE ADVERB — "a slight positive
    # skew", "modestly bearish skew" — plus the breadth comparison
    # "lows-over-highs skew". A Deribit reading is never hedged that way; it
    # carries a number or a plain direction word. Each lookbehind is fixed
    # width on its own, which is what Python's re requires.
    #
    # Verified: this releases those three and nothing else.
    "skew_25d": (
        r"\bskew\b"
        r"(?<!slight positive skew)(?<!slight negative skew)"
        r"(?<!slightly positive skew)(?<!slightly negative skew)"
        r"(?<!modestly bearish skew)(?<!modestly bullish skew)"
        r"(?<!over-highs skew)(?<!over-lows skew)"
    ),
    "iv_rv_premium": r"\bIV\s*[-/]\s*RV\b",
    # Phrase-anchored on purpose: a bare \bbasis\b would eat "basis points" and
    # "on a relative basis", neither of which is Deribit data.
    "basis_pct": (
        r"\b(?:futures|annualized|annualised)\s+basis\b"
        r"|\bbasis\s+(?:at|of|is|sits|below|above|remains|near)\b"
        r"|\bbasis[_\s-]?(?:pct|apr)\b"
    ),
    "gex_net": r"\bGEX\b|\bgamma exposure\b|\b(?:short|long|net|negative|positive)[-\s]gamma\b",
    # Bare "flip (point|price|level)" needs qualifier lookbehinds: chart prose
    # says "PSAR short flip level", "SuperTrend flip point", "signal flip
    # level", "coin-flip level". Measured against the full archive:
    # 279 genuine gamma-flip matches, 12 chart-indicator false positives, and
    # these lookbehinds free 9 of the 12 while keeping all 279 (the "require a
    # gamma keyword nearby" alternative was measured too — it freed all 12 but
    # dropped 9 genuine readings, the expensive direction for a compliance
    # gate). The 3 residual FPs are bare "below flip level" table rows whose
    # SuperTrend qualifier sits out of lookbehind reach — accepted.
    "gex_flip_price": (
        r"\bgex[-\s]?flip\b|\bgamma flip\b"
        r"|(?<!PSAR )(?<!short )(?<!SuperTrend )(?<!signal )(?<!coin-)"
        r"\bflip (?:point|price|level)\b"
    ),
    "options_max_pain": r"\bmax[-\s]?pain\b",
    "options_put_call_ratio": r"\bput[-/\s]call ratio\b",
    "options_iv_atm": r"\bATM\s+IV\b|\bat[-\s]the[-\s]money\s+(?:implied\s+)?vol(?:atility)?\b",
}

_BARRED_RE = re.compile(
    "|".join(f"(?:{p})" for p in DERIBIT_PROSE_PATTERNS.values()), re.IGNORECASE
)

# Tokenizers, not lookbehind splits. A sentence ender is often followed by
# closing markdown — "…not panicking.** DVOL at 40.5…" ends with `.**`, two
# characters — and `re` lookbehind must be fixed width, so it cannot express
# "any number of closing marks". Getting this wrong welded a clean claim to the
# DVOL sentence after it and emitted the bare fragment "**3.".
_SENTENCE_RE = re.compile(r'.*?[.!?][*_")\]]*(?:\s+|$)', re.DOTALL)
# Cells additionally break on semicolons: "Death cross; +12.3% 5-bar; below GEX
# flip 1,788" is three claims on one row.
_CELL_RE = re.compile(r'.*?[.!?;][*_")\]]*(?:\s+|$)', re.DOTALL)


def _tokenize(text: str, pattern) -> list:
    """Split into sentence-ish units, keeping any unterminated tail."""
    parts = pattern.findall(text)
    consumed = sum(len(p) for p in parts)
    if consumed < len(text):
        parts.append(text[consumed:])
    return parts
_LIST_MARKER = re.compile(r"^(\s*(?:[-*+]|\d+\.)\s+)(.*)$")
_HEADING = re.compile(r"^\s*#{1,6}\s")
_DECORATION = re.compile(r"[^0-9A-Za-z]")


def _has_barred(text: str) -> bool:
    return bool(text) and _BARRED_RE.search(text) is not None


def _meaningful(text: str) -> bool:
    """True when anything survives once markdown decoration is discarded.

    Guards against leaving `**`, `-` or `|` scaffolding behind after the words
    they wrapped have gone.
    """
    return bool(_DECORATION.sub("", text or ""))


#: Above this share of a paragraph's sentences being barred, the survivors are
#: dropped too. A paragraph that loses most of itself leaves the remainder
#: explaining something the reader can no longer see — "That is why a modest
#: BTC move can generate outsized follow-through", with the gamma explanation
#: above it gone, reads as a non-sequitur rather than an omission.
_ORPHAN_THRESHOLD = 0.5


def _keep_clean_parts(fragment: str, pattern, drop_orphans: bool = False) -> str:
    parts = [p for p in _tokenize(fragment, pattern) if p.strip()]
    if not parts:
        return ""
    kept = [p for p in parts if not _has_barred(p)]
    if drop_orphans and (len(parts) - len(kept)) / len(parts) > _ORPHAN_THRESHOLD:
        return ""
    # .strip() per unit: the tokenizer keeps each one's trailing whitespace, so
    # joining raw doubles the space wherever a sentence was removed.
    return " ".join(p.strip() for p in kept)


def _scrub_table_row(line: str) -> str | None:
    """Scrub a markdown table row cell-by-cell, preserving its shape.

    Dropping the whole row would delete the asset from the snapshot table —
    price, trend and RSI are not licensed and must survive. But when the row's
    SUBJECT (its first content cell) is itself barred, the row is about Deribit
    data and goes entirely.
    """
    cells = line.split("|")
    content = [c for c in cells if c.strip()]
    if content and _has_barred(content[0]):
        return None
    out = []
    for cell in cells:
        if not _has_barred(cell):
            out.append(cell)
            continue
        kept = _keep_clean_parts(cell.strip(), _CELL_RE)
        out.append(f" {kept} " if kept else " ")
    return "|".join(out)


def _scrub_line(line: str) -> str | None:
    """Return the line with barred sentences removed, or None to drop it."""
    if not _has_barred(line):
        return line
    if line.strip().startswith("|"):
        return _scrub_table_row(line)
    if _HEADING.match(line):
        return None
    marker = _LIST_MARKER.match(line)
    prefix, body = (marker.group(1), marker.group(2)) if marker else ("", line)
    kept = _keep_clean_parts(body, _SENTENCE_RE, drop_orphans=True)
    return prefix + kept if _meaningful(kept) else None


def _drop_empty_sections(text: str) -> str:
    """Remove any ``## Heading`` left with no substantive body.

    Five stored reports devote their whole "One Thing to Learn Today" section to
    explaining GEX. Scrubbing the sentences leaves a heading over a horizontal
    rule, which reads as a rendering bug rather than an omission.
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if not lines[i].lstrip().startswith("#"):
            out.append(lines[i])
            i += 1
            continue
        body, j = [], i + 1
        while j < len(lines) and not lines[j].lstrip().startswith("#"):
            body.append(lines[j])
            j += 1
        if any(_meaningful(b) and b.strip() != "---" for b in body):
            out.append(lines[i])
            out.extend(body)
        i = j
    return "\n".join(out)


def _capture(message: str) -> None:
    """Send one message to Sentry. Seam so the alarm is testable."""
    import sentry_sdk
    sentry_sdk.capture_message(message, level="error")


def alert_if_barred_prose(content, context: str = "report") -> list:
    """Alarm when a report being SAVED still narrates Deribit data.

    Returns the barred terms found (empty list when clean).

    The render-time scrub makes a leak invisible, not absent. If the generation
    gate regresses — a prompt edit, a new data source, a door nobody thought of
    — the scrub would keep quietly cleaning up after it forever. A
    silent failure here would go unnoticed indefinitely, so this is loud.

    Non-fatal by design, and never raises: it sits in the report write path and
    must not be able to fail a save. Same asymmetry as the Haiku fact verifier
    — notify, never withhold.

    Deliberately alarms only on WRITE, not on read. Serving a pre-gate report
    from the archive trips the scrub every single day; alarming there would be
    permanently red, and a guard you learn to ignore is worse than no guard.
    """
    try:
        if not content:
            return []
        found = sorted({
            column for column, pattern in DERIBIT_PROSE_PATTERNS.items()
            if re.search(pattern, content, re.IGNORECASE)
        })
        if not found:
            return []
        msg = (
            f"Deribit-derived prose in a newly saved {context} report: "
            f"{', '.join(found)}. The generation gate should have "
            f"kept these out of the briefing — the render-time scrub will hide "
            f"this from readers, so it will not show up any other way."
        )
        logger.error("BARRED-PROSE ALARM: %s", msg)
        try:
            _capture(msg)
        except Exception:  # noqa: BLE001
            # logger.error above already fired — this is not a silent loss.
            logger.warning("barred-prose alarm: Sentry capture failed", exc_info=True)
        return found
    except Exception:  # noqa: BLE001
        logger.warning("barred-prose alarm failed (non-fatal)", exc_info=True)
        return []


def scrub_deribit_prose(content):
    """Drop sentences carrying Deribit-derived data from report prose.

    Sentence-level rather than line-level: a line is often a whole paragraph,
    and "BTC at $64,407 has RSI 52, a death cross… DVOL sits at cycle lows"
    must keep everything before the last sentence. Measured on the live archive:
    152 of 7319 lines touched, worst report 23%.

    Unconditional by design — see the module note above. Idempotent. Text with
    nothing barred is returned byte-identical, so a clean report is never
    reflowed.
    """
    if not content or not _BARRED_RE.search(content):
        return content
    scrubbed = [_scrub_line(ln) for ln in content.splitlines()]
    return _drop_empty_sections("\n".join(ln for ln in scrubbed if ln is not None))
