"""
Canonical metrics configuration for the Quant DB correlation engine.

Each metric maps to a specific database table/column with a transformation.
Transformations:
    log_return  -- ln(close_t / close_{t-1}), for price series
    first_diff  -- value_t - value_{t-1}, for stock/level variables (M2, supply)
    raw         -- use value as-is, for rates/indices (VIX, yields, funding)

To add a metric: add an entry here. The correlation engine picks it up
automatically on the next run.
"""

# fmt: off
CANONICAL_METRICS = {
    # -- Price returns (log return of daily close) ---------------------
    'BTC':      {'table': 'price_daily', 'asset': 'BTC',    'source': 'binance', 'transform': 'log_return', 'category': 'price'},
    'ETH':      {'table': 'price_daily', 'asset': 'ETH',    'source': 'binance', 'transform': 'log_return', 'category': 'price'},
    'SOL':      {'table': 'price_daily', 'asset': 'SOL',    'source': 'binance', 'transform': 'log_return', 'category': 'price'},
    'SPX':      {'table': 'price_daily', 'asset': 'SPX',    'source': 'yahoo',   'transform': 'log_return', 'category': 'price'},
    'GOLD':     {'table': 'price_daily', 'asset': 'GOLD_F', 'source': 'yahoo',   'transform': 'log_return', 'category': 'price'},
    'OIL':      {'table': 'price_daily', 'asset': 'OIL_F',  'source': 'yahoo',   'transform': 'log_return', 'category': 'price'},
    'DXY':      {'table': 'price_daily', 'asset': 'DXY',    'source': 'yahoo',   'transform': 'log_return', 'category': 'price'},
    'TLT':      {'table': 'price_daily', 'asset': 'TLT',    'source': 'yahoo',   'transform': 'log_return', 'category': 'price'},
    'HYG':      {'table': 'price_daily', 'asset': 'HYG',    'source': 'yahoo',   'transform': 'log_return', 'category': 'price'},

    # -- Chain analysis: additional cross-asset prices ----------------
    'NASDAQ':   {'table': 'price_daily', 'asset': 'NASDAQ',   'source': 'yahoo', 'transform': 'log_return', 'category': 'price'},
    'COPPER':   {'table': 'price_daily', 'asset': 'COPPER_F', 'source': 'yahoo', 'transform': 'log_return', 'category': 'price'},
    'EEM':      {'table': 'price_daily', 'asset': 'EEM',      'source': 'yahoo', 'transform': 'log_return', 'category': 'price'},

    # -- Macro levels / rates -----------------------------------------
    'FED_FUNDS':   {'table': 'macro', 'metric': 'FED_FUNDS',   'transform': 'raw',        'category': 'macro'},
    'YIELD_10Y':   {'table': 'macro', 'metric': 'YIELD_10Y',   'transform': 'raw',        'category': 'macro'},
    'YIELD_10Y2Y': {'table': 'macro', 'metric': 'YIELD_10Y2Y', 'transform': 'raw',        'category': 'macro'},
    'M2':            {'table': 'macro', 'metric': 'M2',            'transform': 'first_diff',  'category': 'macro'},
    'NET_LIQUIDITY': {'table': 'macro', 'metric': 'NET_LIQUIDITY','transform': 'first_diff', 'category': 'macro'},
    'HY_SPREAD':   {'table': 'macro', 'metric': 'HY_SPREAD',   'transform': 'raw',        'category': 'macro'},

    # -- Chain analysis: additional macro metrics ---------------------
    'YIELD_2Y':      {'table': 'macro', 'metric': 'YIELD_2Y',      'transform': 'raw',        'category': 'macro'},
    'YIELD_10Y3M':   {'table': 'macro', 'metric': 'YIELD_10Y3M',   'transform': 'raw',        'category': 'macro'},
    'HY_SPREAD_EU':  {'table': 'macro', 'metric': 'HY_SPREAD_EU',  'transform': 'raw',        'category': 'macro'},
    'BREAKEVEN_10Y': {'table': 'macro', 'metric': 'BREAKEVEN_10Y', 'transform': 'raw',        'category': 'macro'},
    'FED_BALANCE':   {'table': 'macro', 'metric': 'FED_BALANCE',   'transform': 'first_diff', 'category': 'macro'},

    # VIX: level (not returns) -- it's a volatility index, raw value
    # is meaningful for regime correlation
    'VIX':         {'table': 'price_daily', 'asset': 'VIX', 'source': 'yahoo', 'transform': 'raw', 'category': 'macro'},

    # -- Derivatives --------------------------------------------------
    'FUNDING_BTC':    {'table': 'derivatives', 'asset': 'BTC', 'column': 'funding_rate_avg',       'transform': 'raw', 'category': 'derivatives'},
    'FUNDING_ETH':    {'table': 'derivatives', 'asset': 'ETH', 'column': 'funding_rate_avg',       'transform': 'raw', 'category': 'derivatives'},
    'OPTIONS_PC_BTC': {'table': 'derivatives', 'asset': 'BTC', 'column': 'options_put_call_ratio', 'transform': 'raw', 'category': 'derivatives'},
    'DVOL_BTC':       {'table': 'derivatives', 'asset': 'BTC', 'column': 'dvol',                   'transform': 'raw', 'category': 'derivatives'},
    'RVOL_BTC':       {'table': 'sentiment',   'metric': 'REALIZED_VOL_BTC', 'asset': 'BTC', 'transform': 'raw', 'category': 'derivatives'},
    'RVOL_ETH':       {'table': 'sentiment',   'metric': 'REALIZED_VOL_ETH', 'asset': 'ETH', 'transform': 'raw', 'category': 'derivatives'},
    'SKEW_25D_BTC':   {'table': 'derivatives', 'asset': 'BTC', 'column': 'skew_25d',       'transform': 'raw', 'category': 'derivatives'},
    'BASIS_BTC':      {'table': 'derivatives', 'asset': 'BTC', 'column': 'basis_pct',      'transform': 'raw', 'category': 'derivatives'},
    'GEX_BTC':        {'table': 'derivatives', 'asset': 'BTC', 'column': 'gex_net',        'transform': 'raw', 'category': 'derivatives'},
    'IV_RV_BTC':      {'table': 'derivatives', 'asset': 'BTC', 'column': 'iv_rv_premium',  'transform': 'raw', 'category': 'derivatives'},

    # -- Sentiment ----------------------------------------------------
    'FEAR_GREED':        {'table': 'sentiment', 'metric': 'FEAR_GREED',             'asset': '_GLOBAL_', 'transform': 'raw',        'category': 'sentiment'},
    'STABLECOIN_SUPPLY': {'table': 'sentiment', 'metric': 'STABLECOIN_USDT_SUPPLY', 'asset': '_GLOBAL_', 'transform': 'first_diff', 'category': 'sentiment'},
    'STABLECOIN_DOM':    {'table': 'sentiment', 'metric': 'STABLECOIN_DOMINANCE',   'asset': '_GLOBAL_', 'transform': 'raw',        'category': 'sentiment'},
    'ALTCOIN_SEASON':    {'table': 'sentiment', 'metric': 'ALTCOIN_SEASON',         'asset': '_GLOBAL_', 'transform': 'raw',        'category': 'sentiment'},
    'GTREND_BUY_BTC':    {'table': 'sentiment', 'metric': 'GTREND_BUY_BITCOIN',    'asset': '_GLOBAL_', 'transform': 'raw',        'category': 'sentiment'},
    'COT_BTC_SPEC':      {'table': 'sentiment', 'metric': 'COT_BTC_SPEC_NET',      'asset': '_GLOBAL_', 'transform': 'raw',        'category': 'sentiment'},

    'CROSS_DISPERSION':  {'table': 'sentiment', 'metric': 'CROSS_DISPERSION',  'asset': '_GLOBAL_', 'transform': 'raw',        'category': 'sentiment'},

    # -- On-chain -----------------------------------------------------
    'ETF_BTC_FLOW':      {'table': 'onchain', 'metric': 'ETF_BTC_NET_FLOW', 'asset': 'BTC', 'transform': 'raw', 'category': 'sentiment'},

    # BTC network fundamentals (BUILD #3, ONCHAIN-1 — mempool.space, collected daily).
    #
    # BTC_MINER_REVENUE_USD: transform='raw' — absolute USD revenue level is the signal.
    #   High miner revenue → miners hold / accumulate; low → sell pressure (hash-ribbon).
    #   Using 'first_diff' would lose the level signal (comparable to FEAR_GREED / FUNDING_BTC
    #   which are also stored as raw levels). Revenue already captures day-over-day variation
    #   through fee-market dynamics without needing differencing.
    #
    # BTC_HASHRATE: transform='first_diff' — hashrate is a monotonically increasing stock
    #   variable (analogous to M2 money supply, also first_diff). Its absolute level has
    #   shifted ~10,000× since 2010; a raw correlation with BTC price would be dominated
    #   by secular trend. first_diff captures miner additions/removals — the meaningful
    #   signal for hash-ribbon / miner-capitulation detection.
    #
    # BTC_TX_FEES_USD: transform='first_diff' — fee revenue is price-inflated
    #   (fee_BTC × BTC_close), creating a spurious correlation with any BTC price series
    #   if used raw. first_diff removes the price level and isolates fee demand shocks
    #   (congestion events, ordinals waves, mempool clearances).
    #
    # BTC_MEMPOOL_VBYTES: transform='raw' — mempool backlog is a stationary oscillator
    #   bounded by clearing blocks and demand waves. The absolute level IS the signal
    #   (high = demand surge / congestion; low = idle). Comparable to FEAR_GREED (raw 0-100).
    #
    # BTC_DIFFICULTY_ADJ: transform='raw' — already a periodic delta expressed as percent
    #   change per ~2-week epoch. Writing it as first_diff would double-difference a signal
    #   that is already a rate of change. Comparable to OPTIONS_PC_BTC (raw level).
    #
    # BTC_BLOCK_HEIGHT: NOT in CANONICAL_METRICS — display-only counter used by the
    #   report block to derive circulating supply. Not meaningful for z-score/lead-lag.
    'BTC_MINER_REVENUE_USD': {'table': 'onchain', 'metric': 'BTC_MINER_REVENUE_USD', 'asset': 'BTC', 'transform': 'raw',        'category': 'sentiment'},
    'BTC_HASHRATE':          {'table': 'onchain', 'metric': 'BTC_HASHRATE',          'asset': 'BTC', 'transform': 'first_diff', 'category': 'sentiment'},
    'BTC_TX_FEES_USD':       {'table': 'onchain', 'metric': 'BTC_TX_FEES_USD',       'asset': 'BTC', 'transform': 'first_diff', 'category': 'sentiment'},
    'BTC_MEMPOOL_VBYTES':    {'table': 'onchain', 'metric': 'BTC_MEMPOOL_VBYTES',    'asset': 'BTC', 'transform': 'raw',        'category': 'sentiment'},
    'BTC_DIFFICULTY_ADJ':    {'table': 'onchain', 'metric': 'BTC_DIFFICULTY_ADJ',    'asset': 'BTC', 'transform': 'raw',        'category': 'sentiment'},

    # Hyperliquid smart-money positioning bias (BUILD #2, ONCHAIN-1).
    # Bounded 0-100 oscillator: 0=all smart-money short, 50=balanced, 100=all long.
    # Transform: 'raw' — the absolute bias level IS the signal (analogous to
    # FEAR_GREED / ALTCOIN_SEASON, both raw 0-100 indices). Using 'first_diff'
    # would reduce it to day-over-day shift; 'log_return' is for price series.
    # Collected daily from api.hyperliquid.xyz via quant.collectors.hyperliquid_smart_money.
    'SM_BIAS_BTC': {'table': 'onchain', 'metric': 'SMART_MONEY_BIAS', 'asset': 'BTC', 'transform': 'raw', 'category': 'sentiment'},
    'SM_BIAS_ETH': {'table': 'onchain', 'metric': 'SMART_MONEY_BIAS', 'asset': 'ETH', 'transform': 'raw', 'category': 'sentiment'},

    # -- Composite factors (PCA-derived, stored in sentiment) ----------
    'RISK_APPETITE':  {'table': 'sentiment', 'metric': 'FACTOR_RISK_APPETITE',  'asset': '_GLOBAL_', 'transform': 'raw', 'category': 'macro'},
    'LIQUIDITY':      {'table': 'sentiment', 'metric': 'FACTOR_LIQUIDITY',      'asset': '_GLOBAL_', 'transform': 'raw', 'category': 'macro'},
    'CREDIT_STRESS':  {'table': 'sentiment', 'metric': 'FACTOR_CREDIT_STRESS',  'asset': '_GLOBAL_', 'transform': 'raw', 'category': 'macro'},
}
# fmt: on

