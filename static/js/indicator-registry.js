/* Indicator Registry — Single source of truth for all indicators.
 * Must load after klinecharts.min.js, before all other chart scripts. */
(function() {
    'use strict';

    // Ordered categories for menu display
    var CATEGORIES = [
        { id: 'custom', label: 'Custom', htmlLabel: 'Custom' },
        { id: 'oscillators', label: 'Oscillators', htmlLabel: 'Oscillators' },
        { id: 'trend', label: 'Trend & MAs', htmlLabel: 'Trend &amp; MAs' },
        { id: 'levels', label: 'Levels & Zones', htmlLabel: 'Levels &amp; Zones' },
        { id: 'volume', label: 'Volume & Flow', htmlLabel: 'Volume &amp; Flow' },
        { id: 'structure', label: 'Structure', htmlLabel: 'Structure' },
        { id: 'macro', label: 'Macro & Quant', htmlLabel: 'Macro &amp; Quant' }
    ];

    // Ordered indicator keys per category (defines menu + mobile sheet order)
    var CATEGORY_KEYS = {
        oscillators: ['RSI', 'RSI_DIV', 'MACD', 'TTM_SQUEEZE', 'BBW_PCT', 'ATR_PCT', 'NR7', 'WILLIAMS_R', 'CCI', 'STOCH', 'STOCH_RSI'],
        trend: ['MA', 'EMA', 'DAILY_EMA', 'WEEKLY_MA', 'BULL_BAND', 'CM_ULTIMATE', 'EMA_RIBBON', 'CONSENSIO', 'CONSENSIO_LITE', 'ALLIGATOR', 'ICHIMOKU', 'GAUSS_CHAN', 'PI_CYCLE', 'SMA_SIGNAL', 'BOLL', 'KELTNER', 'KELTNER_TIGHT', 'DONCHIAN', 'PSAR', 'HULL_MA', 'EMA_CROSS', 'SUPER_TREND', 'ALMA', 'KAMA'],
        levels: ['CPR', 'CPR_HISTORY', 'WEEKLY_CPR', 'WEEKLY_CPR_HISTORY', 'DAILY_CPR', 'DAILY_CPR_HISTORY', 'KEY_LEVELS', 'MON_RANGE', 'CONFLUENCE', 'LIQ_HEATMAP', 'SR_CHANNELS', 'AUTO_FIB', 'FVG'],
        volume: ['VOL', 'VP', 'NPOC', 'OBV', 'CVD_SPOT', 'CVD_DIV', 'CVD_USDT', 'CVD_COIN', 'OI', 'DERIV_COMBINED'],
        structure: ['MKT_STRUCTURE', 'ORDER_BLOCKS', 'EQ_HL', 'VALUE_ZONES', 'SESSIONS'],
        macro: ['MACRO_M2', 'MACRO_NET_LIQ', 'MACRO_DXY', 'MACRO_STABLECOIN', 'MACRO_FEAR_GREED', 'MACRO_FUNDING', 'MACRO_ETF_FLOW', 'MACRO_VIX', 'MACRO_YIELD_CURVE', 'MACRO_ALT_SEASON', 'MACRO_REGIME', 'MACRO_RISK_APPETITE', 'MACRO_LIQUIDITY', 'MACRO_CREDIT_STRESS']
    };

    // Full indicator metadata
    // Types: builtin_pane, builtin_overlay, pack_overlay, pack_pane, special, derivative
    var INDICATORS = {
        // === Volume (pane created first so it sits directly below the candle pane) ===
        VOL:            { label: 'Volume',           shortLabel: 'Vol',       category: 'volume', type: 'builtin_pane', paneId: 'volume_pane',    paneHeight: 100, calcParams: [20], defaultEnabled: true,
            desc: 'Standard volume bars with SMA(20) average. Green = up candle, red = down. Look for spikes on breakouts for confirmation, declining volume in trends for weakening, and volume climaxes at reversals.' },

        // === Oscillators ===
        RSI:        { label: 'RSI',            shortLabel: 'RSI',       category: 'oscillators', type: 'builtin_pane',  paneId: 'rsi_pane',      paneHeight: 90,  calcParams: [14],            defaultEnabled: true,
            desc: 'Relative Strength Index (14). Overbought >70, oversold <30. Centerline 50 acts as trend filter. Divergences between RSI and price signal potential reversals.',
            states: ['oversold', 'overbought', 'neutral']  },
        RSI_DIV:    { label: 'RSI Divergence', shortLabel: 'RSI Div',   category: 'oscillators', type: 'pack_overlay',  configKey: 'rsiDivergence', groupId: 'ind_rsiDiv',
            desc: 'Detects regular and hidden divergences between price swing points and RSI. Green = bullish, red = bearish. Solid = regular, dashed = hidden. More reliable on HTF (1D/1W).' },
        MACD:       { label: 'MACD',           shortLabel: 'MACD',      category: 'oscillators', type: 'builtin_pane',  paneId: 'macd_pane',     paneHeight: 70,
            desc: 'MACD line = EMA(12) \u2212 EMA(26), Signal = EMA(9) of MACD. Crossover above signal = bullish. Histogram expanding = momentum increasing. Zero-line cross = trend change.',
            states: ['bullish', 'bearish', 'flat']  },
        TTM_SQUEEZE:{ label: 'TTM Squeeze',    shortLabel: 'TTM',       category: 'oscillators', type: 'pack_pane',     configKey: 'ttmSqueeze',   groupId: 'ind_ttmSqueeze',
            desc: 'Server computes BB(30, 2.0) inside Keltner Channel(30, 2.5); squeeze flags after 3+ consecutive bars = volatility compression. Squeeze ending signals imminent explosive move. Longer squeezes \u2192 bigger breakouts.',
            states: ['squeeze_on', 'squeeze_off', 'firing_up', 'firing_down']  },
        BBW_PCT:    { label: 'BB Width %',     shortLabel: 'BBW%',      category: 'oscillators', type: 'pack_overlay',  configKey: 'bbwPct',       groupId: 'ind_bbwPct',
            desc: 'BB width ranked as percentile of last 120 bars. Cyan heatmap segments = unusually tight bands. Brighter = more extreme compression. Precedes volatility expansion.' },
        ATR_PCT:    { label: 'ATR %',          shortLabel: 'ATR%',      category: 'oscillators', type: 'pack_overlay',  configKey: 'atrPct',       groupId: 'ind_atrPct',
            desc: 'ATR(14) ranked as percentile of last 100 bars. Magenta segments = abnormally low volatility. Identifies coiled-spring setups. Combine with BBW% for confluence.' },
        NR7:        { label: 'NR7',            shortLabel: 'NR7',       category: 'oscillators', type: 'pack_overlay',  configKey: 'nr7',          groupId: 'ind_nr7',
            desc: 'Narrowest Range of 7 bars. Flags bars with the smallest range vs previous 6. Range expansion typically follows within 1\u20133 bars. Classic Crabel/Williams concept.' },
        WILLIAMS_R: { label: 'Williams %R',    shortLabel: '%R',        category: 'oscillators', type: 'builtin_pane',  paneId: 'wr_pane',       paneHeight: 90,  calcParams: [14],
            desc: 'Williams %R (14). Above \u221220 = overbought, below \u221280 = oversold. In strong trends it stays at extremes \u2014 use as timing tool within established trends, not counter-trend.' },
        CCI:        { label: 'CCI',            shortLabel: 'CCI',       category: 'oscillators', type: 'builtin_pane',  paneId: 'cci_pane',      paneHeight: 90,  calcParams: [20],
            desc: 'Commodity Channel Index (20). Above +100 = strong upward momentum, below \u2212100 = strong downward. Zero-line cross = momentum shift. Good for identifying cyclical turns.' },
        STOCH:      { label: 'Stochastic',     shortLabel: 'Stoch',     category: 'oscillators', type: 'builtin_pane',  paneId: 'stoch_pane',    paneHeight: 90,  calcParams: [14, 3, 3],
            desc: 'Stochastic (14,3,3). %K crossing %D below 20 = bullish, above 80 = bearish. In strong trends it stays pinned to extremes. Divergences with price signal reversals.' },
        STOCH_RSI:  { label: 'Stoch RSI',      shortLabel: 'StRSI',     category: 'oscillators', type: 'builtin_pane',  paneId: 'stochrsi_pane', paneHeight: 90,  calcParams: [14, 14, 3, 3],
            desc: 'Stochastic applied to RSI values. More sensitive \u2014 reaches extremes faster. Popular in crypto. In uptrends, traders watch for bounces out of oversold readings; in downtrends, for rejections from overbought ones.' },

        // === Trend & MAs ===
        MA:             { label: 'MA (20)',          shortLabel: 'MA',        category: 'trend', type: 'builtin_overlay',
            desc: 'Simple Moving Average (20). Equal-weighted average of last 20 closes. Price above = bullish bias. Slope shows trend direction. Acts as dynamic support/resistance.' },
        EMA:            { label: 'EMA (21)',         shortLabel: 'EMA',       category: 'trend', type: 'builtin_overlay',
            desc: 'Exponential Moving Average (21). Reacts faster than SMA due to exponential weighting. Standard short-term trend filter. Crossovers with price generate signals.' },
        DAILY_EMA:      { label: 'Daily EMAs',       shortLabel: 'D-EMA',     category: 'trend', type: 'pack_overlay', configKey: 'dailyEma',       groupId: 'ind_dailyEma',
            desc: 'EMA 21/50/200 from daily candles on any timeframe. 200 EMA is the most-watched trend indicator. Golden cross (50>200) = bull market. 200 EMA often acts as pullback support.' },
        WEEKLY_MA:      { label: 'Weekly MAs',       shortLabel: 'W-MA',      category: 'trend', type: 'pack_overlay', configKey: 'weeklyMa',       groupId: 'ind_weeklyMa',
            desc: 'SMA 50/100/200 from weekly candles. Long-term trend anchors. 200W SMA is a widely watched long-term reference. MA order (50>100>200) = bullish alignment.' },
        BULL_BAND:      { label: 'Bull Market Band', shortLabel: 'Bull Band', category: 'trend', type: 'pack_overlay', configKey: 'bullMarketBand', groupId: 'ind_bullBand',
            desc: '21-week EMA + 20-week SMA band. In bull markets, price finds support here on pullbacks. Closing below = potential bear phase. Band converging = major move incoming.' },
        CM_ULTIMATE:    { label: 'CM Ultimate',      shortLabel: 'CM Ult',    category: 'trend', type: 'pack_overlay', configKey: 'cmUltimate',     groupId: 'ind_cmUltimate',
            desc: 'Chris Moody\u2019s color-coded volume-weighted MA(20). Green when rising, red when falling. Color change = early trend shift signal. Best on daily/weekly timeframes.' },
        EMA_RIBBON:     { label: 'EMA Ribbon',       shortLabel: 'EMA Rib',   category: 'trend', type: 'pack_overlay', configKey: 'emaRibbon',      groupId: 'ind_emaRibbon',
            desc: 'EMA(30)/EMA(60) with color fill. Green = bullish (fast above slow by ATR margin), orange = bearish, yellow = choppy. Color transitions signal trend changes.',
            states: ['bullish', 'bearish', 'neutral']  },
        CONSENSIO:      { label: 'Consensio',        shortLabel: 'Consensio', category: 'trend', type: 'pack_overlay', configKey: 'consensio',      groupId: 'ind_consensio',
            desc: 'The Consensio method of D. Tyler Jenks (Lucid Investment Strategies), as implemented in Carrac: the ordering of 3/7/30-period SMAs on each timeframe. All MAs ordered fast > slow = full consensio. Shows trend alignment via MA convergence/divergence.',
            states: ['full_bull', 'strong_bull', 'bull', 'neutral', 'bear', 'strong_bear']  },
        CONSENSIO_LITE: { label: 'Consensio Lite',   shortLabel: 'Cons Lite', category: 'trend', type: 'pack_overlay', configKey: 'consensioLite',  groupId: 'ind_consensio',
            desc: 'Enabling this alone shows the compact multi-TF Consensio table without the MA lines on the chart \u2014 same alignment signal with less clutter. Pairs well with lower timeframes; enable Consensio too if you want the MA lines back.' },
        ALLIGATOR:      { label: 'Alligator',        shortLabel: 'Alligator', category: 'trend', type: 'pack_overlay', configKey: 'alligator',      groupId: 'ind_alligator',
            desc: 'Bill Williams\u2019 three smoothed MAs: Jaw (13), Teeth (8), Lips (5). Lines spreading = trending (eating), intertwined = ranging (sleeping). Traders often sit out while it is sleeping.',
            states: ['eating_bullish', 'eating_bearish', 'sleeping', 'mixed']  },
        ICHIMOKU:       { label: 'Ichimoku Cloud',   shortLabel: 'Ichimoku',  category: 'trend', type: 'pack_overlay', configKey: 'ichimoku',       groupId: 'ind_ichimoku',
            desc: 'Tenkan/Kijun + Cloud (Kumo). Price above cloud = bullish, below = bearish, inside = no trend. Cloud twist = potential reversal. Thick cloud = strong S/R.',
            states: ['above_cloud', 'inside_cloud', 'below_cloud'],
            statesTkCross: ['bullish', 'bearish', 'none']  },
        GAUSS_CHAN:     { label: 'Gaussian Channel', shortLabel: 'Gauss Ch',  category: 'trend', type: 'pack_overlay', configKey: 'gaussianChannel', groupId: 'ind_gaussChan',
            desc: 'Ultra-smooth Gaussian filter with ATR channel. Green when rising, red when falling. Price above channel = strong uptrend. Narrow channel = low-vol squeeze. Best on HTF.' },
        PI_CYCLE:       { label: 'Pi Cycle Top',     shortLabel: 'Pi Cycle',  category: 'trend', type: 'pack_overlay', configKey: 'piCycle',        groupId: 'ind_piCycle',
            desc: 'SMA(111) vs SMA(350)\u00d72. Crossover has historically called every BTC cycle top within 3 days. Only relevant for BTC on daily timeframe.' },
        SMA_SIGNAL:     { label: 'SMA 20/200',       shortLabel: 'SMA 20/200', category: 'trend', type: 'pack_overlay', configKey: 'smaSignal',      groupId: 'ind_smaSignal',
            desc: 'Plots SMA 20 and SMA 200 lines from daily data. Golden cross (20 over 200) = bullish context, death cross (20 under 200) = bearish context. Slope and spacing between the two lines show trend strength.' },
        BOLL:           { label: 'Bollinger',        shortLabel: 'Boll',      category: 'trend', type: 'builtin_overlay',
            desc: 'Bollinger Bands (20, 2.0). Bands widen with volatility, contract during compression. Price can walk the band in trends. Squeeze precedes breakout. W/M patterns at bands = reversal.' },
        KELTNER_TIGHT:  { label: 'Keltner Tight',     shortLabel: 'KC Tight',  category: 'trend', type: 'pack_overlay', configKey: 'keltnerTight',   groupId: 'ind_keltnerTight',
            desc: 'EMA(35) \u00b1 ATR(14)\u00d70.5. Tighter than standard Keltner. Price breaking outside = momentum shift. Riding upper/lower channel = strong trend.' },
        DONCHIAN:       { label: 'Donchian Channels',shortLabel: 'Donchian',  category: 'trend', type: 'pack_overlay', configKey: 'donchian',       groupId: 'ind_donchian',
            desc: 'Highest high / lowest low over 20 periods. Turtle Traders classic. Breakout above upper = new uptrend signal. Flat channels = consolidation.',
            states: ['at_high', 'at_low', 'inside']  },
        PSAR:           { label: 'Parabolic SAR',    shortLabel: 'PSAR',      category: 'trend', type: 'pack_overlay', configKey: 'parabolicSar',   groupId: 'ind_psar',
            desc: 'Stop and Reverse dots. Below price = bullish, above = bearish. Dots tightening = trend losing momentum. Best in trending markets \u2014 whipsaws in chop.',
            states: ['bullish', 'bearish']  },
        KELTNER:        { label: 'Keltner Channels', shortLabel: 'Keltner',   category: 'trend', type: 'pack_overlay', configKey: 'keltner',        groupId: 'ind_keltner',
            desc: 'EMA(20) \u00b1 ATR(10)\u00d71.5. Smoother than Bollinger (uses ATR vs StdDev). Used with BB for TTM Squeeze detection. Breakout = strong momentum.' },
        HULL_MA:        { label: 'Hull MA',          shortLabel: 'Hull',      category: 'trend', type: 'pack_overlay', configKey: 'hullMa',         groupId: 'ind_hullMa',
            desc: 'Hull Moving Average (20). Virtually eliminates lag using weighted MAs. Slope change = early reversal signal. Faster than EMA at detecting turns.' },
        EMA_CROSS:      { label: 'EMA Cross',        shortLabel: 'EMA\u00d7', category: 'trend', type: 'pack_overlay', configKey: 'emaCross',       groupId: 'ind_emaCross',
            desc: 'EMA(9) crossing EMA(21). Green triangle = bullish cross, red diamond = bearish cross. More responsive than 50/200 golden cross but more whipsaw-prone.',
            states: ['bullish', 'bearish']  },
        SUPER_TREND:    { label: 'SuperTrend',       shortLabel: 'STrend',    category: 'trend', type: 'pack_overlay', configKey: 'superTrend',     groupId: 'ind_superTrend',
            desc: 'ATR-based trend line (10, 3.0). Green below price = uptrend support, red above = downtrend resistance. Color flip = trend change. Best on higher timeframes.',
            states: ['bullish', 'bearish']  },
        ALMA:           { label: 'ALMA',             shortLabel: 'ALMA',      category: 'trend', type: 'pack_overlay', configKey: 'alma',           groupId: 'ind_alma',
            desc: 'Arnaud Legoux MA using Gaussian weights. Offset 0.85 reduces lag, sigma 6 controls smoothness. Tracks price more closely than EMA during trends with less overshoot.' },
        KAMA:           { label: 'KAMA',             shortLabel: 'KAMA',      category: 'trend', type: 'pack_overlay', configKey: 'kama',           groupId: 'ind_kama',
            desc: 'Kaufman Adaptive MA. Speeds up in trends, slows in chop based on Efficiency Ratio. Flat KAMA = no trend. Best MA for avoiding whipsaws in ranging markets.' },

        // === Levels & Zones ===
        CPR:            { label: 'CPR',              shortLabel: 'CPR',       category: 'levels', type: 'pack_overlay', configKey: 'cpr',           groupId: 'ind_cpr',
            desc: 'Monthly Central Pivot Range. Pivot = (H+L+C)/3 of previous month plus R1\u2013R3 and S1\u2013S3 levels. Narrow CPR = trending, wide = range-bound. R/S levels act as targets.',
            states: ['above_pivot', 'below_pivot', 'between', 'above_R1', 'above_R2', 'above_R3', 'below_S1', 'below_S2', 'below_S3']  },
        CPR_HISTORY:    { label: 'CPR History',      shortLabel: 'CPR Hist',  category: 'levels', type: 'pack_overlay', configKey: 'cprHistory',    groupId: 'ind_cprHistory',
            desc: 'Previous months\u2019 CPR levels. Pivot clusters = strong S/R zones. Virgin pivots (never tested) = levels price has not yet revisited.' },
        WEEKLY_CPR:     { label: 'Weekly CPR',       shortLabel: 'W CPR',     category: 'levels', type: 'pack_overlay', configKey: 'weeklyCpr',     groupId: 'ind_weeklyCpr',
            desc: 'Weekly Central Pivot Range. Uses previous week\u2019s H/L/C. Faster-reacting than monthly. Updates every Monday.',
            states: ['above_pivot', 'below_pivot', 'between', 'above_R1', 'above_R2', 'above_R3', 'below_S1', 'below_S2', 'below_S3']  },
        WEEKLY_CPR_HISTORY: { label: 'Weekly CPR History', shortLabel: 'W CPR H', category: 'levels', type: 'pack_overlay', configKey: 'weeklyCprHistory', groupId: 'ind_weeklyCprHistory',
            desc: 'Previous weeks\u2019 CPR levels for S/R cluster identification.' },
        DAILY_CPR:      { label: 'Daily CPR',        shortLabel: 'D CPR',     category: 'levels', type: 'pack_overlay', configKey: 'dailyCpr',      groupId: 'ind_dailyCpr',
            desc: 'Daily Central Pivot Range. Uses previous day\u2019s H/L/C. Best for intraday reference.',
            states: ['above_pivot', 'below_pivot', 'between', 'above_R1', 'above_R2', 'above_R3', 'below_S1', 'below_S2', 'below_S3']  },
        DAILY_CPR_HISTORY: { label: 'Daily CPR History', shortLabel: 'D CPR H', category: 'levels', type: 'pack_overlay', configKey: 'dailyCprHistory', groupId: 'ind_dailyCprHistory',
            desc: 'Previous days\u2019 CPR levels for intraday S/R clusters.' },
        KEY_LEVELS:     { label: 'Key Levels',       shortLabel: 'Key Lvl',   category: 'levels', type: 'special',
            desc: 'Previous day, week, and month highs and lows (PDH/PDL, PWH/PWL, PMH/PML) drawn as dashed lines projecting from the last candle. Watch for reactions as price approaches these levels.' },
        MON_RANGE:      { label: 'Monday Range',     shortLabel: 'Mon Rng',   category: 'levels', type: 'special',
            desc: 'Monday\u2019s high and low projected through the week. ICT concept \u2014 Monday often sets the weekly range. Breakout beyond Monday\u2019s range signals weekly direction.' },
        CONFLUENCE:     { label: 'Confluence Zones', shortLabel: 'Confl',     category: 'levels', type: 'special',
            desc: 'Price areas where multiple signals stack up \u2014 CPR, EMAs, volume profile (POC/VA), swings, Bollinger and key levels clustered into shaded bands. Thicker/brighter = stronger confluence; the label names the dominant source.' },
        LIQ_HEATMAP:    { label: 'Liquidation Heatmap', shortLabel: 'Liq Heat', category: 'levels', type: 'special',
            desc: 'Estimated liquidation clusters (model-based) \u2014 a time x price heatmap of where over-leveraged long and short positions may be sitting, inferred from our own Binance open-interest, price, and long/short-ratio history (never CoinGlass). Brighter/yellow = higher modeled concentration; dark = little inferred exposure. Informational estimate, not observed order-book liquidity or advice.' },
        SR_CHANNELS:    { label: 'S/R Channels',     shortLabel: 'S/R Ch',    category: 'levels', type: 'pack_overlay', configKey: 'srChannels',    groupId: 'ind_srChannels',
            desc: 'Auto-detected support/resistance from pivot clusters. Strength = number of touches. Red = resistance, green = support, gray = inside. Multi-touch = strongest levels.' },
        AUTO_FIB:       { label: 'Auto Fib',         shortLabel: 'Fib',       category: 'levels', type: 'pack_overlay', configKey: 'autoFib',       groupId: 'ind_autoFib',
            desc: 'Fibonacci retracements from the swing high/low over 265 bars. Key levels: 38.2%, 50%, 61.8% (golden ratio). Price stalling at fib levels = reversal/continuation zone.' },
        FVG:            { label: 'Fair Value Gap',   shortLabel: 'FVG',       category: 'levels', type: 'pack_overlay', configKey: 'fvg',           groupId: 'ind_fvg',
            desc: '3-candle pattern leaving unfilled price gap. ATR-filtered to remove noise. Green = bullish (below), red = bearish (above). Unfilled FVGs act as magnet zones.' },
        // === Volume & Flow ===
        VP:             { label: 'Volume Profile',   shortLabel: 'VP',        category: 'volume', type: 'special',      menuTooltip: 'Volume Profile histogram (V key)',
            desc: 'Volume at each price level across visible bars. POC = highest volume price = magnet level. High-volume nodes = consolidation zones. Low-volume nodes = fast-move zones. Toggle with V.' },
        NPOC:           { label: 'Naked POC',        shortLabel: 'nPOC',      category: 'volume', type: 'special',      menuTooltip: 'Naked point-of-control levels from past sessions (crypto only)',
            desc: 'Point-of-control levels from past sessions that price has not revisited since. An untouched (naked) POC acts as a magnet \u2014 price often returns to fill it. Crypto only.' },
        OBV:            { label: 'OBV',              shortLabel: 'OBV',       category: 'volume', type: 'builtin_pane', paneId: 'obv_pane',       paneHeight: 90,
            desc: 'On Balance Volume \u2014 running total adding volume on up closes, subtracting on down. Rising OBV + flat price = accumulation. OBV divergence from price = leading reversal signal.' },
        CVD_SPOT:       { label: 'CVD Spot',         shortLabel: 'CVD Sp',    category: 'volume', type: 'derivative',
            desc: 'Cumulative Volume Delta from Binance spot taker buy/sell data. Rising CVD + rising price = healthy trend. Price rising + CVD falling = bearish divergence (distribution).' },
        CVD_DIV:        { label: 'CVD Spot Div',     shortLabel: 'CVD Div',   category: 'volume', type: 'pack_overlay',  configKey: 'cvdDivergence', groupId: 'ind_cvdDiv',
            desc: 'Detects divergences between price and CVD Spot. Same logic as RSI Divergence applied to cumulative volume delta. Leading indicator for trend exhaustion.' },
        CVD_USDT:       { label: 'CVD Futures (USDT)', shortLabel: 'CVD U',   category: 'volume', type: 'derivative',   premium: true,
            desc: 'USDT-margined futures taker buy/sell CVD. Futures CVD diverging from spot = smart money positioning. Spikes = leveraged conviction.' },
        CVD_COIN:       { label: 'CVD Futures (COIN)', shortLabel: 'CVD C',   category: 'volume', type: 'derivative',   premium: true,
            desc: 'COIN-margined futures taker buy/sell CVD. Often reflects whale activity as COIN-M contracts are settled in the underlying asset.' },
        OI:             { label: 'Open Interest',    shortLabel: 'OI',        category: 'volume', type: 'derivative',   premium: true,
            desc: 'Binance USDT-M futures open interest. Rising OI + rising price = new longs. Rising OI + falling price = new shorts. Falling OI = positions closing. Spikes at extremes = squeeze risk.' },
        DERIV_COMBINED: { label: 'Derivatives (Combined)', shortLabel: 'Deriv', category: 'volume', type: 'derivative', premium: true,
            desc: 'CVD USDT-M, CVD COIN-M, and Open Interest combined in a single pane. Gives a full derivatives picture without multiple panes.' },

        // === Structure ===
        MKT_STRUCTURE:  { label: 'Structure',        shortLabel: 'Struct',    category: 'structure', type: 'special',
            desc: 'BOS (Break of Structure) and CHoCH (Change of Character) detection with swing labels. HH+HL = bullish, LH+LL = bearish. CHoCH = early reversal, BOS = continuation.',
            states: ['HH_HL', 'LH_LL', 'range']  },
        ORDER_BLOCKS:   { label: 'Order Blocks',     shortLabel: 'OB',        category: 'structure', type: 'special',
            desc: 'Institutional order zones at BOS/CHoCH points. Swing OBs (colored) and internal OBs (gray). Price returning to unmitigated OB = potential entry. Best on HTF for bias.' },
        EQ_HL:          { label: 'Equal H/L',         shortLabel: 'EQ H/L',    category: 'structure', type: 'special',
            desc: 'Swing points at similar price levels (within ATR threshold) = liquidity pools where stops cluster. Equal highs = sell-side liquidity, equal lows = buy-side. Often hunted before reversals.' },
        VALUE_ZONES:    { label: 'Value Zones',       shortLabel: 'Val Zone',  category: 'structure', type: 'special',
            desc: 'Divides current swing range into premium (above equilibrium), discount (below), and equilibrium. Traders using this framework look for longs in discount zones and shorts in premium ones, alongside structure context.' },
        SESSIONS:       { label: 'Market Sessions',  shortLabel: 'Sessions',  category: 'structure', type: 'pack_overlay', configKey: 'sessions',      groupId: 'ind_sessions',
            desc: 'Highlights Asian, London, and New York trading sessions on the chart. Useful for identifying session opens/closes as key liquidity events and range setups.' },

        // === Macro & Quant ===
        MACRO_M2:         { label: 'M2 Money Supply',         shortLabel: 'M2',        category: 'macro', type: 'quant_overlay', premiumOnly: true, quantSeries: 'M2',
            desc: 'US M2 money supply (weekly data, published monthly; right Y-axis). The classic macro-crypto leading indicator \u2014 M2 expansion leads BTC by ~10 weeks. Rising M2 = more liquidity available.' },
        MACRO_NET_LIQ:    { label: 'Net Liquidity',           shortLabel: 'Net Liq',   category: 'macro', type: 'quant_overlay', premiumOnly: true, quantSeries: 'NET_LIQUIDITY',
            desc: 'Fed Balance \u2212 TGA \u2212 Reverse Repo (weekly, right Y-axis). Direct measure of dollar liquidity in the banking system. Rising = bullish for risk assets.' },
        MACRO_DXY:        { label: 'DXY (Dollar Index)',      shortLabel: 'DXY',       category: 'macro', type: 'quant_overlay', premiumOnly: true, quantSeries: 'DXY',
            desc: 'US Dollar Index (daily, right Y-axis). Generally inverse to crypto \u2014 rising DXY = headwind, falling = tailwind. Watch for divergences.' },
        MACRO_STABLECOIN: { label: 'Stablecoin Supply',       shortLabel: 'Stables',   category: 'macro', type: 'quant_overlay', premiumOnly: true, quantSeries: 'STABLECOIN_USDT_SUPPLY',
            desc: 'USDT total supply (daily, right Y-axis). Rising = capital entering crypto ecosystem (dry powder). Rapid increase = bullish, declining = outflows.' },
        MACRO_FEAR_GREED: { label: 'Fear & Greed',            shortLabel: 'F&G',       category: 'macro', type: 'quant_pane', premiumOnly: true, quantSeries: 'FEAR_GREED', paneHeight: 60,
            desc: 'Crypto Fear & Greed Index (0\u2013100). Extreme Fear (<25) often marks bottoms, Extreme Greed (>75) often marks tops. Classic contrarian indicator.' },
        MACRO_FUNDING:    { label: 'Funding Rate (3-exch)',   shortLabel: 'Funding',   category: 'macro', type: 'quant_pane', premiumOnly: true, quantSeries: 'FUNDING', paneHeight: 60,
            desc: 'BTC perpetual funding rate averaged across Binance, OKX, and Bybit. Extreme positive = crowded longs, extreme negative = capitulation. Zero-line is equilibrium.' },
        MACRO_ETF_FLOW:   { label: 'BTC ETF Net Flow',       shortLabel: 'ETF',       category: 'macro', type: 'quant_pane', premiumOnly: true, quantSeries: 'ETF_FLOW', paneHeight: 60,
            desc: 'Daily net flows into/out of US spot BTC ETFs ($M). Positive = institutional buying, negative = outflows. Sustained flows signal regime changes.' },
        MACRO_VIX:        { label: 'VIX',                     shortLabel: 'VIX',       category: 'macro', type: 'quant_pane', premiumOnly: true, quantSeries: 'VIX', paneHeight: 60,
            desc: 'CBOE Volatility Index. Above 30 = high fear, below 15 = complacency. VIX spikes create risk-off moves across all assets including crypto.' },
        MACRO_YIELD_CURVE:{ label: 'Yield Curve (10Y-2Y)',    shortLabel: '10Y-2Y',    category: 'macro', type: 'quant_pane', premiumOnly: true, quantSeries: 'YIELD_CURVE', paneHeight: 60,
            desc: '10Y minus 2Y Treasury yield spread. Negative (inverted) = recession signal. Steepening after inversion often precedes macro bottoms and risk-on rallies.' },
        MACRO_ALT_SEASON: { label: 'Altcoin Season',          shortLabel: 'Alt Szn',   category: 'macro', type: 'quant_pane', premiumOnly: true, quantSeries: 'ALT_SEASON', paneHeight: 60,
            desc: 'Percentage of top altcoins outperforming BTC over 90 days (0\u2013100). Above 75 = alt season, below 25 = BTC dominance phase. Tracks rotation between BTC and altcoins.' },
        MACRO_REGIME:     { label: 'Market Regime (global)',   shortLabel: 'Regime',    category: 'macro', type: 'quant_background', premiumOnly: true, quantSeries: 'REGIME',
            desc: 'HMM-based regime classification. Green bands = favorable (risk-on), red bands = unfavorable (risk-off). Based on BTC returns, VIX, and HY spread. Updated nightly.' },
        MACRO_RISK_APPETITE: { label: 'Risk Appetite',          shortLabel: 'RiskApp',   category: 'macro', type: 'quant_pane', premiumOnly: true, quantSeries: 'RISK_APPETITE', paneHeight: 60,
            desc: 'PCA-derived risk appetite factor (VIX, HY spread, TLT, DXY). Positive = risk-on environment, negative = risk-off. Updated daily.' },
        MACRO_LIQUIDITY:  { label: 'Liquidity Factor',          shortLabel: 'Liq',      category: 'macro', type: 'quant_pane', premiumOnly: true, quantSeries: 'LIQUIDITY_FACTOR', paneHeight: 60,
            desc: 'PCA-derived liquidity factor (M2, Net Liquidity, Fed Balance). Positive = expanding liquidity (bullish macro), negative = tightening. Updated daily.' },
        MACRO_CREDIT_STRESS: { label: 'Credit Stress',          shortLabel: 'CrdStr',   category: 'macro', type: 'quant_pane', premiumOnly: true, quantSeries: 'CREDIT_STRESS_FACTOR', paneHeight: 60,
            desc: 'PCA-derived credit stress factor (HY spread, EU HY spread, yield curve). Positive = stress rising (bearish), negative = calm. Updated daily.' }
    };

    // Populate key field and defaults
    for (var k in INDICATORS) {
        INDICATORS[k].key = k;
        if (!INDICATORS[k].defaultEnabled) INDICATORS[k].defaultEnabled = false;
        if (!INDICATORS[k].premium) INDICATORS[k].premium = false;
    }

    // === Parameter Schemas (configurable indicator settings) ===
    // Keys match configKey (pack indicators) or indicator key (built-in)
    // Types: int, float, bool, color, select
    var PARAM_SCHEMAS = {
        // --- Trend & MAs ---
        dailyEma: [
            { key: 'ema21.color', label: 'EMA 21', type: 'color', tab: 'style' },
            { key: 'ema21.lineWidth', label: 'EMA 21 Width', type: 'int', min: 1, max: 5, tab: 'style' },
            { key: 'ema50.color', label: 'EMA 50', type: 'color', tab: 'style' },
            { key: 'ema50.lineWidth', label: 'EMA 50 Width', type: 'int', min: 1, max: 5, tab: 'style' },
            { key: 'ema200.color', label: 'EMA 200', type: 'color', tab: 'style' },
            { key: 'ema200.lineWidth', label: 'EMA 200 Width', type: 'int', min: 1, max: 5, tab: 'style' }
        ],
        weeklyMa: [
            { key: 'ma50.color', label: 'MA 50', type: 'color', tab: 'style' },
            { key: 'ma50.lineWidth', label: 'MA 50 Width', type: 'int', min: 1, max: 5, tab: 'style' },
            { key: 'ma100.color', label: 'MA 100', type: 'color', tab: 'style' },
            { key: 'ma100.lineWidth', label: 'MA 100 Width', type: 'int', min: 1, max: 5, tab: 'style' },
            { key: 'ma200.color', label: 'MA 200', type: 'color', tab: 'style' },
            { key: 'ma200.lineWidth', label: 'MA 200 Width', type: 'int', min: 1, max: 5, tab: 'style' }
        ],
        bullMarketBand: [
            { key: 'ema21w.color', label: '21W EMA', type: 'color', tab: 'style' },
            { key: 'ema21w.lineWidth', label: '21W EMA Width', type: 'int', min: 1, max: 5, tab: 'style' },
            { key: 'sma20w.color', label: '20W SMA', type: 'color', tab: 'style' },
            { key: 'sma20w.lineWidth', label: '20W SMA Width', type: 'int', min: 1, max: 5, tab: 'style' }
        ],
        cmUltimate: [
            { key: 'length', label: 'Length', type: 'int', min: 5, max: 100, tab: 'inputs' },
            { key: 'fillColor', label: 'Fill', type: 'color', tab: 'style' },
            { key: 'upColor', label: 'Up', type: 'color', tab: 'style' },
            { key: 'downColor', label: 'Down', type: 'color', tab: 'style' }
        ],
        emaRibbon: [
            { key: 'fastEmaLen', label: 'Fast EMA', type: 'int', min: 5, max: 100, tab: 'inputs' },
            { key: 'slowEmaLen', label: 'Slow EMA', type: 'int', min: 10, max: 200, tab: 'inputs' },
            { key: 'atrLen', label: 'ATR Length', type: 'int', min: 5, max: 200, tab: 'inputs' },
            { key: 'atrMult', label: 'ATR Mult', type: 'float', min: 0.05, max: 1.0, step: 0.05, tab: 'inputs' },
            { key: 'bullColor', label: 'Bull', type: 'color', tab: 'style' },
            { key: 'bearColor', label: 'Bear', type: 'color', tab: 'style' },
            { key: 'neutralColor', label: 'Neutral', type: 'color', tab: 'style' },
            { key: 'fillOpacity', label: 'Fill Opacity', type: 'float', min: 0, max: 0.5, step: 0.05, tab: 'style' }
        ],
        consensio: [
            { key: 'periodS', label: 'Short Period', type: 'int', min: 2, max: 20, tab: 'inputs' },
            { key: 'periodM', label: 'Medium Period', type: 'int', min: 3, max: 50, tab: 'inputs' },
            { key: 'periodL', label: 'Long Period', type: 'int', min: 10, max: 200, tab: 'inputs' },
            { key: 'showTable', label: 'Show Table', type: 'bool', tab: 'inputs' },
            { key: 'shortColor', label: 'Short MA', type: 'color', tab: 'style' },
            { key: 'mediumColor', label: 'Medium MA', type: 'color', tab: 'style' },
            { key: 'longColor', label: 'Long MA', type: 'color', tab: 'style' },
            { key: 'lineWidth', label: 'Line Width', type: 'int', min: 1, max: 5, tab: 'style' }
        ],
        alligator: [
            { key: 'jawLength', label: 'Jaw Length', type: 'int', min: 5, max: 50, tab: 'inputs' },
            { key: 'jawOffset', label: 'Jaw Offset', type: 'int', min: 1, max: 20, tab: 'inputs' },
            { key: 'teethLength', label: 'Teeth Length', type: 'int', min: 3, max: 30, tab: 'inputs' },
            { key: 'teethOffset', label: 'Teeth Offset', type: 'int', min: 1, max: 15, tab: 'inputs' },
            { key: 'lipsLength', label: 'Lips Length', type: 'int', min: 2, max: 20, tab: 'inputs' },
            { key: 'lipsOffset', label: 'Lips Offset', type: 'int', min: 1, max: 10, tab: 'inputs' },
            { key: 'jawColor', label: 'Jaw', type: 'color', tab: 'style' },
            { key: 'teethColor', label: 'Teeth', type: 'color', tab: 'style' },
            { key: 'lipsColor', label: 'Lips', type: 'color', tab: 'style' }
        ],
        ichimoku: [
            { key: 'tenkanLen', label: 'Tenkan-sen Period', type: 'int', min: 3, max: 50, tab: 'inputs' },
            { key: 'kijunLen', label: 'Kijun-sen Period', type: 'int', min: 5, max: 100, tab: 'inputs' },
            { key: 'senkouBLen', label: 'Senkou Span B Period', type: 'int', min: 22, max: 200, tab: 'inputs' },
            { key: 'displacement', label: 'Displacement', type: 'int', min: 1, max: 52, tab: 'inputs' },
            { key: 'tenkanColor', label: 'Tenkan-sen', type: 'color', tab: 'style' },
            { key: 'kijunColor', label: 'Kijun-sen', type: 'color', tab: 'style' },
            { key: 'senkouAColor', label: 'Senkou A (bull cloud)', type: 'color', tab: 'style' },
            { key: 'senkouBColor', label: 'Senkou B (bear cloud)', type: 'color', tab: 'style' },
            { key: 'chikouColor', label: 'Chikou Span', type: 'color', tab: 'style' },
            { key: 'cloudOpacity', label: 'Cloud Opacity', type: 'float', min: 0.02, max: 0.4, step: 0.02, tab: 'style' }
        ],
        gaussianChannel: [
            { key: 'poles', label: 'Poles', type: 'int', min: 1, max: 9, tab: 'inputs' },
            { key: 'period', label: 'Sampling Period', type: 'int', min: 2, max: 300, tab: 'inputs' },
            { key: 'mult', label: 'TR Multiplier', type: 'float', min: 0.1, max: 5.0, step: 0.1, tab: 'inputs' },
            { key: 'reducedLag', label: 'Reduced Lag', type: 'bool', tab: 'inputs' },
            { key: 'fastResponse', label: 'Fast Response', type: 'bool', tab: 'inputs' },
            { key: 'bullColor', label: 'Bull', type: 'color', tab: 'style' },
            { key: 'bearColor', label: 'Bear', type: 'color', tab: 'style' },
            { key: 'neutralColor', label: 'Neutral', type: 'color', tab: 'style' },
            { key: 'fillColor', label: 'Fill', type: 'color', tab: 'style' },
            { key: 'fillOpacity', label: 'Fill Opacity', type: 'float', min: 0.02, max: 0.4, step: 0.02, tab: 'style' }
        ],
        piCycle: [
            { key: 'ma111Color', label: '111 DMA', type: 'color', tab: 'style' },
            { key: 'ma350x2Color', label: '350x2 DMA', type: 'color', tab: 'style' }
        ],
        smaSignal: [
            { key: 'sma20Color', label: 'SMA 20', type: 'color', tab: 'style' },
            { key: 'sma200Color', label: 'SMA 200', type: 'color', tab: 'style' },
            { key: 'lineWidth', label: 'Line Width', type: 'int', min: 1, max: 5, tab: 'style' }
        ],
        keltnerTight: [
            { key: 'emaLength', label: 'EMA Length', type: 'int', min: 5, max: 100, tab: 'inputs' },
            { key: 'atrLength', label: 'ATR Length', type: 'int', min: 5, max: 100, tab: 'inputs' },
            { key: 'mult', label: 'Multiplier', type: 'float', min: 0.1, max: 5.0, step: 0.1, tab: 'inputs' },
            { key: 'basisColor', label: 'Basis', type: 'color', tab: 'style' },
            { key: 'upperColor', label: 'Upper', type: 'color', tab: 'style' },
            { key: 'lowerColor', label: 'Lower', type: 'color', tab: 'style' },
            { key: 'fillOpacity', label: 'Fill Opacity', type: 'float', min: 0, max: 0.3, step: 0.01, tab: 'style' }
        ],
        donchian: [
            { key: 'period', label: 'Period', type: 'int', min: 5, max: 200, tab: 'inputs' },
            { key: 'showMiddle', label: 'Show Middle', type: 'bool', tab: 'inputs' },
            { key: 'upperColor', label: 'Upper', type: 'color', tab: 'style' },
            { key: 'lowerColor', label: 'Lower', type: 'color', tab: 'style' },
            { key: 'middleColor', label: 'Middle', type: 'color', tab: 'style' },
            { key: 'fillOpacity', label: 'Fill Opacity', type: 'float', min: 0, max: 0.3, step: 0.01, tab: 'style' }
        ],
        parabolicSar: [
            { key: 'afStart', label: 'AF Start', type: 'float', min: 0.01, max: 0.1, step: 0.01, tab: 'inputs' },
            { key: 'afStep', label: 'AF Step', type: 'float', min: 0.01, max: 0.1, step: 0.01, tab: 'inputs' },
            { key: 'afMax', label: 'AF Max', type: 'float', min: 0.1, max: 0.5, step: 0.01, tab: 'inputs' },
            { key: 'bullColor', label: 'Bull', type: 'color', tab: 'style' },
            { key: 'bearColor', label: 'Bear', type: 'color', tab: 'style' },
            { key: 'dotSize', label: 'Dot Size', type: 'int', min: 1, max: 8, tab: 'style' }
        ],
        keltner: [
            { key: 'emaLength', label: 'EMA Length', type: 'int', min: 5, max: 100, tab: 'inputs' },
            { key: 'atrLength', label: 'ATR Length', type: 'int', min: 5, max: 100, tab: 'inputs' },
            { key: 'mult', label: 'Multiplier', type: 'float', min: 0.5, max: 5.0, step: 0.1, tab: 'inputs' },
            { key: 'basisColor', label: 'Basis', type: 'color', tab: 'style' },
            { key: 'upperColor', label: 'Upper', type: 'color', tab: 'style' },
            { key: 'lowerColor', label: 'Lower', type: 'color', tab: 'style' },
            { key: 'fillOpacity', label: 'Fill Opacity', type: 'float', min: 0, max: 0.3, step: 0.01, tab: 'style' }
        ],
        hullMa: [
            { key: 'period', label: 'Period', type: 'int', min: 5, max: 200, tab: 'inputs' },
            { key: 'color', label: 'Color', type: 'color', tab: 'style' },
            { key: 'lineWidth', label: 'Line Width', type: 'int', min: 1, max: 5, tab: 'style' }
        ],
        emaCross: [
            { key: 'fastPeriod', label: 'Fast Period', type: 'int', min: 2, max: 50, tab: 'inputs' },
            { key: 'slowPeriod', label: 'Slow Period', type: 'int', min: 5, max: 200, tab: 'inputs' },
            { key: 'showLines', label: 'Show Lines', type: 'bool', tab: 'inputs' },
            { key: 'bullColor', label: 'Bull Signal', type: 'color', tab: 'style' },
            { key: 'bearColor', label: 'Bear Signal', type: 'color', tab: 'style' },
            { key: 'fastColor', label: 'Fast Line', type: 'color', tab: 'style' },
            { key: 'slowColor', label: 'Slow Line', type: 'color', tab: 'style' }
        ],
        superTrend: [
            { key: 'period', label: 'ATR Period', type: 'int', min: 5, max: 100, tab: 'inputs' },
            { key: 'mult', label: 'Multiplier', type: 'float', min: 0.5, max: 10, step: 0.1, tab: 'inputs' },
            { key: 'bullColor', label: 'Bull', type: 'color', tab: 'style' },
            { key: 'bearColor', label: 'Bear', type: 'color', tab: 'style' },
            { key: 'lineWidth', label: 'Line Width', type: 'int', min: 1, max: 5, tab: 'style' }
        ],
        alma: [
            { key: 'period', label: 'Period', type: 'int', min: 5, max: 200, tab: 'inputs' },
            { key: 'offset', label: 'Offset', type: 'float', min: 0.01, max: 1.0, step: 0.01, tab: 'inputs' },
            { key: 'sigma', label: 'Sigma', type: 'int', min: 1, max: 20, tab: 'inputs' },
            { key: 'color', label: 'Color', type: 'color', tab: 'style' },
            { key: 'lineWidth', label: 'Line Width', type: 'int', min: 1, max: 5, tab: 'style' }
        ],
        kama: [
            { key: 'period', label: 'Period', type: 'int', min: 2, max: 100, tab: 'inputs' },
            { key: 'fastPeriod', label: 'Fast Period', type: 'int', min: 2, max: 30, tab: 'inputs' },
            { key: 'slowPeriod', label: 'Slow Period', type: 'int', min: 10, max: 200, tab: 'inputs' },
            { key: 'color', label: 'Color', type: 'color', tab: 'style' },
            { key: 'lineWidth', label: 'Line Width', type: 'int', min: 1, max: 5, tab: 'style' }
        ],
        // --- Oscillators ---
        rsiDivergence: [
            { key: 'period', label: 'RSI Period', type: 'int', min: 5, max: 50, tab: 'inputs' },
            { key: 'lookback', label: 'Lookback', type: 'int', min: 2, max: 20, tab: 'inputs' },
            { key: 'maxBarsBetween', label: 'Max Bars', type: 'int', min: 10, max: 120, tab: 'inputs' },
            { key: 'bullColor', label: 'Bull', type: 'color', tab: 'style' },
            { key: 'bearColor', label: 'Bear', type: 'color', tab: 'style' },
            { key: 'lineWidth', label: 'Line Width', type: 'int', min: 1, max: 5, tab: 'style' }
        ],
        ttmSqueeze: [
            { key: 'bbLength', label: 'BB Length', type: 'int', min: 5, max: 100, tab: 'inputs' },
            { key: 'bbMult', label: 'BB Mult', type: 'float', min: 0.5, max: 5.0, step: 0.1, tab: 'inputs' },
            { key: 'kcLength', label: 'KC Length', type: 'int', min: 5, max: 100, tab: 'inputs' },
            { key: 'kcMult', label: 'KC Mult', type: 'float', min: 0.5, max: 5.0, step: 0.1, tab: 'inputs' },
            { key: 'minDuration', label: 'Min Duration', type: 'int', min: 1, max: 10, tab: 'inputs' },
            { key: 'squeezeOnColor', label: 'Squeeze Color', type: 'color', tab: 'style' }
        ],
        bbwPct: [
            { key: 'bbLength', label: 'BB Length', type: 'int', min: 5, max: 100, tab: 'inputs' },
            { key: 'lookback', label: 'Lookback', type: 'int', min: 20, max: 300, tab: 'inputs' },
            { key: 'threshold', label: 'Threshold %', type: 'int', min: 5, max: 50, tab: 'inputs' },
            { key: 'color', label: 'Color', type: 'color', tab: 'style' }
        ],
        atrPct: [
            { key: 'atrLength', label: 'ATR Length', type: 'int', min: 5, max: 50, tab: 'inputs' },
            { key: 'lookback', label: 'Lookback', type: 'int', min: 20, max: 300, tab: 'inputs' },
            { key: 'threshold', label: 'Threshold %', type: 'int', min: 5, max: 50, tab: 'inputs' },
            { key: 'color', label: 'Color', type: 'color', tab: 'style' }
        ],
        nr7: [
            { key: 'lookback', label: 'Lookback', type: 'int', min: 3, max: 14, tab: 'inputs' },
            { key: 'color', label: 'Color', type: 'color', tab: 'style' }
        ],
        // --- Volume & Flow ---
        cvdDivergence: [
            { key: 'lookback', label: 'Lookback', type: 'int', min: 2, max: 20, tab: 'inputs' },
            { key: 'maxBarsBetween', label: 'Max Bars', type: 'int', min: 10, max: 120, tab: 'inputs' },
            { key: 'bullColor', label: 'Bull', type: 'color', tab: 'style' },
            { key: 'bearColor', label: 'Bear', type: 'color', tab: 'style' },
            { key: 'lineWidth', label: 'Line Width', type: 'int', min: 1, max: 5, tab: 'style' }
        ],
        // --- Levels & Zones ---
        cpr: [
            { key: 'timeframe', label: 'Timeframe', type: 'select', options: [
                { value: 'daily', label: 'Daily' }, { value: 'weekly', label: 'Weekly' },
                { value: 'monthly', label: 'Monthly' }, { value: 'quarterly', label: 'Quarterly' },
                { value: 'yearly', label: 'Yearly' }
            ], tab: 'inputs' },
            { key: 'pivotColor', label: 'Pivot', type: 'color', tab: 'style' },
            { key: 'rColor', label: 'Resistance', type: 'color', tab: 'style' },
            { key: 'sColor', label: 'Support', type: 'color', tab: 'style' }
        ],
        cprHistory: [
            { key: 'months', label: 'Months', type: 'int', min: 6, max: 120, tab: 'inputs' },
            { key: 'pivotColor', label: 'Color', type: 'color', tab: 'style' },
            { key: 'lineWidth', label: 'Line Width', type: 'int', min: 1, max: 5, tab: 'style' }
        ],
        autoFib: [
            { key: 'lookback', label: 'Lookback', type: 'int', min: 50, max: 500, tab: 'inputs' }
        ],
        srChannels: [
            { key: 'pivotPeriod', label: 'Pivot Period', type: 'int', min: 3, max: 30, tab: 'inputs' },
            { key: 'channelWidth', label: 'Channel Width', type: 'int', min: 1, max: 20, tab: 'inputs' },
            { key: 'minStrength', label: 'Min Strength', type: 'int', min: 1, max: 10, tab: 'inputs' },
            { key: 'maxChannels', label: 'Max Channels', type: 'int', min: 1, max: 20, tab: 'inputs' },
            { key: 'loopback', label: 'Lookback', type: 'int', min: 50, max: 500, tab: 'inputs' }
        ],
        fvg: [
            { key: 'fillByMid', label: 'Fill by Mid', type: 'bool', tab: 'inputs' },
            { key: 'showMidLine', label: 'Show Mid Line', type: 'bool', tab: 'inputs' },
            { key: 'autoThreshold', label: 'Auto Threshold', type: 'bool', tab: 'inputs' },
            { key: 'atrThresholdPct', label: 'ATR Threshold %', type: 'float', min: 0.05, max: 1.0, step: 0.05, tab: 'inputs' },
            { key: 'atrLength', label: 'ATR Length', type: 'int', min: 5, max: 50, tab: 'inputs' },
            { key: 'showMitigated', label: 'Show Mitigated', type: 'bool', tab: 'inputs' },
            { key: 'maxGaps', label: 'Max Gaps', type: 'int', min: 10, max: 200, tab: 'inputs' }
        ],
        // --- Structure ---
        sessions: [
            { key: 'rectHeight', label: 'Height', type: 'int', min: 2, max: 15, tab: 'style' },
            { key: 'bottomMargin', label: 'Bottom Margin', type: 'int', min: 0, max: 20, tab: 'style' },
            { key: 'labelSize', label: 'Label Size', type: 'int', min: 8, max: 16, tab: 'style' }
        ]
    };

    // Built-in KlineChart indicators: map calcParams positions
    var BUILTIN_PARAM_SCHEMAS = {
        RSI: [
            { calcParamIndex: 0, label: 'Period', type: 'int', min: 2, max: 100, tab: 'inputs' }
        ],
        MACD: [
            { calcParamIndex: 0, label: 'Fast', type: 'int', min: 2, max: 100, tab: 'inputs' },
            { calcParamIndex: 1, label: 'Slow', type: 'int', min: 2, max: 200, tab: 'inputs' },
            { calcParamIndex: 2, label: 'Signal', type: 'int', min: 2, max: 50, tab: 'inputs' }
        ],
        VOL: [
            { calcParamIndex: 0, label: 'MA Period', type: 'int', min: 5, max: 100, tab: 'inputs' }
        ],
        WILLIAMS_R: [
            { calcParamIndex: 0, label: 'Period', type: 'int', min: 2, max: 100, tab: 'inputs' }
        ],
        CCI: [
            { calcParamIndex: 0, label: 'Period', type: 'int', min: 2, max: 100, tab: 'inputs' }
        ],
        STOCH: [
            { calcParamIndex: 0, label: 'K Period', type: 'int', min: 2, max: 100, tab: 'inputs' },
            { calcParamIndex: 1, label: 'Smooth (%K)', type: 'int', min: 1, max: 50, tab: 'inputs' },
            { calcParamIndex: 2, label: 'D Period', type: 'int', min: 1, max: 20, tab: 'inputs' }
        ],
        STOCH_RSI: [
            { calcParamIndex: 0, label: 'RSI Period', type: 'int', min: 2, max: 100, tab: 'inputs' },
            { calcParamIndex: 1, label: 'Stoch Period', type: 'int', min: 2, max: 100, tab: 'inputs' },
            { calcParamIndex: 2, label: 'K Smooth', type: 'int', min: 1, max: 20, tab: 'inputs' },
            { calcParamIndex: 3, label: 'D Smooth', type: 'int', min: 1, max: 20, tab: 'inputs' }
        ],
        MA: [
            { calcParamIndex: 0, label: 'Period', type: 'int', min: 2, max: 500, tab: 'inputs' }
        ],
        EMA: [
            { calcParamIndex: 0, label: 'Period', type: 'int', min: 2, max: 500, tab: 'inputs' }
        ],
        BOLL: [
            { calcParamIndex: 0, label: 'Period', type: 'int', min: 5, max: 200, tab: 'inputs' },
            { calcParamIndex: 1, label: 'Multiplier', type: 'float', min: 0.5, max: 5.0, step: 0.1, tab: 'inputs' },
            { calcParamIndex: 2, label: 'Offset', type: 'int', min: -20, max: 20, step: 1, default: 0, tab: 'inputs' }
        ]
    };

    function hasSettings(key) {
        var ind = INDICATORS[key];
        if (!ind) return false;
        if (ind.configKey && PARAM_SCHEMAS[ind.configKey]) return true;
        if (BUILTIN_PARAM_SCHEMAS[key]) return true;
        return false;
    }

    function getParamSchema(key) {
        var ind = INDICATORS[key];
        if (!ind) return null;
        if (ind.configKey && PARAM_SCHEMAS[ind.configKey]) return PARAM_SCHEMAS[ind.configKey];
        if (BUILTIN_PARAM_SCHEMAS[key]) return BUILTIN_PARAM_SCHEMAS[key];
        return null;
    }

    // === Helper methods ===

    function getIndicator(key) {
        return INDICATORS[key] || null;
    }

    function getStates(key) {
        var ind = INDICATORS[key];
        return (ind && Array.isArray(ind.states)) ? ind.states.slice() : null;
    }

    function getAllKeys() {
        var keys = [];
        CATEGORIES.forEach(function(cat) {
            CATEGORY_KEYS[cat.id].forEach(function(k) { keys.push(k); });
        });
        return keys;
    }

    function getByType(type) {
        var result = {};
        for (var k in INDICATORS) {
            if (INDICATORS[k].type === type) result[k] = INDICATORS[k];
        }
        return result;
    }

    function getByCategory(category) {
        var result = {};
        for (var k in INDICATORS) {
            if (INDICATORS[k].category === category) result[k] = INDICATORS[k];
        }
        return result;
    }

    function getDefaults() {
        var d = {};
        for (var k in INDICATORS) d[k] = INDICATORS[k].defaultEnabled;
        return d;
    }

    function getLabels() {
        var m = {};
        for (var k in INDICATORS) m[k] = INDICATORS[k].shortLabel;
        return m;
    }

    // Maps UI toggle key (e.g. 'DAILY_EMA') → pack config key (e.g. 'dailyEma')
    function getPackConfigMap() {
        var m = {};
        for (var k in INDICATORS) {
            if (INDICATORS[k].configKey) m[k] = INDICATORS[k].configKey;
        }
        return m;
    }

    // Maps indicator key → pane ID for built-in pane indicators
    function getBuiltInPanes() {
        var m = {};
        for (var k in INDICATORS) {
            if (INDICATORS[k].type === 'builtin_pane') m[k] = INDICATORS[k].paneId;
        }
        return m;
    }

    // Maps indicator key → 'candle_pane' for built-in overlay indicators
    function getBuiltInOverlays() {
        var m = {};
        for (var k in INDICATORS) {
            if (INDICATORS[k].type === 'builtin_overlay') m[k] = 'candle_pane';
        }
        return m;
    }

    // Maps pack config key (e.g. 'dailyEma') → overlay groupId (e.g. 'ind_dailyEma')
    // Used by indicator pack for overlay cleanup
    function getGroupIds() {
        var m = {};
        for (var k in INDICATORS) {
            if (INDICATORS[k].configKey && INDICATORS[k].groupId) {
                m[INDICATORS[k].configKey] = INDICATORS[k].groupId;
            }
        }
        return m;
    }

    // Returns ordered array of { label, keys } for group editor / mobile sheet
    function getCategoryOrder() {
        return CATEGORIES.map(function(cat) {
            return { label: cat.label, keys: (CATEGORY_KEYS[cat.id] || []).slice() };
        });
    }

    // Generates complete indicator menu HTML (reads klineActiveIndicators at call time)
    function buildIndicatorMenuHTML() {
        // Scrollable body
        var html = '<div class="ind-menu-body">';
        html += '<div class="indicator-master-toggle" id="indicator-master-toggle">' +
                '<i data-lucide="eye"></i> Hide All <span class="shortcut-hint">I</span></div>' +
            '<div class="indicator-menu-divider"></div>';

        CATEGORIES.forEach(function(cat) {
            // Custom category: get keys dynamically from CustomIndicators
            var catKeys = cat.id === 'custom' && window.CustomIndicators ?
                window.CustomIndicators.getCustomCategoryKeys() : CATEGORY_KEYS[cat.id];
            if (!catKeys || catKeys.length === 0) {
                // Still show custom category header even when empty
                if (cat.id === 'custom') {
                    html += '<div class="indicator-menu-header">' + cat.htmlLabel + '</div>';
                    html += '<div class="indicator-menu-divider"></div>';
                }
                return;
            }
            html += '<div class="indicator-menu-header">' + cat.htmlLabel + '</div>';
            catKeys.forEach(function(key) {
                var ind = INDICATORS[key];
                var active = typeof klineActiveIndicators !== 'undefined' && klineActiveIndicators[key];
                html += '<div class="indicator-menu-item' + (active ? ' active' : '') + '" data-ind="' + key + '"';
                if (ind.premium || ind.premiumOnly) html += ' data-premium="true"';
                html += '><span class="ind-checkbox' + (active ? ' active' : '') + '"></span><span class="ind-label-text">' + ind.label;
                if (ind.menuSuffix) html += ind.menuSuffix;
                if (ind.premiumOnly && !window._hasPremiumFeatures) html += ' <span style="opacity:0.4;font-size:10px">PRO</span>';
                else if (ind.premium && !window._isPremium) html += ' <span style="opacity:0.4;font-size:10px">PRO</span>';
                html += '</span>';
                if (ind.type === 'user_formula') html += ' <span data-edit-formula="' + ind.formulaId + '" title="Edit formula" style="cursor:pointer;font-size:14px;opacity:0.5;padding:2px 6px;float:right;margin-right:20px">&#9998;</span>';
                if (hasSettings(key)) html += '<span class="ind-settings-gear" data-settings="' + key + '" title="Settings">&#9881;</span>';
                if (ind.desc) html += '<span class="ind-info-icon" data-desc="' + ind.desc.replace(/"/g, '&quot;') + '"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg></span>';
                html += '</div>';
            });
            html += '<div class="indicator-menu-divider"></div>';
        });
        html += '</div>';

        // Pinned footer — always visible at bottom
        html += '<div class="ind-menu-footer">';
        html += '<div class="ind-menu-footer-item fe-new-btn">' +
            '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>' +
            'New indicator</div>';
        html += '<div class="ind-menu-footer-item" id="ind-save-group-btn" style="display:none">' +
            '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="20" rx="2"/><path d="M12 8v8M8 12h8"/></svg>' +
            'Save as group</div>';
        html += '<a href="/indicators" target="_blank" class="ind-menu-footer-item ind-menu-footer-link">' +
            '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>' +
            'Indicator guide</a>';
        html += '</div>';
        return html;
    }

    // Returns ordered array of { key, label, section?, pro? } for mobile bottom sheet
    function buildMobileIndicatorList() {
        var list = [];
        CATEGORIES.forEach(function(cat) {
            var catKeys = cat.id === 'custom' && window.CustomIndicators ?
                window.CustomIndicators.getCustomCategoryKeys() : CATEGORY_KEYS[cat.id];
            if (!catKeys || catKeys.length === 0) return;
            catKeys.forEach(function(key, i) {
                var ind = INDICATORS[key];
                if (!ind) return;
                var item = { key: key, label: ind.label };
                if (i === 0) item.section = cat.label;
                // Same PRO-badge rule buildIndicatorMenuHTML uses for the desktop
                // menu: premiumOnly items need premium/tester/admin, plain premium
                // items (CVD_USDT etc.) need any paid tier.
                if (ind.premiumOnly && !window._hasPremiumFeatures) item.pro = true;
                else if (ind.premium && !window._isPremium) item.pro = true;
                list.push(item);
            });
        });
        return list;
    }

    // One-time localStorage migration: old indicator keys → new keys
    // Larsson Line → EMA Ribbon
    (function migrateLocalStorage() {
        var migrations = { 'LARSSON': 'EMA_RIBBON', 'MONEY_NOODLE': 'KELTNER_TIGHT' };
        // SMC split: if SMC was enabled, enable all 4 sub-indicators
        try {
            var storedSmc = localStorage.getItem('klineActiveIndicators');
            if (storedSmc) {
                var parsedSmc = JSON.parse(storedSmc);
                if (parsedSmc.SMC && !parsedSmc.MKT_STRUCTURE) {
                    parsedSmc.MKT_STRUCTURE = true;
                    parsedSmc.ORDER_BLOCKS = true;
                    parsedSmc.EQ_HL = true;
                    parsedSmc.VALUE_ZONES = true;
                    delete parsedSmc.SMC;
                    localStorage.setItem('klineActiveIndicators', JSON.stringify(parsedSmc));
                } else if (parsedSmc.SMC) {
                    delete parsedSmc.SMC;
                    localStorage.setItem('klineActiveIndicators', JSON.stringify(parsedSmc));
                }
            }
        } catch (e) { /* migration best-effort */ }
        var paramMigrations = { 'larsson': 'emaRibbon', 'moneyNoodle': 'keltnerTight' };
        try {
            // Migrate klineActiveIndicators
            var stored = localStorage.getItem('klineActiveIndicators');
            if (stored) {
                var parsed = JSON.parse(stored);
                var changed = false;
                for (var old in migrations) {
                    if (old in parsed && !(migrations[old] in parsed)) {
                        parsed[migrations[old]] = parsed[old];
                        delete parsed[old];
                        changed = true;
                    } else if (old in parsed) {
                        delete parsed[old];
                        changed = true;
                    }
                }
                if (changed) localStorage.setItem('klineActiveIndicators', JSON.stringify(parsed));
            }
            // Migrate favourites
            var favs = localStorage.getItem('indicatorFavourites');
            if (favs) {
                var favArr = JSON.parse(favs);
                var favChanged = false;
                for (var i = 0; i < favArr.length; i++) {
                    if (favArr[i] in migrations) {
                        favArr[i] = migrations[favArr[i]];
                        favChanged = true;
                    }
                }
                if (favChanged) localStorage.setItem('indicatorFavourites', JSON.stringify(favArr));
            }
            // Migrate indicator groups
            var groups = localStorage.getItem('indicatorGroups');
            if (groups) {
                var groupArr = JSON.parse(groups);
                var gChanged = false;
                groupArr.forEach(function(g) {
                    if (g && g.indicators) {
                        for (var old2 in migrations) {
                            if (old2 in g.indicators && !(migrations[old2] in g.indicators)) {
                                g.indicators[migrations[old2]] = g.indicators[old2];
                                delete g.indicators[old2];
                                gChanged = true;
                            } else if (old2 in g.indicators) {
                                delete g.indicators[old2];
                                gChanged = true;
                            }
                        }
                    }
                });
                if (gChanged) localStorage.setItem('indicatorGroups', JSON.stringify(groupArr));
            }
            // Migrate indicator params
            var params = localStorage.getItem('indicatorParams');
            if (params) {
                var paramObj = JSON.parse(params);
                var pChanged = false;
                for (var oldP in paramMigrations) {
                    if (oldP in paramObj && !(paramMigrations[oldP] in paramObj)) {
                        paramObj[paramMigrations[oldP]] = paramObj[oldP];
                        delete paramObj[oldP];
                        pChanged = true;
                    } else if (oldP in paramObj) {
                        delete paramObj[oldP];
                        pChanged = true;
                    }
                }
                if (pChanged) localStorage.setItem('indicatorParams', JSON.stringify(paramObj));
            }
        } catch (e) { /* migration best-effort */ }
    })();

    // Expose as global
    window.IndicatorRegistry = {
        getIndicator: getIndicator,
        getStates: getStates,
        getAllKeys: getAllKeys,
        getByCategory: getByCategory,
        getByType: getByType,
        getDefaults: getDefaults,
        getLabels: getLabels,
        getPackConfigMap: getPackConfigMap,
        getBuiltInPanes: getBuiltInPanes,
        getBuiltInOverlays: getBuiltInOverlays,
        getGroupIds: getGroupIds,
        getCategoryOrder: getCategoryOrder,
        buildIndicatorMenuHTML: buildIndicatorMenuHTML,
        buildMobileIndicatorList: buildMobileIndicatorList,
        hasSettings: hasSettings,
        getParamSchema: getParamSchema,
        CATEGORIES: CATEGORIES,
        INDICATORS: INDICATORS,
        PARAM_SCHEMAS: PARAM_SCHEMAS,
        BUILTIN_PARAM_SCHEMAS: BUILTIN_PARAM_SCHEMAS
    };
})();
