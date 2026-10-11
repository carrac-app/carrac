// PURE MODULE — no DOM, no chart, no globals beyond window.FormulaParser
// Consumed by: custom-indicators.js (chart), backtester.js (future)
//
// FormulaParser.evaluate(formulaString, ohlcv) → { values: number[], error: string|null }
// No eval(), no new Function(). Pure recursive descent parser.

(function() {
    'use strict';

    var T = window.TALib;

    // ── Function Table ──────────────────────────────────────────────────
    // types: 'd'=data array, 'p'=scalar param
    // prefix: OHLCV identifiers prepended when user provides fewer args
    // defaults: { position: value } for optional trailing params

    var FUNCTIONS = {
        // Moving averages
        sma:     { fn: T.sma,     types: 'dp',     prefix: ['close'], defaults: {} },
        ema:     { fn: T.ema,     types: 'dp',     prefix: ['close'], defaults: {} },
        wma:     { fn: T.wma,     types: 'dp',     prefix: ['close'], defaults: {} },
        hma:     { fn: T.hma,     types: 'dp',     prefix: ['close'], defaults: {} },
        alma:    { fn: T.alma,    types: 'dppp',   prefix: ['close'], defaults: { 2: 0.85, 3: 6 } },
        kama:    { fn: T.kama,    types: 'dppp',   prefix: ['close'], defaults: { 2: 2, 3: 30 } },

        // Oscillators
        rsi:      { fn: T.rsi,      types: 'dp',     prefix: ['close'], defaults: {} },
        cci:      { fn: T.cci,      types: 'dddp',   prefix: ['high', 'low', 'close'], defaults: {} },
        willr:    { fn: T.willr,    types: 'dddp',   prefix: ['high', 'low', 'close'], defaults: {} },
        stoch_k:  { fn: T.stoch_k,  types: 'dddpp',  prefix: ['high', 'low', 'close'], defaults: { 4: 1 } },
        stoch_d:  { fn: T.stoch_d,  types: 'dddppp', prefix: ['high', 'low', 'close'], defaults: { 4: 1, 5: 3 } },
        mfi:      { fn: T.mfi,      types: 'ddddp',  prefix: ['high', 'low', 'close', 'volume'], defaults: {} },
        adx:      { fn: T.adx,      types: 'dddp',   prefix: ['high', 'low', 'close'], defaults: {} },
        plus_di:  { fn: T.plus_di,  types: 'dddp',   prefix: ['high', 'low', 'close'], defaults: {} },
        minus_di: { fn: T.minus_di, types: 'dddp',   prefix: ['high', 'low', 'close'], defaults: {} },

        // Volatility
        tr:             { fn: T.tr,             types: 'ddd',    prefix: ['high', 'low', 'close'], defaults: {} },
        atr:            { fn: T.atr,            types: 'dddp',   prefix: ['high', 'low', 'close'], defaults: {} },
        stdev:          { fn: T.stdev,          types: 'dp',     prefix: ['close'], defaults: {} },
        bb_upper:       { fn: T.bb_upper,       types: 'dpp',    prefix: ['close'], defaults: {} },
        bb_lower:       { fn: T.bb_lower,       types: 'dpp',    prefix: ['close'], defaults: {} },
        bb_mid:         { fn: T.bb_mid,         types: 'dp',     prefix: ['close'], defaults: {} },
        keltner_upper:  { fn: T.keltner_upper,  types: 'dddpp',  prefix: ['high', 'low', 'close'], defaults: {} },
        keltner_lower:  { fn: T.keltner_lower,  types: 'dddpp',  prefix: ['high', 'low', 'close'], defaults: {} },

        // Trend
        supertrend:  { fn: T.supertrend,  types: 'dddpp',  prefix: ['high', 'low', 'close'], defaults: {} },
        psar:        { fn: T.psar,        types: 'ddpp',   prefix: ['high', 'low'], defaults: { 2: 0.02, 3: 0.2 } },
        macd_line:   { fn: T.macd_line,   types: 'dppp',   prefix: ['close'], defaults: { 1: 12, 2: 26, 3: 9 } },
        macd_signal: { fn: T.macd_signal, types: 'dppp',   prefix: ['close'], defaults: { 1: 12, 2: 26, 3: 9 } },
        macd_hist:   { fn: T.macd_hist,   types: 'dppp',   prefix: ['close'], defaults: { 1: 12, 2: 26, 3: 9 } },

        // Volume
        obv:  { fn: T.obv,  types: 'dd',    prefix: ['close', 'volume'], defaults: {} },
        ad:   { fn: T.ad,   types: 'dddd',  prefix: ['high', 'low', 'close', 'volume'], defaults: {} },
        cmf:  { fn: T.cmf,  types: 'ddddp', prefix: ['high', 'low', 'close', 'volume'], defaults: {} },
        vwap: { fn: T.vwap, types: 'ddddp', prefix: ['high', 'low', 'close', 'volume'], defaults: {} },

        // Bar reference
        highest: { fn: T.highest, types: 'dp', prefix: ['high'], defaults: {} },
        lowest:  { fn: T.lowest,  types: 'dp', prefix: ['low'], defaults: {} },
        median:  { fn: T.median,  types: 'dp', prefix: ['close'], defaults: {} },
        sum:     { fn: T.sum,     types: 'dp', prefix: ['close'], defaults: {} },
        change:  { fn: T.change,  types: 'dp', prefix: ['close'], defaults: { 1: 1 } },
        roc:     { fn: T.roc,     types: 'dp', prefix: ['close'], defaults: {} },
        mom:     { fn: T.mom,     types: 'dp', prefix: ['close'], defaults: { 1: 1 } },
        offset:  { fn: T.offset,  types: 'dp', prefix: ['close'], defaults: {} },

        // Logic / signals
        crossover:  { fn: T.crossover,  types: 'dd', prefix: [], defaults: {} },
        crossunder: { fn: T.crossunder, types: 'dd', prefix: [], defaults: {} },
        valuewhen:  { fn: T.valuewhen,  types: 'dd', prefix: [], defaults: {} },
        barssince:  { fn: T.barssince,  types: 'd',  prefix: [], defaults: {} },
        count:      { fn: T.count,      types: 'dp', prefix: [], defaults: {} },
        cum:        { fn: T.cum,        types: 'd',  prefix: ['close'], defaults: {} },
        streak:     { fn: T.streak,     types: 'd',  prefix: [], defaults: {} },

        // Structure
        highest_bars: { fn: T.highest_bars, types: 'dp',  prefix: ['high'], defaults: {} },
        lowest_bars:  { fn: T.lowest_bars,  types: 'dp',  prefix: ['low'],  defaults: {} },
        pivothigh:    { fn: T.pivothigh,    types: 'dpp', prefix: ['high'], defaults: { 1: 5, 2: 5 } },
        pivotlow:     { fn: T.pivotlow,     types: 'dpp', prefix: ['low'],  defaults: { 1: 5, 2: 5 } },

        // Math
        abs:     { fn: T.abs,     types: 'd',   prefix: [], defaults: {} },
        sqrt:    { fn: T.sqrt,    types: 'd',   prefix: [], defaults: {} },
        pow:     { fn: T.pow,     types: 'dd',  prefix: [], defaults: {} },
        log:     { fn: T.log,     types: 'd',   prefix: [], defaults: {} },
        exp:     { fn: T.exp,     types: 'd',   prefix: [], defaults: {} },
        round:   { fn: T.round,   types: 'dd',  prefix: [], defaults: {} },
        clamp:   { fn: T.clamp,   types: 'ddd', prefix: [], defaults: {} },
        max_val: { fn: T.max_val, types: 'dd',  prefix: [], defaults: {} },
        min_val: { fn: T.min_val, types: 'dd',  prefix: [], defaults: {} },

        // Quant: Normalization
        zscore:      { fn: T.zscore,      types: 'dp', prefix: ['close'], defaults: {} },
        percentile:  { fn: T.percentile,  types: 'dp', prefix: ['close'], defaults: {} },
        normalize:   { fn: T.normalize,   types: 'dp', prefix: ['close'], defaults: {} },

        // Quant: Correlation & Sensitivity
        corr: { fn: T.corr, types: 'ddp', prefix: [], defaults: {} },
        beta: { fn: T.beta, types: 'ddp', prefix: [], defaults: {} },

        // Quant: Linear Fit & Trend Quality
        linreg:       { fn: T.linreg,       types: 'dp', prefix: ['close'], defaults: {} },
        linreg_slope: { fn: T.linreg_slope, types: 'dp', prefix: ['close'], defaults: {} },
        r_squared:    { fn: T.r_squared,    types: 'dp', prefix: ['close'], defaults: {} },

        // Quant: Distribution & Volatility
        skew:         { fn: T.skew,         types: 'dp', prefix: ['close'], defaults: {} },
        realized_vol: { fn: T.realized_vol, types: 'dp', prefix: ['close'], defaults: {} },

        // Ichimoku
        tenkan:   { fn: T.tenkan,   types: 'ddp', prefix: ['high', 'low'], defaults: { 2: 9 } },
        kijun:    { fn: T.kijun,    types: 'ddp', prefix: ['high', 'low'], defaults: { 2: 26 } },
        senkou_a: { fn: T.senkou_a, types: 'dd',  prefix: ['high', 'low'], defaults: {} },
        senkou_b: { fn: T.senkou_b, types: 'ddp', prefix: ['high', 'low'], defaults: { 2: 52 } },
        chikou:   { fn: T.chikou,   types: 'd',   prefix: ['close'],       defaults: {} },

        // Alligator
        alligator_jaw:   { fn: T.alligator_jaw,   types: 'ddpp', prefix: ['high', 'low'], defaults: { 2: 13, 3: 8 } },
        alligator_teeth: { fn: T.alligator_teeth, types: 'ddpp', prefix: ['high', 'low'], defaults: { 2: 8, 3: 5 } },
        alligator_lips:  { fn: T.alligator_lips,  types: 'ddpp', prefix: ['high', 'low'], defaults: { 2: 5, 3: 3 } },

        // Stochastic RSI
        stoch_rsi_k: { fn: T.stoch_rsi_k, types: 'dpp',  prefix: ['close'], defaults: { 1: 14, 2: 3 } },
        stoch_rsi_d: { fn: T.stoch_rsi_d, types: 'dppp', prefix: ['close'], defaults: { 1: 14, 2: 3, 3: 3 } },

        // Donchian
        donchian_high: { fn: T.donchian_high, types: 'dp', prefix: ['high'], defaults: { 1: 20 } },
        donchian_low:  { fn: T.donchian_low,  types: 'dp', prefix: ['low'],  defaults: { 1: 20 } },

        // Detection
        nr7:            { fn: T.nr7,            types: 'dd',    prefix: ['high', 'low'],          defaults: {} },
        // SMC pattern primitives — mirror of carrac/formula_eval.py
        gap_up:         { fn: T.gap_up,         types: 'dddp',  prefix: ['high', 'low', 'close'], defaults: { 3: 0.25 } },
        gap_down:       { fn: T.gap_down,       types: 'dddp',  prefix: ['high', 'low', 'close'], defaults: { 3: 0.25 } },
        swept_low:      { fn: T.swept_low,      types: 'ddp',   prefix: ['low', 'close'],         defaults: { 2: 20 } },
        swept_high:     { fn: T.swept_high,     types: 'ddp',   prefix: ['high', 'close'],        defaults: { 2: 20 } },
        displacement:   { fn: T.displacement,   types: 'dddp',  prefix: ['high', 'low', 'close'], defaults: { 3: 2.0 } },
        reversal_close_up:   { fn: T.reversal_close_up,   types: 'dddp', prefix: ['open', 'close', 'high'], defaults: { 3: 2 } },
        reversal_close_down: { fn: T.reversal_close_down, types: 'dddp', prefix: ['open', 'close', 'low'],  defaults: { 3: 2 } },
        // SMC vocabulary aliases — same fns. 'order_block' is deliberately
        // absent; the chart draws real OB zones (smart-money-concepts.js).
        fvg_up:               { fn: T.gap_up,              types: 'dddp', prefix: ['high', 'low', 'close'],  defaults: { 3: 0.25 } },
        fvg_down:             { fn: T.gap_down,            types: 'dddp', prefix: ['high', 'low', 'close'],  defaults: { 3: 0.25 } },
        liquidity_sweep_low:  { fn: T.swept_low,           types: 'ddp',  prefix: ['low', 'close'],          defaults: { 2: 20 } },
        liquidity_sweep_high: { fn: T.swept_high,          types: 'ddp',  prefix: ['high', 'close'],         defaults: { 2: 20 } },
        csd_up:               { fn: T.reversal_close_up,   types: 'dddp', prefix: ['open', 'close', 'high'], defaults: { 3: 2 } },
        csd_down:             { fn: T.reversal_close_down, types: 'dddp', prefix: ['open', 'close', 'low'],  defaults: { 3: 2 } },
        atr_percentile: { fn: T.atr_percentile, types: 'dddpp', prefix: ['high', 'low', 'close'], defaults: { 3: 14, 4: 90 } },
        bbw_percentile: { fn: T.bbw_percentile, types: 'dppp',  prefix: ['close'],                defaults: { 1: 20, 2: 2, 3: 90 } },
        squeeze_on:     { fn: T.squeeze_on,     types: 'ddd',   prefix: ['high', 'low', 'close'], defaults: {} },

        // CPR (Central Pivot Range)
        cpr_pivot: { fn: T.cpr_pivot, types: 'ddd', prefix: ['high', 'low', 'close'], defaults: {} },
        cpr_r1:    { fn: T.cpr_r1,    types: 'ddd', prefix: ['high', 'low', 'close'], defaults: {} },
        cpr_r2:    { fn: T.cpr_r2,    types: 'ddd', prefix: ['high', 'low', 'close'], defaults: {} },
        cpr_r3:    { fn: T.cpr_r3,    types: 'ddd', prefix: ['high', 'low', 'close'], defaults: {} },
        cpr_s1:    { fn: T.cpr_s1,    types: 'ddd', prefix: ['high', 'low', 'close'], defaults: {} },
        cpr_s2:    { fn: T.cpr_s2,    types: 'ddd', prefix: ['high', 'low', 'close'], defaults: {} },
        cpr_s3:    { fn: T.cpr_s3,    types: 'ddd', prefix: ['high', 'low', 'close'], defaults: {} },

        // Composite State
        ema_ribbon_trend: { fn: T.ema_ribbon_trend, types: 'ddd', prefix: ['high', 'low', 'close'], defaults: {} },
        consensio:        { fn: T.consensio,        types: 'd',   prefix: ['close'],                defaults: {} },
        structure_trend:  { fn: T.structure_trend,  types: 'ddd', prefix: ['high', 'low', 'close'], defaults: {} }
    };

    // ── Tokenizer ───────────────────────────────────────────────────────

    function tokenize(formula) {
        // Strip comments
        formula = formula.replace(/\/\/.*$/gm, '').replace(/#.*$/gm, '');
        var tokens = [], i = 0, n = formula.length;

        while (i < n) {
            var ch = formula[i];
            if (/\s/.test(ch)) { i++; continue; }

            // Number: 123, 1.5, .5
            if (ch >= '0' && ch <= '9' || (ch === '.' && i + 1 < n && formula[i + 1] >= '0' && formula[i + 1] <= '9')) {
                var start = i;
                while (i < n && ((formula[i] >= '0' && formula[i] <= '9') || formula[i] === '.')) i++;
                var val = parseFloat(formula.substring(start, i));
                if (isNaN(val)) return { error: 'Invalid number at position ' + start };
                tokens.push({ type: 'NUM', value: val, pos: start });
                continue;
            }

            // Identifier or keyword
            if ((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || ch === '_') {
                var start = i;
                while (i < n && ((formula[i] >= 'a' && formula[i] <= 'z') || (formula[i] >= 'A' && formula[i] <= 'Z') ||
                       (formula[i] >= '0' && formula[i] <= '9') || formula[i] === '_')) i++;
                var name = formula.substring(start, i).toLowerCase();
                if (name === 'let') {
                    tokens.push({ type: 'LET', pos: start });
                } else if (name === 'and' || name === 'or' || name === 'not') {
                    tokens.push({ type: 'OP', value: name, pos: start });
                } else {
                    tokens.push({ type: 'ID', value: name, pos: start });
                }
                continue;
            }

            // Two-char operators
            if (i + 1 < n) {
                var two = formula[i] + formula[i + 1];
                if (two === '>=' || two === '<=' || two === '==' || two === '!=') {
                    tokens.push({ type: 'OP', value: two, pos: i }); i += 2; continue;
                }
            }

            // Single-char tokens
            if ('+-*/%><'.indexOf(ch) >= 0) { tokens.push({ type: 'OP', value: ch, pos: i }); i++; continue; }
            if (ch === '(') { tokens.push({ type: 'LP', pos: i }); i++; continue; }
            if (ch === ')') { tokens.push({ type: 'RP', pos: i }); i++; continue; }
            if (ch === ',') { tokens.push({ type: 'CM', pos: i }); i++; continue; }
            if (ch === '[') { tokens.push({ type: 'LB', pos: i }); i++; continue; }
            if (ch === ']') { tokens.push({ type: 'RB', pos: i }); i++; continue; }
            if (ch === '=') { tokens.push({ type: 'EQ', pos: i }); i++; continue; }

            // String literals — single or double quoted, no escape sequences
            if (ch === "'" || ch === '"') {
                var quote = ch;
                var start = i;
                i++;
                var sb = '';
                while (i < n && formula[i] !== quote) {
                    if (formula[i] === '\n') return { error: 'Unterminated string at position ' + start };
                    sb += formula[i]; i++;
                }
                if (i >= n) return { error: 'Unterminated string at position ' + start };
                i++; // consume closing quote
                tokens.push({ type: 'STR', value: sb, pos: start });
                continue;
            }

            return { error: 'Unexpected character "' + ch + '" at position ' + i };
        }

        tokens.push({ type: 'EOF', pos: i });
        return { tokens: tokens };
    }

    // ── Parser (Recursive Descent) ──────────────────────────────────────
    // Precedence (highest to lowest):
    //   0. Postfix [n] (bar offset — sugar for OFFSET)
    //   1. Unary -, NOT
    //   2. *, /, %
    //   3. +, -
    //   4. >, <, >=, <=, ==, !=
    //   5. AND, OR
    // Top-level: let bindings, then final expression

    function parse(tokens) {
        var pos = 0, depth = 0;
        function peek() { return tokens[pos]; }
        function advance() { return tokens[pos++]; }

        function program() {
            var lets = [];
            while (peek().type === 'LET') {
                if (lets.length >= 10) throw 'Too many let bindings (max 10)';
                advance();
                if (peek().type !== 'ID') throw 'Expected variable name after let';
                var name = advance().value;
                if (peek().type !== 'EQ') throw 'Expected = after variable name';
                advance();
                var value = expression();
                lets.push({ name: name, value: value });
            }
            var body = expression();
            if (lets.length === 0) return body;
            return { type: 'program', lets: lets, body: body };
        }

        function expression() {
            if (++depth > 20) throw 'Formula too deeply nested';
            var r = orExpr(); depth--; return r;
        }

        function orExpr() {
            var left = andExpr();
            while (peek().type === 'OP' && peek().value === 'or') {
                advance(); left = { type: 'bin', op: 'or', l: left, r: andExpr() };
            }
            return left;
        }

        function andExpr() {
            var left = comparison();
            while (peek().type === 'OP' && peek().value === 'and') {
                advance(); left = { type: 'bin', op: 'and', l: left, r: comparison() };
            }
            return left;
        }

        function comparison() {
            var left = additive();
            while (peek().type === 'OP' && (peek().value === '>' || peek().value === '<' ||
                   peek().value === '>=' || peek().value === '<=' || peek().value === '==' || peek().value === '!=')) {
                var op = advance().value;
                left = { type: 'bin', op: op, l: left, r: additive() };
            }
            return left;
        }

        function additive() {
            var left = multiplicative();
            while (peek().type === 'OP' && (peek().value === '+' || peek().value === '-')) {
                var op = advance().value;
                left = { type: 'bin', op: op, l: left, r: multiplicative() };
            }
            return left;
        }

        function multiplicative() {
            var left = unary();
            while (peek().type === 'OP' && (peek().value === '*' || peek().value === '/' || peek().value === '%')) {
                var op = advance().value;
                left = { type: 'bin', op: op, l: left, r: unary() };
            }
            return left;
        }

        function unary() {
            if (peek().type === 'OP' && peek().value === '-') {
                advance(); return { type: 'un', op: '-', x: unary() };
            }
            if (peek().type === 'OP' && peek().value === 'not') {
                advance(); return { type: 'un', op: 'not', x: unary() };
            }
            return postfix();
        }

        function postfix() {
            var node = primary();
            while (peek().type === 'LB') {
                advance();
                var neg = false;
                if (peek().type === 'OP' && peek().value === '-') {
                    neg = true;
                    advance();
                }
                if (peek().type !== 'NUM') throw 'Expected number in [offset]';
                var off = advance().value;
                if (neg) off = -off;
                if (off !== Math.floor(off)) throw 'Offset must be an integer';
                if (peek().type !== 'RB') throw 'Expected ] after offset';
                advance();
                node = { type: 'index', expr: node, offset: off };
            }
            return node;
        }

        function primary() {
            var t = peek();
            if (t.type === 'NUM') { advance(); return { type: 'num', v: t.value }; }
            if (t.type === 'STR') { advance(); return { type: 'str', v: t.value }; }
            // AND/OR/NOT can be function calls: AND(a, b), OR(a, b), NOT(a)
            // Don't advance here — the ID handler below does it
            if (t.type === 'OP' && (t.value === 'and' || t.value === 'or' || t.value === 'not') &&
                pos + 1 < tokens.length && tokens[pos + 1].type === 'LP') {
                t = { type: 'ID', value: t.value, pos: t.pos };
            }
            if (t.type === 'ID') {
                advance();
                if (peek().type === 'LP') {
                    advance();
                    var args = [];
                    if (peek().type !== 'RP') {
                        args.push(expression());
                        while (peek().type === 'CM') { advance(); args.push(expression()); }
                    }
                    if (peek().type !== 'RP') throw 'Expected ) at position ' + peek().pos;
                    advance();
                    return { type: 'call', name: t.value, args: args };
                }
                return { type: 'id', name: t.value };
            }
            if (t.type === 'LP') {
                advance(); var expr = expression();
                if (peek().type !== 'RP') throw 'Expected ) at position ' + peek().pos;
                advance(); return expr;
            }
            throw 'Unexpected token at position ' + t.pos;
        }

        var ast = program();
        if (peek().type !== 'EOF') throw 'Unexpected token at position ' + peek().pos;
        return ast;
    }

    // ── Evaluator ───────────────────────────────────────────────────────

    function evalFormula(ast, ohlcv) {
        var n = ohlcv.close.length;
        var callCount = 0, startTime = Date.now();
        var computed = {};
        var scope = {};

        function ident(name) {
            if (name === 'open') return ohlcv.open;
            if (name === 'high') return ohlcv.high;
            if (name === 'low') return ohlcv.low;
            if (name === 'close') return ohlcv.close;
            if (name === 'volume') return ohlcv.volume;
            if (computed[name]) return computed[name];
            if (name === 'ohlc4') return (computed.ohlc4 = T.ohlc4(ohlcv.open, ohlcv.high, ohlcv.low, ohlcv.close));
            if (name === 'hlc3') return (computed.hlc3 = T.hlc3(ohlcv.high, ohlcv.low, ohlcv.close));
            if (name === 'hl2') return (computed.hl2 = T.hl2(ohlcv.high, ohlcv.low));
            if (name === 'tr') return (computed.tr = T.tr(ohlcv.high, ohlcv.low, ohlcv.close));
            // Quant/external data injected into ohlcv context (e.g., m2, fear_greed, regime, btc, spx)
            if (ohlcv[name] && Array.isArray(ohlcv[name])) return ohlcv[name];
            return null;
        }

        function bcast(val) {
            var a = new Array(n);
            for (var i = 0; i < n; i++) a[i] = val;
            return a;
        }

        function toArr(r) {
            if (r.t === 's') return bcast(r.v);
            if (r.t === 'str') throw 'String literal can only be used as a function argument';
            return r.v;
        }

        // Scalar result: {t:'s', v:number}   Array result: {t:'a', v:number[]}   String: {t:'str', v:string}
        function ev(node) {
            if (Date.now() - startTime > 200) throw 'Formula took too long to evaluate';

            switch (node.type) {
                case 'num': return { t: 's', v: node.v };
                case 'str': return { t: 'str', v: node.v };

                case 'id': {
                    if (node.name in scope) return scope[node.name];
                    var arr = ident(node.name);
                    if (!arr) throw 'Unknown identifier: ' + node.name;
                    return { t: 'a', v: arr };
                }

                case 'un': {
                    var x = ev(node.x);
                    if (x.t === 's') {
                        if (node.op === '-') return { t: 's', v: -x.v };
                        return { t: 's', v: x.v ? 0 : 1 }; // not
                    }
                    var a = x.v, out = new Array(n);
                    if (node.op === '-') {
                        for (var i = 0; i < n; i++) out[i] = -a[i];
                    } else { // not
                        for (var i = 0; i < n; i++) out[i] = isNaN(a[i]) ? NaN : (a[i] !== 0 ? 0 : 1);
                    }
                    return { t: 'a', v: out };
                }

                case 'bin': return evBin(node);
                case 'call': return evCall(node);

                case 'program': {
                    for (var i = 0; i < node.lets.length; i++) {
                        scope[node.lets[i].name] = ev(node.lets[i].value);
                    }
                    return ev(node.body);
                }

                case 'index': {
                    var x = ev(node.expr);
                    var arr = toArr(x);
                    return { t: 'a', v: T.offset(arr, node.offset) };
                }
            }
        }

        function evBin(node) {
            var L = ev(node.l), R = ev(node.r), op = node.op;

            // Scalar fast path
            if (L.t === 's' && R.t === 's') {
                var a = L.v, b = R.v, r;
                switch (op) {
                    case '+': r = a + b; break; case '-': r = a - b; break;
                    case '*': r = a * b; break; case '/': r = a / b; break;
                    case '%': r = a % b; break;
                    case '>': r = a > b ? 1 : 0; break; case '<': r = a < b ? 1 : 0; break;
                    case '>=': r = a >= b ? 1 : 0; break; case '<=': r = a <= b ? 1 : 0; break;
                    case '==': r = a === b ? 1 : 0; break; case '!=': r = a !== b ? 1 : 0; break;
                    case 'and': r = (a !== 0 && b !== 0) ? 1 : 0; break;
                    case 'or': r = (a !== 0 || b !== 0) ? 1 : 0; break;
                }
                return { t: 's', v: r };
            }

            var a = toArr(L), b = toArr(R), out = new Array(n);
            switch (op) {
                case '+': for (var i = 0; i < n; i++) out[i] = a[i] + b[i]; break;
                case '-': for (var i = 0; i < n; i++) out[i] = a[i] - b[i]; break;
                case '*': for (var i = 0; i < n; i++) out[i] = a[i] * b[i]; break;
                case '/': for (var i = 0; i < n; i++) out[i] = a[i] / b[i]; break;
                case '%': for (var i = 0; i < n; i++) out[i] = a[i] % b[i]; break;
                case '>': for (var i = 0; i < n; i++) out[i] = (isNaN(a[i]) || isNaN(b[i])) ? NaN : (a[i] > b[i] ? 1 : 0); break;
                case '<': for (var i = 0; i < n; i++) out[i] = (isNaN(a[i]) || isNaN(b[i])) ? NaN : (a[i] < b[i] ? 1 : 0); break;
                case '>=': for (var i = 0; i < n; i++) out[i] = (isNaN(a[i]) || isNaN(b[i])) ? NaN : (a[i] >= b[i] ? 1 : 0); break;
                case '<=': for (var i = 0; i < n; i++) out[i] = (isNaN(a[i]) || isNaN(b[i])) ? NaN : (a[i] <= b[i] ? 1 : 0); break;
                case '==': for (var i = 0; i < n; i++) out[i] = (isNaN(a[i]) || isNaN(b[i])) ? NaN : (a[i] === b[i] ? 1 : 0); break;
                case '!=': for (var i = 0; i < n; i++) out[i] = (isNaN(a[i]) || isNaN(b[i])) ? NaN : (a[i] !== b[i] ? 1 : 0); break;
                case 'and': for (var i = 0; i < n; i++) out[i] = (isNaN(a[i]) || isNaN(b[i])) ? NaN : ((a[i] !== 0 && b[i] !== 0) ? 1 : 0); break;
                case 'or': for (var i = 0; i < n; i++) out[i] = (isNaN(a[i]) || isNaN(b[i])) ? NaN : ((a[i] !== 0 || b[i] !== 0) ? 1 : 0); break;
            }
            return { t: 'a', v: out };
        }

        function evCall(node) {
            var name = node.name, nArgs = node.args.length;

            // ── Special functions ───────────────────────────────────────
            if (name === 'if') {
                if (nArgs !== 3) throw 'IF expects 3 arguments, got ' + nArgs;
                var cond = toArr(ev(node.args[0]));
                var then_ = toArr(ev(node.args[1]));
                var else_ = toArr(ev(node.args[2]));
                var out = new Array(n);
                for (var i = 0; i < n; i++)
                    out[i] = (!isNaN(cond[i]) && cond[i] !== 0) ? then_[i] : else_[i];
                return { t: 'a', v: out };
            }
            if (name === 'and') {
                if (nArgs !== 2) throw 'AND expects 2 arguments, got ' + nArgs;
                var a = toArr(ev(node.args[0])), b = toArr(ev(node.args[1])), out = new Array(n);
                for (var i = 0; i < n; i++) out[i] = (isNaN(a[i]) || isNaN(b[i])) ? NaN : ((a[i] !== 0 && b[i] !== 0) ? 1 : 0);
                return { t: 'a', v: out };
            }
            if (name === 'or') {
                if (nArgs !== 2) throw 'OR expects 2 arguments, got ' + nArgs;
                var a = toArr(ev(node.args[0])), b = toArr(ev(node.args[1])), out = new Array(n);
                for (var i = 0; i < n; i++) out[i] = (isNaN(a[i]) || isNaN(b[i])) ? NaN : ((a[i] !== 0 || b[i] !== 0) ? 1 : 0);
                return { t: 'a', v: out };
            }
            if (name === 'not') {
                if (nArgs !== 1) throw 'NOT expects 1 argument, got ' + nArgs;
                var a = toArr(ev(node.args[0])), out = new Array(n);
                for (var i = 0; i < n; i++) out[i] = isNaN(a[i]) ? NaN : (a[i] !== 0 ? 0 : 1);
                return { t: 'a', v: out };
            }

            // HTF('1W', sub_expr) — server-authoritative; client renders NaN if the
            // preview endpoint didn't inject the alignment array.
            if (name === 'htf') {
                if (nArgs !== 2) throw 'HTF expects 2 arguments: HTF(target_tf, expression)';
                var tfRes = ev(node.args[0]);
                if (tfRes.t !== 'str') throw "HTF arg 1 must be a timeframe string literal, e.g., '1W'";
                var target = tfRes.v;
                // Look up pre-aligned array injected by server preview response.
                // Key pattern: __htf__[target_tf][sub_expr_key]. For client-side use, the
                // feature is server-rendered; if absent, return NaN array (graceful).
                var htfBundle = ohlcv.__htf__ && ohlcv.__htf__[target];
                if (!htfBundle) { var o = new Array(n); for (var k = 0; k < n; k++) o[k] = NaN; return { t: 'a', v: o }; }
                // Without a sub-expr cache-key strategy, support only `close`/`open`/`high`/`low`/`volume` on the HTF.
                // For richer sub-expressions, rely on server-side eval.
                var subNode = node.args[1];
                if (subNode.type === 'id' && htfBundle[subNode.name]) {
                    return { t: 'a', v: htfBundle[subNode.name] };
                }
                var o = new Array(n); for (var k = 0; k < n; k++) o[k] = NaN; return { t: 'a', v: o };
            }

            // DIVERGENCE(price, osc, lookback=60)
            if (name === 'divergence') {
                if (nArgs < 2 || nArgs > 3) throw 'DIVERGENCE expects (price, osc) or (price, osc, lookback)';
                var price = toArr(ev(node.args[0]));
                var osc = toArr(ev(node.args[1]));
                var lookback = 60;
                if (nArgs === 3) {
                    var lb = ev(node.args[2]);
                    if (lb.t !== 's') throw 'DIVERGENCE lookback must be a number';
                    lookback = Math.floor(lb.v);
                }
                if (lookback < 20) throw 'DIVERGENCE lookback must be >= 20';
                if (price.length !== osc.length) throw 'DIVERGENCE: price and osc arrays must be same length';

                var left = 5, right = 5;
                var phP = T.pivothigh(price, left, right);
                var plP = T.pivotlow(price, left, right);
                var out = new Array(n);
                for (var i = 0; i < n; i++) out[i] = 0;
                var priceHighs = [];
                var priceLows = [];
                for (var i = 0; i < n; i++) {
                    if (!isNaN(phP[i])) {
                        var pb = i - right;
                        priceHighs.push({ idx: pb, p: price[pb], o: osc[pb] });
                        if (priceHighs.length >= 2) {
                            var cur = priceHighs[priceHighs.length - 1];
                            var prev = priceHighs[priceHighs.length - 2];
                            if ((cur.idx - prev.idx) <= lookback && !isNaN(cur.o) && !isNaN(prev.o)
                                && cur.p > prev.p && cur.o < prev.o) {
                                out[i] = 1;
                            }
                        }
                    }
                    if (!isNaN(plP[i])) {
                        var pb = i - right;
                        priceLows.push({ idx: pb, p: price[pb], o: osc[pb] });
                        if (priceLows.length >= 2) {
                            var cur = priceLows[priceLows.length - 1];
                            var prev = priceLows[priceLows.length - 2];
                            if ((cur.idx - prev.idx) <= lookback && !isNaN(cur.o) && !isNaN(prev.o)
                                && cur.p < prev.p && cur.o > prev.o) {
                                out[i] = -1;
                            }
                        }
                    }
                }
                var warmup = Math.min(n, left + right + 20);
                for (var i = 0; i < warmup; i++) out[i] = NaN;
                return { t: 'a', v: out };
            }

            // ── TALib functions ─────────────────────────────────────────
            var def = FUNCTIONS[name];
            if (!def) throw 'Unknown function: ' + name.toUpperCase();
            if (++callCount > 50) throw 'Too many function calls (max 50)';

            // Evaluate user args
            var userArgs = [];
            for (var i = 0; i < nArgs; i++) userArgs.push(ev(node.args[i]));

            var resolved = resolveArgs(name, def, userArgs);
            return { t: 'a', v: def.fn.apply(null, resolved) };
        }

        // ── Argument Resolution ─────────────────────────────────────────
        // Handles implicit OHLCV prefix, optional trailing defaults, and type conversion.

        function resolveArgs(name, def, userArgs) {
            var types = def.types;
            var total = types.length;
            var prefix = def.prefix;
            var defaults = def.defaults;
            var numDefaults = Object.keys(defaults).length;
            var userN = userArgs.length;

            // Determine min/max user args for error messages
            var minUser = total - prefix.length - numDefaults;
            var maxUser = total;

            if (userN > maxUser) {
                throw name.toUpperCase() + ' expects at most ' + maxUser + ' arguments, got ' + userN;
            }

            // Heuristic: if first user arg is a scalar number and prefix available,
            // user is using shorthand (e.g., SMA(20) instead of SMA(close, 20))
            var needPrefix = prefix.length > 0 && userN < total &&
                             (userN === 0 || userArgs[0].t === 's');

            var args = [];

            // 1. Prepend OHLCV prefix if needed
            if (needPrefix) {
                for (var i = 0; i < prefix.length; i++) {
                    var arr = ident(prefix[i]);
                    if (!arr) throw 'Internal error: unknown prefix identifier ' + prefix[i];
                    args.push(arr);
                }
            }

            // 2. Add user args with type conversion
            for (var i = 0; i < userN; i++) {
                var pos = args.length;
                if (pos >= total) {
                    throw name.toUpperCase() + ' expects ' + minUser + ' to ' + maxUser + ' arguments';
                }
                if (types[pos] === 'd') {
                    args.push(toArr(userArgs[i]));
                } else { // 'p' — must be scalar
                    if (userArgs[i].t !== 's') {
                        throw name.toUpperCase() + ': argument ' + (i + 1) + ' must be a number';
                    }
                    args.push(userArgs[i].v);
                }
            }

            // 3. Fill trailing defaults
            while (args.length < total) {
                var pos = args.length;
                if (pos in defaults) {
                    args.push(defaults[pos]);
                } else {
                    throw name.toUpperCase() + ' expects at least ' +
                          (needPrefix ? minUser : total - numDefaults) + ' argument(s), got ' + userN;
                }
            }

            return args;
        }

        // ── Run ─────────────────────────────────────────────────────────
        var result = ev(ast);
        var values = toArr(result);

        // Output validation: Infinity → NaN
        for (var i = 0; i < values.length; i++) {
            if (!isFinite(values[i])) values[i] = NaN;
        }
        return values;
    }

    // ── Public API ──────────────────────────────────────────────────────

    window.FormulaParser = {
        /**
         * Evaluate a formula string against OHLCV data.
         * @param {string} formulaString - e.g., "EMA(close, 9) - EMA(close, 21)"
         * @param {Object} ohlcv - { open:[], high:[], low:[], close:[], volume:[] }
         * @returns {{ values: number[], error: string|null }}
         */
        evaluate: function(formulaString, ohlcv) {
            try {
                if (!formulaString || typeof formulaString !== 'string') {
                    return { values: null, error: 'Formula is empty' };
                }
                if (formulaString.length > 500) {
                    return { values: null, error: 'Formula exceeds 500 character limit' };
                }

                var tok = tokenize(formulaString);
                if (tok.error) return { values: null, error: tok.error };

                var ast = parse(tok.tokens);
                var values = evalFormula(ast, ohlcv);

                return { values: values, error: null };
            } catch (e) {
                return { values: null, error: typeof e === 'string' ? e : e.message || 'Unknown error' };
            }
        },

        /**
         * Parse-only (for validation without OHLCV data).
         * @returns {{ ast: Object, error: string|null }}
         */
        parse: function(formulaString) {
            try {
                if (!formulaString || typeof formulaString !== 'string') {
                    return { ast: null, error: 'Formula is empty' };
                }
                if (formulaString.length > 500) {
                    return { ast: null, error: 'Formula exceeds 500 character limit' };
                }
                var tok = tokenize(formulaString);
                if (tok.error) return { ast: null, error: tok.error };
                var ast = parse(tok.tokens);
                return { ast: ast, error: null };
            } catch (e) {
                return { ast: null, error: typeof e === 'string' ? e : e.message || 'Unknown error' };
            }
        },

        /**
         * Quant variable catalog — maps formula identifiers to bridge series names.
         * Caller uses this to detect which quant series a formula needs.
         */
        QUANT_VARS: {
            // Macro overlays
            m2: 'M2', net_liquidity: 'NET_LIQUIDITY', dxy: 'DXY',
            stablecoin: 'STABLECOIN_USDT_SUPPLY',
            // Sentiment
            fear_greed: 'FEAR_GREED', alt_season: 'ALT_SEASON',
            // BTC-reference series (ETF net flow tracks BTC only)
            etf_flow: 'ETF_FLOW',
            // Macro panes
            vix: 'VIX', hy_spread: 'HY_SPREAD', yield_curve: 'YIELD_CURVE',
            // Regime
            regime: 'REGIME',
            // Cross-asset prices
            btc: 'BTC', eth: 'ETH', sol: 'SOL', spx: 'SPX', gold: 'GOLD_F',
            // PCA composite factors (match Python)
            risk_appetite: 'RISK_APPETITE', liquidity_factor: 'LIQUIDITY_FACTOR',
            credit_stress_factor: 'CREDIT_STRESS_FACTOR',
            // Transmission chain strengths
            chain_monetary_policy: 'CHAIN_MONETARY_POLICY',
            chain_dollar_liquidity: 'CHAIN_DOLLAR_LIQUIDITY',
            chain_risk_appetite: 'CHAIN_RISK_APPETITE'
            // funding/dvol moved to DERIV_VARS (per-symbol);
            // scanner/consensio/ribbon/hurst are not offered — no writer feeds those
            // series, so the vars would never resolve.
        },

        /** Per-asset derivatives vars — resolved for the formula's current symbol. */
        DERIV_VARS: {
            basis_apr: 'basis_pct',
            skew_25d: 'skew_25d',
            iv_7d: 'iv_7d',
            iv_30d: 'iv_30d',
            iv_90d: 'iv_90d',
            term_slope: 'term_structure_slope',
            gex_net: 'gex_net',
            gex_flip_price: 'gex_flip_price',
            iv_rv_premium: 'iv_rv_premium',
            // funding_oi_ratio removed — mapped to a column that
            // has never existed, so every formula using it errored.
            // funding and dvol are per-symbol series
            funding: 'funding_rate_avg',
            dvol: 'dvol'
        },

        ENGINE_VERSION: 3,

        /**
         * Scan a formula string for quant variable references.
         * Returns array of bridge series names (e.g., ['M2', 'FEAR_GREED']).
         */
        getQuantDeps: function(formulaString) {
            if (!formulaString) return [];
            var tok = tokenize(formulaString);
            if (tok.error) return [];
            var deps = [];
            var seen = {};
            var QV = this.QUANT_VARS;
            for (var i = 0; i < tok.tokens.length; i++) {
                var t = tok.tokens[i];
                if (t.type === 'ID' && QV[t.value] && !seen[QV[t.value]]) {
                    seen[QV[t.value]] = true;
                    deps.push(QV[t.value]);
                }
            }
            return deps;
        },

        /**
         * True if the formula references features only the server can resolve
         * (HTF, per-asset derivatives vars). Caller should route to /api/formula/preview.
         */
        needsServerEval: function(formulaString) {
            if (!formulaString) return false;
            var tok = tokenize(formulaString);
            if (tok.error) return false;
            var DV = this.DERIV_VARS;
            for (var i = 0; i < tok.tokens.length; i++) {
                var t = tok.tokens[i];
                if (t.type === 'ID') {
                    if (t.value === 'htf') return true;
                    if (DV[t.value]) return true;
                }
            }
            return false;
        },

        /**
         * Server-side evaluation via /api/formula/preview — returns a Promise resolving
         * to {values: number[] | null, error: string | null}. Used for HTF and per-asset
         * derivatives vars which require DB access. Results are locally cached per
         * (symbol, timeframe, expression) for 60s.
         */
        _previewCache: {},
        evaluateServerSide: function(formulaString, symbol, timeframe) {
            if (!formulaString || !symbol) {
                return Promise.resolve({ values: null, error: 'Missing formula or symbol' });
            }
            var tf = timeframe || '1D';
            var key = symbol + '|' + tf + '|' + formulaString;
            var nowBucket = Math.floor(Date.now() / 60000);
            var cached = this._previewCache[key];
            if (cached && cached.bucket === nowBucket) {
                return Promise.resolve(cached.result);
            }
            var url = '/api/formula/preview?symbol=' + encodeURIComponent(symbol)
                    + '&tf=' + encodeURIComponent(tf)
                    + '&expression=' + encodeURIComponent(formulaString);
            var self = this;
            return fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
                .then(function(r) {
                    if (!r.ok) return r.json().then(function(j) { return { values: null, error: j.error || ('HTTP ' + r.status) }; });
                    return r.json();
                })
                .then(function(data) {
                    var result = (data && data.error)
                        ? { values: null, error: data.error }
                        : { values: (data && data.values) || null, error: null };
                    self._previewCache[key] = { bucket: nowBucket, result: result };
                    return result;
                })
                .catch(function(e) {
                    return { values: null, error: String(e) };
                });
        }
    };
})();