# Rolling correlation windows (calendar days)
CORRELATION_WINDOWS = [60, 120]

# Minimum observations required for a valid correlation
MIN_OBSERVATIONS = 30

# ── Lead-lag analysis ──────────────────────────────────
#
# Each tuple: (leader, target, (lag_min, lag_max), description)
#
# Lag semantics:
#   lag > 0  →  leader precedes target by `lag` days
#   lag < 0  →  target precedes leader by |lag| days
#
# Lag ranges vary by signal speed:
#   Macro (M2)         : ±90 days (~10-week reported lag in literature)
#   Macro/cross-asset  : ±30 days
#   Sentiment/deriv    : ±14 days (fast-moving signals)

LEADLAG_PAIRS = [
    ('M2',                'BTC', (-90, 90),  'M2 money supply → BTC (~10-week reported lag)'),
    ('NET_LIQUIDITY',     'BTC', (-90, 90),  'Net dollar liquidity → BTC (~multi-week reported lag)'),
    ('DXY',               'BTC', (-30, 30),  'Dollar strength → BTC'),
    ('DXY',               'ETH', (-30, 30),  'Dollar strength → ETH'),
    ('YIELD_10Y2Y',       'SPX', (-30, 30),  'Yield curve → S&P 500'),
    ('YIELD_10Y2Y',       'BTC', (-30, 30),  'Yield curve → BTC'),
    ('VIX',               'BTC', (-30, 30),  'Equity volatility → BTC'),
    ('HY_SPREAD',         'BTC', (-30, 30),  'Credit spread → BTC'),
    ('STABLECOIN_SUPPLY', 'BTC', (-30, 30),  'Stablecoin inflows → BTC'),
    ('FEAR_GREED',        'BTC', (-14, 14),  'Crypto sentiment → BTC'),
    ('COT_BTC_SPEC',      'BTC', (-14, 14),  'CME speculative positioning → BTC'),
    ('FUNDING_BTC',       'BTC', (-14, 14),  'Funding rate → BTC (short-term)'),
    ('FUNDING_BTC',       'BASIS_BTC', (-7, 7), 'Funding rate → futures basis (leverage chain)'),
    ('ETF_BTC_FLOW',      'BTC', (-14, 14),  'ETF net flows → BTC (supply/demand)'),
    ('SM_BIAS_BTC',       'BTC', (-7, 7),   'Hyperliquid whale positioning → BTC (fast-moving sentiment)'),
    ('SM_BIAS_ETH',       'ETH', (-7, 7),   'Hyperliquid whale positioning → ETH (fast-moving sentiment)'),
    ('BTC_MINER_REVENUE_USD', 'BTC', (-7, 7), 'Miner revenue USD → BTC (miner selling-pressure signal)'),
    ('BTC_HASHRATE',          'BTC', (-7, 7), 'Network hashrate → BTC (miner commitment / difficulty proxy)'),
    ('RVOL_BTC',          'BTC', (-14, 14),  'BTC realized vol → BTC (vol regime)'),
    ('SKEW_25D_BTC',      'BTC', (-14, 14),  'Options skew → BTC price direction'),
    ('SKEW_25D_BTC',      'RVOL_BTC', (-7, 7), 'Options skew → realized vol (derivatives chain)'),
    ('BASIS_BTC',         'SKEW_25D_BTC', (-7, 7), 'Futures basis → options skew (derivatives chain)'),
    ('BASIS_BTC',         'BTC', (-14, 14),  'Futures basis → BTC direction'),
    ('GEX_BTC',           'RVOL_BTC', (-7, 7), 'Gamma exposure → realized volatility'),
    ('IV_RV_BTC',         'BTC', (-14, 14),  'Vol premium → BTC (mean reversion)'),

    # ── Monetary policy chain ─────────────────────────────────────
    # FED_FUNDS → YIELD_2Y → YIELD_10Y → HY_SPREAD → SPX → BTC
    ('FED_FUNDS',   'YIELD_2Y',   (-30, 30), 'Fed funds → 2Y yield (policy transmission)'),
    ('YIELD_2Y',    'YIELD_10Y',  (-30, 30), '2Y yield → 10Y yield (term structure)'),
    ('YIELD_10Y',   'HY_SPREAD',  (-30, 30), '10Y yield → HY spread (credit pricing)'),
    ('HY_SPREAD',   'SPX',        (-30, 30), 'Credit stress → equities'),
    ('SPX',         'BTC',        (-14, 14), 'S&P 500 → BTC (risk transmission)'),

    # ── Dollar / liquidity chain ──────────────────────────────────
    # M2 → NET_LIQUIDITY → DXY → GOLD → BTC
    ('M2',            'NET_LIQUIDITY', (-90, 90), 'M2 → net liquidity'),
    ('NET_LIQUIDITY', 'DXY',          (-60, 60), 'Net liquidity → dollar'),
    ('DXY',           'GOLD',         (-30, 30), 'Dollar → gold (inverse)'),
    ('GOLD',          'BTC',          (-30, 30), 'Gold → BTC (hard asset rotation)'),

    # ── Risk appetite chain ───────────────────────────────────────
    # VIX → HY_SPREAD → SPX → BTC (SPX→BTC already above)
    ('VIX',         'HY_SPREAD',  (-14, 14), 'Vol spike → credit stress'),
    ('VIX',         'SPX',        (-14, 14), 'VIX → equity selloff'),

    # ── Cross-asset supplementary ─────────────────────────────────
    ('SPX',         'ETH',        (-14, 14), 'S&P 500 → ETH'),
    ('TLT',         'SPX',        (-30, 30), 'Bonds → equities (duration rotation)'),
    ('TLT',         'BTC',        (-30, 30), 'Bonds → BTC'),
    ('OIL',         'DXY',        (-30, 30), 'Oil → dollar (petrodollar)'),
    ('OIL',     'BREAKEVEN_10Y',  (-30, 30), 'Oil → inflation expectations'),
    ('COPPER',      'SPX',        (-30, 30), 'Copper → equities (Dr. Copper)'),
    ('EEM',         'BTC',        (-30, 30), 'Emerging markets → BTC'),
    ('DXY',         'EEM',        (-30, 30), 'Dollar → emerging markets'),

    # ── Composite factor pairs ────────────────────────────────────
    ('RISK_APPETITE', 'BTC', (-30, 30), 'Risk appetite factor → BTC'),
    ('RISK_APPETITE', 'SPX', (-30, 30), 'Risk appetite factor → equities'),
    ('LIQUIDITY',     'BTC', (-90, 90), 'Liquidity factor → BTC'),
    ('LIQUIDITY',     'SPX', (-60, 60), 'Liquidity factor → equities'),
    ('CREDIT_STRESS', 'BTC', (-30, 30), 'Credit stress factor → BTC'),
]

