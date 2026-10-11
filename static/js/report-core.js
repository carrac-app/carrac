/**
 * Report Core — Shared parsing utilities for morning report.
 * Used by both standalone morning_report.html and inline panel in split-layout.js.
 */
var ReportCore = (function() {
    'use strict';

    function stripSectionNumber(s) {
        return s.replace(/^\d+[a-z]?\.\s+/i, '');
    }

    // Pages load shared-utils.js before this file; plain Node (tests) requires it.
    var escapeHtml = typeof globalThis.escapeHtml === 'function' ? globalThis.escapeHtml : require('./shared-utils.js').escapeHtml;

    function colorizeAnalysis(html) {
        return html.replace(/(<[^>]*>)|([^<]+)/g, function(m, tag, text) {
            if (tag) return tag;
            text = text.replace(/\b(bullish|support|oversold|accumulation|bounce|recovery|undervalued)\b/gi, '<span class="clr-bull">$1</span>');
            text = text.replace(/\b(bearish|resistance|overbought|breakdown|crash|risk-off|overvalued|capitulation)\b/gi, '<span class="clr-bear">$1</span>');
            text = text.replace(/(\+\d+(?:\.\d+)?%)/g, '<span class="clr-pct-up">$1</span>');
            text = text.replace(/(-\d+(?:\.\d+)?%)/g, '<span class="clr-pct-dn">$1</span>');
            return text;
        });
    }

    function resolveRelativeTime(metaStr, reportCreatedAt) {
        var refTime = reportCreatedAt || window._reportCreatedAt;
        if (!refTime || !metaStr) return metaStr;
        var reportTime = refTime.getTime();
        return metaStr.replace(/(\d+)(h|m|d)\s*ago/gi, function(match, num, unit) {
            var ms = parseInt(num) * (unit === 'h' ? 3600000 : unit === 'd' ? 86400000 : 60000);
            var absDate = new Date(reportTime - ms);
            var mon = absDate.toLocaleString('en-US', { month: 'short' });
            var day = absDate.getDate();
            var hh = absDate.getUTCHours().toString().padStart(2, '0');
            var mm = absDate.getUTCMinutes().toString().padStart(2, '0');
            return mon + ' ' + day + ', ' + hh + ':' + mm + ' UTC';
        });
    }

    function simpleMarkdownToHtml(md) {
        var html = escapeHtml(md)
            .replace(/^## (.+)$/gm, function(_, t) { return '<h2>' + t.replace(/^\d+[a-z]?\.\s+/i, '') + '</h2>'; })
            .replace(/^### (.+)$/gm, function(_, t) { return '<h3>' + t.replace(/^\d+[a-z]?\.\s+/i, '') + '</h3>'; })
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            .replace(/\*(.+?)\*/g, '<em>$1</em>')
            .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, function(m, alt, url) {
                if (/^(javascript|vbscript|data:text)/i.test(url.trim())) return alt;
                return '<img src="' + url + '" alt="' + alt + '" class="xfeed-img" loading="lazy">';
            })
            .replace(/\[([^\]]+)\]\(([^)]+)\)/g, function(m, text, url) {
                if (/^(javascript|vbscript|data:text)/i.test(url.trim())) return text;
                return '<a href="' + url + '" target="_blank" rel="noopener" style="color:var(--color-accent,#4dd0e1)">' + text + '</a>';
            })
            .replace(/`([^`]+)`/g, '<code class="xfeed-tag">$1</code>')
            .replace(/^&gt; (.+)$/gm, '<blockquote style="border-left:3px solid var(--border-default,#30363d);padding-left:10px;margin:6px 0;color:var(--text-tertiary,#6e7681);font-style:italic">$1</blockquote>')
            .replace(/^---$/gm, '<hr style="border:none;border-top:1px solid var(--border-subtle,#21262d);margin:12px 0">')
            .replace(/^\s*- (.+)$/gm, '<li>$1</li>')
            .replace(/(<li>[\s\S]*?<\/li>[\s]*)+/g, function(m) {
                return '<ul>' + m.replace(/<\/li>\s*<li>/g, '</li><li>') + '</ul>';
            })
            .replace(/^\s*\d+\.\s+(.+)$/gm, '<oli>$1</oli>')
            .replace(/(<oli>[\s\S]*?<\/oli>[\s]*)+/g, function(m) {
                return '<ol>' + m.replace(/<\/?oli>/g, function(t) { return t.replace('oli', 'li'); }) + '</ol>';
            })
            .replace(/(^\|.+\|(?:\n\|.+\|)*)/gm, function(block) {
                var rows = block.trim().split('\n');
                var tbl = '<table>';
                rows.forEach(function(row, i) {
                    if (/^\|[\s\-:|]+\|$/.test(row)) return;
                    var cells = row.split('|').filter(function(c, j, a) { return j > 0 && j < a.length - 1; });
                    var tag = (i === 0) ? 'th' : 'td';
                    tbl += '<tr>' + cells.map(function(c) { return '<' + tag + '>' + c.trim() + '</' + tag + '>'; }).join('') + '</tr>';
                });
                tbl += '</table>';
                return tbl;
            });
        html = html.replace(/\n\n+/g, '</p><p>').replace(/\n/g, '<br>');
        html = html.replace(/<\/li>\s*<br>\s*<li>/g, '</li><li>');
        html = html.replace(/<ul>\s*<br>/g, '<ul>').replace(/<br>\s*<\/ul>/g, '</ul>');
        html = html.replace(/<ol>\s*<br>/g, '<ol>').replace(/<br>\s*<\/ol>/g, '</ol>');
        html = html.replace(/<br>\s*<(h2|h3|ul|ol|table|img|hr|blockquote)/g, '<$1');
        html = html.replace(/<\/(h2|h3|ul|ol|table|blockquote)>\s*<br>/g, '</$1>');
        html = html.replace(/(<img [^>]+>)\s*<br>/g, '$1');
        html = html.replace(/(<hr [^>]*>)\s*<br>/g, '$1');
        if (html && !html.match(/^<(h2|h3|ul|ol|table|img|p)/)) html = '<p>' + html;
        if (html && !html.match(/<\/(h2|h3|ul|ol|table|p)>$/)) html += '</p>';
        html = html.replace(/<p>\s*<\/p>/g, '').replace(/<p>\s*<br>\s*<\/p>/g, '');
        html = html.replace(/<ul>([\s\S]*?)<\/ul>/g, function(m) { return m.replace(/<\/?p>/g, ''); });
        html = html.replace(/<ol>([\s\S]*?)<\/ol>/g, function(m) { return m.replace(/<\/?p>/g, ''); });
        html = html.replace(/&lt;details data-full-summary&gt;/g, '<details data-full-summary>');
        html = html.replace(/&lt;\/details&gt;/g, '</details>');
        html = html.replace(/&lt;summary&gt;/g, '<summary>');
        html = html.replace(/&lt;\/summary&gt;/g, '</summary>');
        html = colorizeAnalysis(html);
        if (typeof DOMPurify !== 'undefined') {
            html = DOMPurify.sanitize(html, {
                ADD_TAGS: ['details', 'summary'],
                ADD_ATTR: ['data-full-summary', 'target', 'rel']
            });
        } else {
            // DOMPurify unavailable — strip the whitelisted structural tags back to escaped
            // form so no raw HTML from LLM output can execute. The pre-escaped base (from
            // escapeHtml at the top of simpleMarkdownToHtml) is safe; only the four
            // intentionally-reintroduced structural tags need to be re-escaped here.
            html = html
                .replace(/<details data-full-summary>/g, '&lt;details data-full-summary&gt;')
                .replace(/<\/details>/g, '&lt;/details&gt;')
                .replace(/<summary>/g, '&lt;summary&gt;')
                .replace(/<\/summary>/g, '&lt;/summary&gt;');
        }
        return html;
    }

    /* Canonical tab definitions — single source of truth for both sidebar and full-page.
     * Includes BOTH old and new section names for backward compatibility:
     * old reports (17-section) and new reports (8-section narrative) both render. */
    var REPORT_TABS = [
        { id: 'overview', label: 'Overview', sections: ['audioBriefing', 'story', 'sinceYesterday', 'actionDashboard', 'scenarios', 'overview', 'actionItems', 'tradeDesk', 'macro', 'calendar', 'catalysts', 'foresight', 'invalidation'] },
        { id: 'analysis', label: 'Analysis', sections: ['regimePositioning', 'assetSpotlight', 'marketSnapshot', 'quantEngine', 'marketStructure', 'bullish', 'htfOverview', 'ratioIntermarket', 'commodityRotation', 'confluenceZones', 'assets', 'correlation'] },
        { id: 'data', label: 'Data', sections: ['onchain', 'options'] },
        { id: 'personal', label: 'Personal', sections: ['personal'], requiresPersonal: true },
    ];

    function parseReportSections(md, opts) {
        opts = opts || {};
        var result = {
            assets: {}, macro: '', correlation: '', actionItems: '', calendar: '',
            overview: '', quantEngine: '', marketStructure: '',
            bullish: '', social: '', whale: '', catalysts: '', xfeedRaw: '',
            youtube: '', heatmap: '', options: '', onchain: '',
            tradeDesk: '', htfOverview: '', ratioIntermarket: '',
            commodityRotation: '', confluenceZones: '', foresight: '', invalidation: '',
            /* Narrative sections */
            story: '', sinceYesterday: '', actionDashboard: '', scenarios: '',
            regimePositioning: '', assetSpotlight: '', marketSnapshot: '',
            oneThingToLearn: '',
            hasPersonalSplit: false,
            personal: { summary: '', devils: '', plays: '', youtube: '', xfeedRaw: '',
                        alertsFollowup: '', comparison: '', patternStatus: '', actionItems: '' }
        };
        var symbols = ['BTC', 'SPX', 'QQQ', 'OIL', 'GOLD', 'SILVER', 'ETH', 'XRP', 'TOTAL',
            'MAGS', 'COPPER', 'NATGAS', 'XLE', 'CORN', 'WHEAT', 'SOYBEANS',
            'BTC_SPX', 'GOLD_BTC', 'SPX_GOLD', 'ETH_BTC', 'XRP_BTC', 'QQQ_SPX', 'GOLD_QQQ', 'MAGS_SPX', 'DXY'];

        var generalMd = md, personalMd = '';
        var markerIdx = md.indexOf('<!-- PERSONAL_SECTION_START -->');
        if (markerIdx !== -1) {
            result.hasPersonalSplit = true;
            generalMd = md.substring(0, markerIdx);
            personalMd = md.substring(markerIdx + '<!-- PERSONAL_SECTION_START -->'.length);
            // Credits exhausted marker
            if (personalMd.indexOf('CREDITS_EXHAUSTED') !== -1) {
                result.creditsExhausted = true;
                personalMd = '';
            }
        }

        var parts = generalMd.split(/(?=^## )/m);
        parts.forEach(function(part) {
            var headerMatch = part.match(/^## (.+)/);
            if (!headerMatch) return;
            var rawHeader = stripSectionNumber(headerMatch[1].trim());
            var header = rawHeader.toLowerCase();
            var body = part.replace(/^## .+\n/, '');

            /* === Narrative sections === */
            if (header.includes('the story') || header === 'story') {
                result.story = simpleMarkdownToHtml(body); return;
            }
            if (header.includes('since yesterday') || header === 'since yesterday') {
                result.sinceYesterday = simpleMarkdownToHtml(body); return;
            }
            if (header.includes('action dashboard')) {
                result.actionDashboard = simpleMarkdownToHtml(body); return;
            }
            if (header.includes('scenario') || header.includes('what would change the picture')) {
                /* The operator/personalized reports emit "## Scenarios"; the
                   free-tier general report's reframed observational section
                   "## What Would Change the Picture" shares this
                   same render slot so both keep rendering. */
                result.scenarios = simpleMarkdownToHtml(body);
                return;
            }
            if (header.includes('regime') && header.includes('positioning')) {
                result.regimePositioning = simpleMarkdownToHtml(body); return;
            }
            /* === General report (free-tier) sections === */
            /* TL;DR → overview card (Legacy Overview container in Overview tab) */
            if (header === 'tl;dr' || header === 'tl dr' || header.startsWith('tl;dr') || header.startsWith('tl dr')) {
                result.overview = simpleMarkdownToHtml(body); return;
            }
            /* "What the Data Says" → regimePositioning slot (Analysis tab) */
            if (header.includes('what the data') || header.includes('data says')) {
                result.regimePositioning = simpleMarkdownToHtml(body); return;
            }
            /* "One Thing to Learn Today" → catalysts slot (Overview tab) + oneThingToLearn for pulse */
            if (header.includes('one thing to learn') || header.includes('learn today') || header.includes('thing to learn')) {
                var _learnHtml = simpleMarkdownToHtml(body);
                result.catalysts = _learnHtml;
                result.oneThingToLearn = _learnHtml;
                return;
            }
            if (header.includes('asset spotlight')) {
                result.assetSpotlight = simpleMarkdownToHtml(body);
                /* Also parse ### SYMBOL sub-headers into assets{} for card rendering */
                var subParts = part.split(/(?=^### )/m);
                subParts.forEach(function(sub) {
                    var subMatch = sub.match(/^### (.+)/);
                    if (!subMatch) return;
                    var subHeader = subMatch[1].trim();
                    symbols.forEach(function(sym) {
                        var normalizedHeader = subHeader.toUpperCase().replace(/\//g, '_');
                        if (normalizedHeader.includes(sym) || subHeader.toUpperCase().includes(sym)) {
                            result.assets[sym] = (result.assets[sym] || '') + simpleMarkdownToHtml(sub);
                        }
                    });
                });
                return;
            }
            if (header.includes('market snapshot')) {
                result.marketSnapshot = simpleMarkdownToHtml(body); return;
            }
            /* === Personal sections inline in personalized reports === */
            if (header.includes('your portfolio') || header.includes('portfolio summary')) {
                result.hasPersonalSplit = true;
                result.personal.summary = simpleMarkdownToHtml(body); return;
            }
            if (header.includes('devil') || header.includes('counter-thesis')) {
                result.hasPersonalSplit = true;
                result.personal.devils = simpleMarkdownToHtml(body); return;
            }
            if (header.includes('play evaluation') || header.includes('play status')) {
                result.hasPersonalSplit = true;
                result.personal.plays = simpleMarkdownToHtml(body); return;
            }
            if (header.includes('pattern status')) {
                result.hasPersonalSplit = true;
                result.personal.patternStatus = simpleMarkdownToHtml(body); return;
            }
            /* === Legacy section matchers (backward compat) === */
            if (header.includes('macro context') || header.includes('macro event') || header.includes('overnight')) {
                var macroSubs = body.split(/(?=^### )/m);
                var macroRemainder = [];
                macroSubs.forEach(function(sub) {
                    var sm = sub.match(/^### (.+)/);
                    if (!sm) { macroRemainder.push(sub); return; }
                    var sh = sm[1].trim().toLowerCase();
                    if (sh.includes('calendar') || sh.includes('economic')) {
                        result.calendar = simpleMarkdownToHtml(sub);
                    } else if (sh.includes('youtube') || sh.includes('analyst insight')) {
                        result.youtube = simpleMarkdownToHtml(sub);
                    } else if (sh.includes('social') || sh.includes('traders are saying')) {
                        result.social = simpleMarkdownToHtml(sub);
                    } else if (sh.includes('whale') || sh.includes('institutional') || sh.includes('etf flow')) {
                        result.whale = simpleMarkdownToHtml(sub);
                    } else if (sh.includes('catalyst') || sh.includes('derivative') || sh.includes('upcoming')) {
                        result.catalysts = simpleMarkdownToHtml(sub);
                    } else if (sh.includes('x feed') || sh.includes('twitter')) {
                        result.xfeedRaw = sub.replace(/^### .+\n/, '');
                    } else if (sh.includes('liquidation') || sh.includes('heatmap')) {
                        result.heatmap = simpleMarkdownToHtml(sub);
                    } else if (sh.includes('on-chain') || sh.includes('onchain') || sh.includes('on chain')) {
                        result.onchain = simpleMarkdownToHtml(sub);
                    } else {
                        macroRemainder.push(sub);
                    }
                });
                result.macro = simpleMarkdownToHtml(macroRemainder.join(''));
                return;
            }
            if (header.includes('calendar') || (opts.editorial && header.includes('economic event'))) { result.calendar = simpleMarkdownToHtml(body); return; }
            if (header.includes('relative strength') || header.includes('bullish') || header.includes('outperform')) {
                result.bullish = simpleMarkdownToHtml(body); return;
            }
            if (header.includes('social pulse') || header.includes('social sentiment') || header.includes('traders are saying')) {
                result.social = simpleMarkdownToHtml(body); return;
            }
            if (header.includes('youtube') || header.includes('analyst insight')) {
                result.youtube = simpleMarkdownToHtml(body); return;
            }
            if (header.includes('whale') || header.includes('institutional flow') || header.includes('etf flow')) {
                result.whale = simpleMarkdownToHtml(body); return;
            }
            if (header.includes('options corner') || header.includes('options learning') || header.includes('crypto options corner')) {
                result.options = simpleMarkdownToHtml(body); return;
            }
            if (header.includes('catalyst') || header.includes('derivative') || header.includes('options market') || header.includes('upcoming event')) {
                result.catalysts = simpleMarkdownToHtml(body); return;
            }
            if (header.includes('on-chain') || header.includes('onchain') || header.includes('on chain')) {
                result.onchain = simpleMarkdownToHtml(body); return;
            }
            if (header.includes('liquidation') || header.includes('heatmap') || header.includes('liquidity map')) {
                result.heatmap = simpleMarkdownToHtml(body); return;
            }
            if (header.includes('x feed') || header.includes('twitter') || header.includes('trading accounts')) {
                result.xfeedRaw = body; return;
            }
            if (header.includes('trade desk')) { result.tradeDesk = simpleMarkdownToHtml(body); return; }
            if (header.includes('quant engine')) { result.quantEngine = simpleMarkdownToHtml(body); return; }
            if (header.includes('market structure')) { result.marketStructure = simpleMarkdownToHtml(body); return; }
            if (header.includes('high-timeframe') || header.includes('htf overview')) { result.htfOverview = simpleMarkdownToHtml(body); return; }
            if (header.includes('ratio') || header.includes('intermarket')) { result.ratioIntermarket = simpleMarkdownToHtml(body); return; }
            if ((header.includes('commodity') && header.includes('rotation')) || header.includes('sector rotation')) { result.commodityRotation = simpleMarkdownToHtml(body); return; }
            if (header.includes('confluence zone')) { result.confluenceZones = simpleMarkdownToHtml(body); return; }
            if (header.includes('foresight') || header.includes('scenario tree')) { result.foresight = simpleMarkdownToHtml(body); return; }
            if (header.includes('invalidat')) { result.invalidation = simpleMarkdownToHtml(body); return; }
            if (header.includes('correlation')) {
                result.correlation = (result.correlation || '') + '<h3>' + escapeHtml(rawHeader) + '</h3>' + simpleMarkdownToHtml(body); return;
            }
            if (header.includes('action item') || header.includes('action')) {
                var html = simpleMarkdownToHtml(body);
                result.actionItems = html;
                // In personalized reports, action items serve as both shared and personal
                if (result.hasPersonalSplit && !result.personal.actionItems) {
                    result.personal.actionItems = html;
                }
                return;
            }
            if (header === 'overview') { result.overview = simpleMarkdownToHtml(body); return; }
            if (header.includes('counter')) {
                result.correlation = (result.correlation || '') + '<h3>' + escapeHtml(rawHeader) + '</h3>' + simpleMarkdownToHtml(body);
                return;
            }
            // Parse asset sub-sections
            var subParts = part.split(/(?=^### )/m);
            subParts.forEach(function(sub) {
                var subMatch = sub.match(/^### (.+)/);
                if (!subMatch) return;
                var subHeader = subMatch[1].trim();
                symbols.forEach(function(sym) {
                    var normalizedHeader = subHeader.toUpperCase().replace(/\//g, '_');
                    if (normalizedHeader.includes(sym) || subHeader.toUpperCase().includes(sym)) {
                        result.assets[sym] = (result.assets[sym] || '') + simpleMarkdownToHtml(sub);
                    }
                });
            });
        });

        // Parse personal sections
        if (personalMd) {
            var pParts = personalMd.split(/(?=^## )/m);
            pParts.forEach(function(part) {
                var hm = part.match(/^## (.+)/);
                if (!hm) return;
                var h = stripSectionNumber(hm[1].trim()).toLowerCase();
                var body = part.replace(/^## .+\n/, '');
                if (h.includes('executive summary') || h.includes('summary')) result.personal.summary = simpleMarkdownToHtml(body);
                else if (h.includes('devil') || h.includes('counter-thesis') || h.includes('counter thesis')) result.personal.devils = simpleMarkdownToHtml(body);
                else if (h.includes('play evaluation') || h.includes('play status')) result.personal.plays = simpleMarkdownToHtml(body);
                else if (h.includes('youtube') || h.includes('analyst insight')) result.personal.youtube = simpleMarkdownToHtml(body);
                else if (h.includes('x feed') || h.includes('twitter') || h.includes('trading accounts')) result.personal.xfeedRaw = body;
                else if (h.includes('alert') || h.includes('position')) result.personal.alertsFollowup = simpleMarkdownToHtml(body);
                else if (h.includes('comparison') || h.includes('yesterday')) result.personal.comparison = simpleMarkdownToHtml(body);
                else if (h.includes('pattern status') || h.includes('pattern')) result.personal.patternStatus = simpleMarkdownToHtml(body);
                else if (h.includes('action item') || h.includes('action')) result.personal.actionItems = simpleMarkdownToHtml(body);
            });
        }
        return result;
    }

    function renderXFeedCards(md, reportCreatedAt) {
        var authorBlocks = md.split(/(?=^### @)/m).filter(function(b) { return b.trim(); });
        var html = '';
        authorBlocks.forEach(function(block) {
            var headerMatch = block.match(/^### @(\S+)\s*(\([^)]*\))?/);
            if (!headerMatch) return;
            var author = escapeHtml(headerMatch[1]);
            var count = escapeHtml(headerMatch[2] || '');
            var body = block.replace(/^### .+\n/, '').trim();
            var tweets = [];
            var parts = body.split(/\[View tweet\]\([^)]+\)/);
            var links = [];
            var linkRe = /\[View tweet\]\(([^)]+)\)/g;
            var lm;
            while ((lm = linkRe.exec(body)) !== null) links.push(lm[1]);
            for (var i = 0; i < links.length; i++) {
                var raw = (parts[i] || '').trim();
                if (!raw && !links[i]) continue;
                var images = [];
                raw = raw.replace(/!\[[^\]]*\]\(([^)]+)\)/g, function(_, url) { images.push(url); return ''; });
                var meta = '';
                raw = raw.replace(/\*([^*]+(?:likes|views|ago)[^*]*)\*/g, function(_, m) { meta = m.trim(); return ''; });
                raw = escapeHtml(raw);
                raw = raw.replace(/`(\$[A-Z]+)`/g, '<span class="xfeed-tag">$1</span>');
                raw = raw.replace(/^&gt;\s*(.+)$/gm, '<span style="color:var(--text-muted,#6e7681);font-style:italic">$1</span>');
                var text = raw.replace(/\n{2,}/g, '<br>').replace(/\n/g, ' ').trim();
                if (!text && images.length === 0) continue;
                tweets.push({ text: text, images: images, meta: meta, link: links[i] });
            }
            if (parts.length > links.length) {
                var remainder = (parts[parts.length - 1] || '').trim();
                var extraImages = [];
                remainder.replace(/!\[[^\]]*\]\(([^)]+)\)/g, function(_, url) { extraImages.push(url); });
                if (extraImages.length > 0 && tweets.length > 0) {
                    tweets[tweets.length - 1].images = tweets[tweets.length - 1].images.concat(extraImages);
                }
                var extraMeta = '';
                remainder.replace(/\*([^*]+(?:likes|views|ago)[^*]*)\*/g, function(_, m) { extraMeta = m.trim(); });
                if (extraMeta && tweets.length > 0 && !tweets[tweets.length - 1].meta) {
                    tweets[tweets.length - 1].meta = extraMeta;
                }
            }
            if (tweets.length === 0 && body.trim()) {
                var raw2 = body.trim();
                var imgs2 = [];
                raw2 = raw2.replace(/!\[[^\]]*\]\(([^)]+)\)/g, function(_, url) { imgs2.push(url); return ''; });
                var meta2 = '';
                raw2 = raw2.replace(/\*([^*]+(?:likes|views|ago)[^*]*)\*/g, function(_, m) { meta2 = m.trim(); return ''; });
                raw2 = escapeHtml(raw2);
                raw2 = raw2.replace(/`(\$[A-Z]+)`/g, '<span class="xfeed-tag">$1</span>');
                raw2 = raw2.replace(/^&gt;\s*(.+)$/gm, '<span style="color:var(--text-muted,#6e7681);font-style:italic">$1</span>');
                var text2 = raw2.replace(/\n{2,}/g, '<br>').replace(/\n/g, ' ').trim();
                if (text2 || imgs2.length > 0) tweets.push({ text: text2, images: imgs2, meta: meta2, link: '' });
            }
            if (tweets.length === 0) return;
            html += '<div class="xfeed-author">@' + author + '<span class="xfeed-author-count">' + count + '</span></div>';
            html += '<div class="xfeed-grid">';
            tweets.forEach(function(t) {
                html += '<div class="xfeed-card">';
                html += '<div class="xfeed-card-text">' + t.text;
                if (t.link && !/^(javascript|vbscript)/i.test(t.link.trim())) html += ' <a href="' + escapeHtml(t.link) + '" target="_blank" rel="noopener">view &rarr;</a>';
                html += '</div>';
                if (t.images.length > 0) {
                    html += '<div class="xfeed-card-images">';
                    t.images.forEach(function(src) {
                        html += '<img src="' + escapeHtml(src) + '" class="xfeed-img" loading="lazy">';
                    });
                    html += '</div>';
                }
                if (t.meta) html += '<div class="xfeed-meta">' + escapeHtml(resolveRelativeTime(t.meta, reportCreatedAt)) + '</div>';
                html += '</div>';
            });
            html += '</div>';
        });
        return html || simpleMarkdownToHtml(md);
    }

    // Tokenize for redundancy comparison: strip tags + ellipses, lowercase, words only.
    function _normTokens(s) {
        return (s || '').replace(/<[^>]+>/g, ' ').replace(/[…]/g, ' ')
            .toLowerCase().replace(/[^a-z0-9 ]+/g, ' ').split(/\s+/).filter(Boolean);
    }

    // True when `summaryText` is just a truncated/flattened copy of `overviewText`
    // (the Executive-Summary-vs-Overview duplicate). Handles the truncation case the
    // old exact-200-char check missed: drop a likely-cut final token, then require the
    // summary to be a prefix of the overview, or fall back to high token overlap.
    function isSummaryRedundant(summaryText, overviewText) {
        var s = _normTokens(summaryText), o = _normTokens(overviewText);
        if (s.length < 3 || o.length === 0) return false;
        var sCore = s.slice(0, s.length - 1);
        var isPrefix = sCore.every(function(t, i) { return o[i] === t; });
        if (isPrefix) return true;
        var oset = {};
        o.forEach(function(t) { oset[t] = true; });
        var hit = sCore.filter(function(t) { return oset[t]; }).length;
        return hit / sCore.length >= 0.85;
    }

    return {
        REPORT_TABS: REPORT_TABS,
        stripSectionNumber: stripSectionNumber,
        escapeHtml: escapeHtml,
        colorizeAnalysis: colorizeAnalysis,
        resolveRelativeTime: resolveRelativeTime,
        simpleMarkdownToHtml: simpleMarkdownToHtml,
        parseReportSections: parseReportSections,
        renderXFeedCards: renderXFeedCards,
        isSummaryRedundant: isSummaryRedundant
    };
})();
if (typeof window !== 'undefined') { window.ReportCore = ReportCore; }
if (typeof module !== 'undefined' && module.exports) { module.exports = ReportCore; }