# Analysis window for lead-lag (calendar days, most recent)
LEADLAG_WINDOW = 730

# ── Composite factor definitions ────────────────────────
#
# Each factor is the first principal component of its component group.
# flip_sign_ref: ensure this metric's loading is positive (sign convention).
COMPOSITE_FACTORS = {
    'RISK_APPETITE': {
        'components': ['VIX', 'HY_SPREAD', 'TLT', 'DXY'],
        'flip_sign_ref': 'VIX',
        'description': 'Multi-dimensional risk appetite (PCA)',
    },
    'LIQUIDITY': {
        'components': ['M2', 'NET_LIQUIDITY', 'FED_BALANCE'],
        'flip_sign_ref': 'M2',
        'description': 'Aggregate liquidity conditions (PCA)',
    },
    'CREDIT_STRESS': {
        'components': ['HY_SPREAD', 'HY_SPREAD_EU', 'YIELD_10Y2Y'],
        'flip_sign_ref': 'HY_SPREAD',
        'description': 'Credit stress conditions (PCA)',
    },
}

# ── Transmission chains ──────────────────────────────────
#
# Named macro transmission paths for the /chain API endpoint.
# Each chain shows r at every link so you can see where signal
# breaks down (e.g. "credit → equities is strong but equities → BTC
# is the weak link").
TRANSMISSION_CHAINS = {
    'monetary_policy': {
        'description': 'FED_FUNDS → YIELD_2Y → YIELD_10Y → HY_SPREAD → SPX → BTC',
        'links': [
            ('FED_FUNDS', 'YIELD_2Y'),
            ('YIELD_2Y',  'YIELD_10Y'),
            ('YIELD_10Y', 'HY_SPREAD'),
            ('HY_SPREAD', 'SPX'),
            ('SPX',       'BTC'),
        ],
    },
    'dollar_liquidity': {
        'description': 'M2 → NET_LIQUIDITY → DXY → GOLD → BTC',
        'links': [
            ('M2',            'NET_LIQUIDITY'),
            ('NET_LIQUIDITY', 'DXY'),
            ('DXY',           'GOLD'),
            ('GOLD',          'BTC'),
        ],
    },
    'risk_appetite': {
        'description': 'VIX → HY_SPREAD → SPX → BTC',
        'links': [
            ('VIX',       'HY_SPREAD'),
            ('HY_SPREAD', 'SPX'),
            ('SPX',       'BTC'),
        ],
    },
    'derivatives_leverage': {
        'description': 'FUNDING_BTC → BASIS_BTC → SKEW_25D_BTC → RVOL_BTC → BTC',
        'links': [
            ('FUNDING_BTC',  'BASIS_BTC'),
            ('BASIS_BTC',    'SKEW_25D_BTC'),
            ('SKEW_25D_BTC', 'RVOL_BTC'),
            ('RVOL_BTC',     'BTC'),
        ],
    },
}

# ── Regime detection ──────────────────────────────────
#
# HMM features: keys into CANONICAL_METRICS
# BTC return captures the return regime, VIX captures volatility,
# HY_SPREAD captures credit conditions — the macro trifecta.
REGIME_FEATURES = ['BTC', 'VIX', 'HY_SPREAD']

# PELT change-point detection parameters
PELT_MIN_SIZE = 30    # Minimum segment length (days)
PELT_MODEL = 'rbf'    # Kernel-based cost function

# ── Conditional analysis & analogues ──────────────────
#
# Aliases: map short canonical names to z-score metric names where they differ
ZSCORE_ALIASES = {
    'COT_BTC_SPEC':      'COT_BTC_SPEC_NET',
    'STABLECOIN_SUPPLY': 'STABLECOIN_USDT_SUPPLY',
    'GTREND_BUY_BTC':    'GTREND_BUY_BITCOIN',
}

# Target asset → (asset, source) for forward return lookups in price_daily
TARGET_ASSETS = {
    'BTC':  ('BTC',    'binance'),
    'ETH':  ('ETH',    'binance'),
    'SOL':  ('SOL',    'binance'),
    'SPX':  ('SPX',    'yahoo'),
    'GOLD': ('GOLD_F', 'yahoo'),
    'OIL':  ('OIL_F',  'yahoo'),
    'DXY':  ('DXY',    'yahoo'),
    'TLT':  ('TLT',    'yahoo'),
    'HYG':  ('HYG',    'yahoo'),
    'NASDAQ': ('NASDAQ',   'yahoo'),
    'COPPER': ('COPPER_F', 'yahoo'),
    'EEM':    ('EEM',      'yahoo'),
}

# Core metrics for historical analogue z-score comparison (3 domains)
ANALOGUE_METRICS = [
    'VIX', 'HY_SPREAD', 'YIELD_10Y', 'YIELD_10Y2Y', 'M2', 'NET_LIQUIDITY',
    'FED_FUNDS', 'FEAR_GREED', 'COT_BTC_SPEC_NET', 'FUNDING_BTC',
    'SKEW_25D_BTC', 'BASIS_BTC', 'DVOL_BTC',
]

# Anomaly cluster thresholds
CLUSTER_MIN_METRICS = 3   # Minimum extreme metrics (|z| >= 2)
CLUSTER_MIN_DOMAINS = 2   # Must span at least this many domains
