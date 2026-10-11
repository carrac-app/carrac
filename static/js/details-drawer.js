/* ============================================================================
   CARRAC — ASSET DETAILS DRAWER
   Per-asset detail bundle (assemble_asset_detail) presented as a right-side
   drawer over the screener panel. Indicator math mirrors Carrac's:
   Consensio = sum of positive per-TF MA-stack positions (0..9).
   Non-advice posture: observations only.
   Flag-gated: window._features.detailsDrawer must be truthy to activate.
   Namespaced under #cd-drawer / #cd-scrim to avoid collisions.
   ============================================================================ */
(function () {
  "use strict";

  /* ---- icons ---------------------------------------------------------- */
  var IC = {
    back:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>',
    cross:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/></svg>',
    copy:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
    alert:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/></svg>',
    activity:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l2.5 6 5-13L17 12h4"/></svg>',
    layers:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 22 8.5 12 15 2 8.5 12 2"/><polyline points="2 15.5 12 22 22 15.5"/></svg>',
    target:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="0.6" fill="currentColor"/></svg>',
    gauge:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z"/><path d="M12 4a8 8 0 1 0 8 8"/><path d="m12 12 5-5"/></svg>',
    check:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    book:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/></svg>',
    pen:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
    up:'<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 6 19 16 5 16Z"/></svg>',
    down:'<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 18 5 8 19 8Z"/></svg>',
    pulse:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8 12h2l1.5-3 2 6 1.5-3h2"/></svg>',
    warn:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h16.9a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/></svg>',
    info:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>',
    wallet:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/><path d="M16 12h.01M3 9h18"/></svg>'
  };

  /* ---- helpers -------------------------------------------------------- */
  function fmt(v, dec){ if(v==null) return "—"; if(Math.abs(v)>=1e12) return (v/1e12).toFixed(2)+"T"; if(Math.abs(v)>=1e9) return (v/1e9).toFixed(2)+"B"; return v.toLocaleString("en-US",{minimumFractionDigits:dec,maximumFractionDigits:dec}); }
  function consTier(x){ return x===0?"t0":x<=3?"tlow":x<=5?"tmid":x<=7?"tgood":"thigh"; }
  function structClass(s){ return s==="HH/HL"?"up":s==="LH/LL"?"down":"mix"; }

  /* Verdict display map — mirrors scenarios.js _scannerVerdictLabel() exactly.
     Internal verdict values (STRONG/GOOD/NEUTRAL/WEAK/AVOID) keep flowing through
     logic unchanged; this only relabels what's shown to the user. */
  var VERDICT_LABEL = { STRONG:'High', GOOD:'Moderate', NEUTRAL:'Mixed', WEAK:'Low', AVOID:'Conflicting' };
  function _vLabel(v){ return VERDICT_LABEL[String(v||'').toUpperCase()] || v; }

  /* ---- state ---------------------------------------------------------- */
  var state = { openSym: null, cprScope: "monthly", lastDetail: null };

  /* ---- DOM refs (populated by initDetails) ---------------------------- */
  var drawer, scrim, _initDone = false;

  /* ---- real-data helpers ---------------------------------------------- */
  function num(v){ return (v===null||v===undefined)?null:v; }
  function decimalsFor(sym, price){
    if (typeof window.priceDecimals === 'function') { try { return window.priceDecimals(sym, price); } catch(e){} }
    var p = Math.abs(price||0);
    return p>=100?2 : p>=1?2 : p>=0.01?4 : 6;
  }
  function mapDetail(sym, d){
    var row = (window.allAssets && allAssets[sym]) || {};
    var t = d.technicals || {};
    var cons = t.consensio || {};
    function sig(k){ var v = cons[k]; return typeof v === 'number' ? v : 0; }
    var c1d = sig('1d'), c3d = sig('3d'), c1w = sig('1w');
    var emaTrend = t.ema_ribbon_trend;
    var a = {
      sym: sym,
      name: row.name || d.name || sym,
      cls: row.type || 'asset',
      price: num(d.price), dec: decimalsFor(sym, d.price),
      chg: num(d.change_24h) || 0,
      c1d: c1d, c3d: c3d, c1w: c1w,
      xof9: Math.max(0,c1d)+Math.max(0,c3d)+Math.max(0,c1w),
      aligned: !!t.consensio_aligned,
      wrsi: num(t.rsi_weekly), adx: num(t.adx),
      ema: ({bull:'above', bear:'below'})[emaTrend] || 'inside',
      struct: t.structure || '—',
      bb: t.bb ? t.bb.squeeze_pct : null,
      sigsHtml: row._signalsHtml || ''
    };
    function scope(o){ if(!o) return null; return {R3:o.r3,R2:o.r2,R1:o.r1,TC:o.tc,Pivot:o.pivot,BC:o.bc,S1:o.s1,S2:o.s2,S3:o.s3}; }
    var L = d.levels || {};
    var levels = { monthly: scope(L.cpr), weekly: scope(L.weekly_cpr), daily: scope(L.daily_cpr) };
    var prior = { PDH:num(L.pdh), PDL:num(L.pdl), PWH:num(L.pwh), PWL:num(L.pwl) };
    var sc = d.scanner || {};
    var longScore = (sc.long && num(sc.long.score)) || 0;
    var shortScore = (sc.short && num(sc.short.score)) || 0;
    var longVerdict = (sc.long && sc.long.verdict) || '—';
    var shortVerdict = (sc.short && sc.short.verdict) || '—';
    var best = longScore >= shortScore
      ? { side:'Long',  score:longScore,  dir:'bull', verdict:(sc.long&&sc.long.verdict)||sc.best_verdict||'—' }
      : { side:'Short', score:shortScore, dir:'bear', verdict:(sc.short&&sc.short.verdict)||sc.best_verdict||'—' };
    var sen = d.sentiment || {};
    var plays = (d.plays||[]).map(function(p){
      var mo = String(p.conditions_met||'').split('/');
      return { title:p.title||'Play', status:p.status||'idle', dir:(p.direction||'long'),
               met: parseInt(mo[0],10)||0, of: parseInt(mo[1],10)||0 };
    });
    var positions = (d.positions||[]).map(function(p){
      return { dir:p.direction||'long', entry:num(p.entry_price),
               size:p.size||p.risk_status||'', pnl:num(p.unrealized_pnl_pct) };
    });
    var alerts = (d.alerts||[]).map(function(al){
      return { label: al.label || al.description || al.type || 'Alert',
               kind: String(al.alert_type||al.type||'').indexOf('price')>=0 ? 'price' : 'ind' };
    });
    var drawings = (d.drawings||[]).map(function(dr){
      return { type:dr.type||'Drawing', label:dr.label||'(unlabeled)', tf:dr.timeframe||'', price:num(dr.price_level) };
    });
    var av = d.availability || {};
    return { a:a, levels:levels, prior:prior, longScore:longScore, shortScore:shortScore,
             longVerdict:longVerdict, shortVerdict:shortVerdict,
             best:best, regime:sen.regime, regimeScore:num(sen.regime_score),
             plays:plays, positions:positions, alerts:alerts, drawings:drawings,
             avail: { sentiment: av.sentiment !== false } };
  }

  /* ---- section helpers ------------------------------------------------ */
  function secHd(icon, title, right){ return '<div class="sec-hd"><span class="si">'+icon+'</span><h4>'+title+'</h4>'+(right||"")+'</div>'; }
  function availTag(ok){ return ok ? '<span class="avail"><span class="d"></span>live</span>' : ''; }
  function degraded(label){ return '<div class="degraded">'+IC.warn+'<span>'+label+' temporarily unavailable</span><button class="retry">Retry</button></div>'; }

  /* ---- technicals ----------------------------------------------------- */
  function consensioTile(a){
    function tfRow(lab, v){
      var w = Math.abs(v)/3*50;
      var bar = v>0 ? '<i class="up" style="width:'+w+'%"></i>' : v<0 ? '<i class="dn" style="width:'+w+'%"></i>' : '';
      var vc = v>0?"up":v<0?"dn":"z";
      return '<div class="ctf"><span class="lab">'+lab+'</span><span class="div"><span class="mid"></span>'+bar+'</span><span class="v '+vc+'">'+(v>0?"+":"")+v+'</span></div>';
    }
    return '<div class="tile wide"><div class="tl">Consensio <span class="hint">MA stack · 3/7/30</span></div>' +
      '<div class="cons-block"><div class="cons-big '+consTier(a.xof9)+'"><span class="x">'+a.xof9+'</span><span class="of">/ 9</span></div>' +
      '<div class="cons-tfs">'+tfRow("1D",a.c1d)+tfRow("3D",a.c3d)+tfRow("1W",a.c1w) +
      (a.aligned?'<span class="aligned-tag">'+IC.check+'Aligned — stack agrees across timeframes</span>':'') +
      '</div></div></div>';
  }
  function rsiTile(a){
    var v = a.wrsi!=null ? a.wrsi : 50;
    var lab = v<30?"Oversold":v>70?"Overbought":v>50?"Bullish half":"Bearish half";
    return '<div class="tile"><div class="tl">Weekly RSI <span class="hint">14</span></div>' +
      '<div class="gauge-val"><span class="gv">'+v.toFixed(1)+'</span><span class="gl">'+lab+'</span></div>' +
      '<div class="gauge"><div class="track"><span class="zone" style="width:30%;background:var(--blue-soft)"></span><span class="zone" style="width:40%;background:var(--panel-3)"></span><span class="zone" style="width:30%;background:var(--red-soft)"></span></div>' +
      '<span class="tick" style="left:30%"></span><span class="tick" style="left:50%"></span><span class="tick" style="left:70%"></span>' +
      '<span class="mk" style="left:'+v+'%"></span>' +
      '<div class="lbls"><span>0</span><span>30</span><span>70</span><span>100</span></div></div></div>';
  }
  function adxTile(a){
    var v = a.adx!=null ? a.adx : 0;
    var pos = Math.min(100, v/50*100);
    var lab = v<20?"Ranging":v<25?"Forming":"Strong trend";
    return '<div class="tile"><div class="tl">ADX <span class="hint">trend strength</span></div>' +
      '<div class="gauge-val"><span class="gv">'+v+'</span><span class="gl">'+lab+'</span></div>' +
      '<div class="gauge"><div class="track"><span class="zone" style="width:40%;background:var(--panel-3)"></span><span class="zone" style="width:10%;background:var(--orange-soft)"></span><span class="zone" style="width:50%;background:var(--green-soft)"></span></div>' +
      '<span class="tick" style="left:40%"></span><span class="tick" style="left:50%"></span>' +
      '<span class="mk" style="left:'+pos+'%"></span>' +
      '<div class="lbls"><span>0</span><span>20</span><span>25</span><span>50+</span></div></div></div>';
  }
  function emaTile(a){
    var lab = a.ema==="above"?"Price above ribbon":a.ema==="below"?"Price below ribbon":"Inside / crossing";
    return '<div class="tile"><div class="tl">EMA Ribbon <span class="hint">30 / 60</span></div>' +
      '<div class="statline"><span class="ema '+a.ema+'" style="font-size:var(--t-h4)"><span class="d"></span>'+a.ema.charAt(0).toUpperCase()+a.ema.slice(1)+'</span></div>' +
      '<div class="sl" style="font-size:var(--t-2xs);color:var(--fg3);margin-top:6px">'+lab+'</div></div>';
  }
  function bbTile(a){
    var bb = a.bb!=null ? a.bb : 0;
    var c = bb<30?"sqz":bb<50?"sqz":bb<150?"norm":"wide";
    var lab = bb<30?"Extreme squeeze":bb<50?"Squeeze":bb<150?"Normal":"Expanded";
    var fills = bb<30?1:bb<50?2:bb<150?3:4;
    var bars=""; for(var i=1;i<=4;i++) bars+='<i class="'+(i<=fills?"f":"")+'"></i>';
    return '<div class="tile"><div class="tl">Bollinger Width <span class="hint">percentile</span></div>' +
      '<div class="statline"><span class="sv">'+bb+'%</span><span class="sl '+(bb<50?"warn":"")+'">'+lab+'</span></div>' +
      '<div class="bb-gauge '+c+'">'+bars+'</div></div>';
  }
  function structTile(a){
    var up=a.struct==="HH/HL", dn=a.struct==="LH/LL";
    var lab = up?"Uptrend (HH/HL)":dn?"Downtrend (LH/LL)":"Transition";
    var conflict = (up&&a.ema==="below")||(dn&&a.ema==="above");
    return '<div class="tile"><div class="tl">Structure <span class="hint">swing</span></div>' +
      '<div class="statline"><span class="struct '+structClass(a.struct)+'" style="font-size:var(--t-h4)">'+escapeHtml(a.struct)+'</span></div>' +
      '<div class="sl" style="font-size:var(--t-2xs);color:'+(conflict?"var(--orange)":"var(--fg3)")+';margin-top:6px">'+(conflict?"⚠ Conflicts with ribbon":lab)+'</div></div>';
  }

  /* ---- levels ladder -------------------------------------------------- */
  function levelsLadder(d){
    var a = d.a;
    var cpr = d.levels[state.cprScope];
    if (!cpr) return '<div class="uempty">Level data not available for '+escapeHtml(state.cprScope)+' scope</div>';
    var rows = [];
    Object.keys(cpr).forEach(function(k){
      if (cpr[k] == null) return;
      var kind = /R[123]|TC/.test(k)?"res":/S[123]|BC/.test(k)?"sup":"piv";
      if(k==="Pivot") kind="piv";
      rows.push({ name:k, price:cpr[k], kind:kind });
    });
    if (d.prior.PDH != null) rows.push({name:"PDH",price:d.prior.PDH,kind:"prior"});
    if (d.prior.PDL != null) rows.push({name:"PDL",price:d.prior.PDL,kind:"prior"});
    if (d.prior.PWH != null) rows.push({name:"PWH",price:d.prior.PWH,kind:"prior"});
    if (d.prior.PWL != null) rows.push({name:"PWL",price:d.prior.PWL,kind:"prior"});
    if (!rows.length || a.price == null) return '<div class="uempty">No level data available</div>';
    var prices = rows.map(function(r){return r.price;}).concat([a.price]);
    var hi=Math.max.apply(null,prices), lo=Math.min.apply(null,prices), span=hi-lo||1;
    var H=288;
    function y(pr){ return 6 + (1-(pr-lo)/span)*H; }
    var html = '<div class="ladder"><div class="axis"></div>';
    rows.sort(function(a,b){return b.price-a.price;}).forEach(function(r){
      var dist = ((r.price-a.price)/a.price*100);
      var ds = (dist>=0?"+":"")+dist.toFixed(1)+"%";
      html += '<div class="lvl '+r.kind+'" style="top:'+y(r.price)+'px"><span class="lname">'+escapeHtml(r.name)+'</span><span class="lnode"></span><span class="lprice">'+fmt(r.price,a.dec)+'</span><span class="ldist">'+ds+'</span></div>';
    });
    html += '<div class="price-now" style="top:'+y(a.price)+'px"><span class="pn-line"></span><span class="pn-dot"></span><span class="pn-tag">'+fmt(a.price,a.dec)+'</span></div>';
    html += '</div>';
    return html;
  }

  /* ---- scanner -------------------------------------------------------- */
  function scannerSection(d){
    function side(cls, label, score, ic, verdictStr){
      var bars=""; for(var i=1;i<=12;i++) bars+='<i class="'+(i<=score?"f":"")+'"></i>';
      return '<div class="scan-side '+cls+'"><div class="sh">'+ic+label+'</div><div class="sc"><span class="n">'+score+'</span><span class="d">/12 conditions</span></div>' +
        '<div class="vd">'+escapeHtml(_vLabel(verdictStr))+'</div><div class="meter">'+bars+'</div></div>';
    }
    return '<div class="scan-row">'+side("long","Long",d.longScore,IC.up,d.longVerdict)+side("short","Short",d.shortScore,IC.down,d.shortVerdict)+'</div>' +
      '<div class="best-tag">'+IC.target+'Best read: <b>'+escapeHtml(d.best.side)+' '+d.best.score+'/12 conditions · '+escapeHtml(_vLabel(d.best.verdict))+'</b></div>';
  }

  /* ---- conditions (Desk context — raw indicator state, no scores/verdicts) --
     Desk replaces the scanner score/verdict section with a plain conditions
     checklist. Reads the raw scanner row (window.allAssets[sym]) — the same
     shape the dashboard's screener columns read (asset.ema_ribbon, asset.structure,
     asset.bb, asset.cpr, asset.volume_metrics, asset.adx, asset.macd) — NOT
     the mapped `d`/`a` detail-bundle shape used elsewhere in this file.
     CONDITIONS-ONLY: never include scores, "x/12" counts, or verdict words
     (STRONG/GOOD/WEAK/AVOID) here. */
  function conditionsSection(row) {
    var RIBBON = { bull: 'Above ribbon', bear: 'Below ribbon', neutral: 'Inside ribbon' };
    var items = [];
    var er = row.ema_ribbon || {};
    if (er.trend) items.push(['EMA Ribbon', RIBBON[er.trend] +
      (typeof er.days_since_flip === 'number' ? ' · ' + er.days_since_flip + 'd' : '')]);
    var st = row.structure || {};
    if (st.last_high_type && st.last_low_type) items.push(['Structure', escapeHtml(st.last_high_type) + '·' + escapeHtml(st.last_low_type)]);
    if (row.weekly_rsi != null) items.push(['Weekly RSI', Math.round(row.weekly_rsi)]);
    var bb = row.bb || {};
    if (bb.squeeze_pct != null) items.push(['BB Width', Math.round(bb.squeeze_pct) + '% of avg']);
    var cpr = row.cpr || {};
    if (cpr.price_level) items.push(['CPR zone', escapeHtml(cpr.price_level)]);
    var vm = row.volume_metrics || {};
    if (vm.ratio != null) items.push(['Volume', vm.ratio.toFixed(1) + 'x avg']);
    var adx = row.adx || {};
    if (adx.regime) items.push(['ADX regime', escapeHtml(String(adx.regime).replace('_', ' '))]);
    var macd = row.macd || {};
    if (macd.histogram != null) items.push(['MACD histogram', macd.histogram > 0 ? 'positive' : 'negative']);
    if (!items.length) return '';
    return '<div class="cd-conditions">' +
      items.map(function (it) {
        return '<div class="cd-cond"><span>' + it[0] + '</span><b>' + it[1] + '</b></div>';
      }).join('') + '</div>';
  }

  /* ---- user lists ----------------------------------------------------- */
  function playsSection(d){
    if(!d.plays.length) return '<div class="uempty">No active plays on '+escapeHtml(d.a.sym)+'</div>';
    return '<div class="ulist">'+d.plays.map(function(p){
      return '<div class="urow"><div class="ut"><div class="a">'+escapeHtml(p.title)+' <span class="dirtag '+escapeHtml(p.dir)+'">'+escapeHtml(p.dir.toUpperCase())+'</span></div>' +
        '<div class="b">conditions met</div></div><div class="uright"><span class="statustag '+escapeHtml(p.status)+'">'+escapeHtml(p.status)+'</span>' +
        '<div class="cond" style="margin-top:6px"><b>'+p.met+'</b>/'+p.of+'</div></div></div>';
    }).join("")+'</div>';
  }
  function positionsSection(d){
    if(!d.positions.length) return '<div class="uempty">No open position</div>';
    return '<div class="ulist">'+d.positions.map(function(p){
      var cls = p.pnl>=0?"pos":"neg";
      return '<div class="urow"><div class="ut"><div class="a"><span class="dirtag '+escapeHtml(p.dir)+'">'+escapeHtml(p.dir.toUpperCase())+'</span> '+escapeHtml(String(p.size))+'</div>' +
        '<div class="b">entry '+fmt(p.entry,d.a.dec)+'</div></div><div class="uright"><span class="pnl '+cls+'">'+(p.pnl!=null?(p.pnl>=0?"+":"")+p.pnl+'%':'—')+'</span><div class="small">unrealized</div></div></div>';
    }).join("")+'</div>';
  }
  function alertsSection(d){
    if(!d.alerts.length) return '<div class="uempty">No alerts on '+escapeHtml(d.a.sym)+'</div>';
    return '<div class="ulist">'+d.alerts.map(function(al){
      return '<div class="urow"><div class="ut"><div class="a">'+escapeHtml(al.label)+'</div><div class="b">'+(al.kind==="price"?"price cross":"indicator")+'</div></div>' +
        '<div class="uright"><span class="statustag armed">armed</span></div></div>';
    }).join("")+'</div>';
  }
  function drawingsSection(d){
    if(!d.drawings.length) return '<div class="uempty">No drawings on '+escapeHtml(d.a.sym)+'</div>';
    return '<div class="ulist">'+d.drawings.map(function(dr){
      return '<div class="urow"><div class="ut"><div class="a">'+escapeHtml(dr.label)+'</div><div class="b">'+escapeHtml(dr.type)+' · '+escapeHtml(dr.tf)+'</div></div>' +
        '<div class="uright"><span class="price">'+fmt(dr.price,d.a.dec)+'</span></div></div>';
    }).join("")+'</div>';
  }

  /* ---- skeleton + error states ---------------------------------------- */
  function renderDrawerSkeleton(sym){
    var row = (window.allAssets && allAssets[sym]) || {};
    // allAssets rows come from /api/assets (both desk.js and the dashboard populate it
    // identically from that same payload shape), which carries `latest_price` —
    // `row.price` here was always undefined. Fallback kept in case a future
    // caller ever passes a .price-bearing object.
    var rowPrice = row.latest_price != null ? row.latest_price : row.price;
    var price = rowPrice != null ? '$'+fmt(rowPrice, decimalsFor(sym, rowPrice)) : '';
    var H = '<div class="dhead">' +
      '<div class="dh-top"><button class="back" id="drClose">'+IC.back+'</button><span class="spread"></span></div>' +
      '<div class="dh-id"><div class="symwrap"><span class="sym">'+escapeHtml(sym)+'</span>' +
      '<span class="name">'+escapeHtml(row.name||sym)+'<span class="cls">'+escapeHtml(row.type||'')+'</span></span></div>' +
      (price?'<div class="pxwrap"><div class="px">'+price+'</div></div>':'') +
      '</div></div>' +
      '<div style="padding:24px 18px;color:var(--fg3);font-size:var(--t-xs)">Loading detail bundle…</div>';
    var body = document.getElementById('cd-drawer-body');
    if (body) body.innerHTML = H;
    var c = document.getElementById('drClose');
    if(c) c.onclick = closeDrawer;
  }

  function renderDrawerError(sym, e){
    var isPremium = e && (e.message === '402' || e.message === '403');
    var body = document.getElementById('cd-drawer-body');
    if (!body) return;
    var msg = isPremium
      ? 'Premium feature — upgrade to view full detail.'
      : 'Failed to load detail bundle.';
    body.innerHTML = '<div class="dhead"><div class="dh-top"><button class="back" id="drClose">'+IC.back+'</button></div></div>' +
      '<div style="padding:24px 18px">' +
      '<div class="degraded">'+IC.warn+'<span>'+msg+'</span>'+(isPremium?'':'<button class="retry">Retry</button>')+'</div></div>';
    var c = document.getElementById('drClose');
    if(c) c.onclick = closeDrawer;
    body.querySelectorAll('.retry').forEach(function(r){ r.onclick = function(){ openDrawer(sym); }; });
  }

  /* ---- drawer assembly ------------------------------------------------ */
  function renderDrawer(sym, d){
    var a = d.a;
    // Evaluated at render time (not module load) so a context change between
    // drawer opens (or a script-load-order quirk) is always picked up fresh —
    // desk.html sets data-context in markup, but this stays robust either way.
    var _isDesk = document.body.dataset.context === 'desk';
    var row = (window.allAssets && window.allAssets[sym]) || {};
    var H = "";
    // header
    H += '<div class="dhead"><div class="dh-top">' +
      '<button class="back" id="drClose">'+IC.back+'</button>' +
      '<button class="star'+(typeof isInWatchlist==='function'&&isInWatchlist(sym)?" on":"")+'" id="drStar" title="Watchlist">★</button>' +
      '<span class="spread"></span>' +
      // Copy for AI ships the raw /api/asset/<sym>/detail bundle verbatim, which
      // includes scanner verdict strings — the Desk is conditions-only,
      // so v1 simply doesn't offer this action
      // there. bindDrawer()'s `if(copyBtn)` guard already no-ops when the button
      // is absent. Revisit with a Desk-safe payload if it is wanted back.
      (_isDesk ? '' : '<button class="dh-act" id="drCopy">'+IC.copy+'Copy for AI</button>') +
      '<button class="dh-act primary" id="drChart">'+IC.cross+'Chart</button>' +
      '</div>';
    H += '<div class="dh-id"><div class="symwrap"><span class="sym">'+escapeHtml(a.sym)+'</span>' +
      '<span class="name">'+escapeHtml(a.name)+'<span class="cls">'+escapeHtml(a.cls)+'</span></span></div>' +
      '<div class="pxwrap"><div class="px">$'+fmt(a.price,a.dec)+'</div>' +
      '<div class="pxchg '+(a.chg>=0?"pos":"neg")+'">'+(a.chg>=0?"+":"")+a.chg.toFixed(2)+'% 24h</div></div></div>';
    // verdict band — chart-view only; Desk context is conditions-only (no scores/verdicts)
    if (!_isDesk) {
      H += '<div class="verdict-band '+d.best.dir+'"><span class="vb-ic">'+(d.best.dir==="bull"?IC.up:IC.down)+'</span>' +
        '<span class="vb-t">Best scanner read<b>'+escapeHtml(d.best.side)+' setup · '+escapeHtml(_vLabel(d.best.verdict))+'</b></span>' +
        '<span class="vb-score">'+d.best.score+'<span class="u">/12 conditions</span></span></div>';
    }
    // badges from captured screener row HTML
    if(a.sigsHtml) H += '<div class="badge-row">'+a.sigsHtml+'</div>';
    H += '</div>'; // dhead

    // TECHNICALS — chart view only. The Consensio "x/9" tile lives in this row
    // and duplicates what conditionsSection() already covers in plain-state
    // form, so it's suppressed on the Desk (the Desk design rule:
    // "no counts like 9/12").
    if (!_isDesk) {
      H += '<div class="section">'+secHd(IC.activity,"Technicals",availTag(true)) +
        '<div class="tech-grid">'+consensioTile(a)+rsiTile(a)+adxTile(a)+emaTile(a)+bbTile(a)+structTile(a)+'</div></div>';
    }
    // LEVELS
    H += '<div class="section">'+secHd(IC.layers,"Levels", '<div class="cpr-switch">' +
      ["monthly","weekly","daily"].map(function(s){return '<button data-cpr="'+s+'" class="'+(state.cprScope===s?"on":"")+'">'+s.charAt(0).toUpperCase()+s.slice(1)+'</button>';}).join("") + '</div>') +
      levelsLadder(d) + '</div>';
    // SCANNER (chart view) / CONDITIONS (Desk — score+verdict section suppressed)
    if (_isDesk) {
      H += '<div class="section">'+secHd(IC.target,"Conditions",availTag(true))+conditionsSection(row)+'</div>';
    } else {
      H += '<div class="section">'+secHd(IC.target,"Scanner",availTag(true))+scannerSection(d)+'</div>';
    }
    // SENTIMENT — chart view only. The numeric regime score is aggregate/
    // verdict-adjacent language the Desk's conditions-only rule excludes.
    if (!_isDesk) {
      H += '<div class="section">'+secHd(IC.pulse,"Sentiment",availTag(d.avail.sentiment));
      if(!d.avail.sentiment) H += degraded("Regime feed");
      else {
        var rc = d.regime==="Risk-on"?"on":d.regime==="Risk-off"?"off":"neu";
        var rs = d.regimeScore!=null ? d.regimeScore : 50;
        H += '<div class="regime"><span class="rchip '+rc+'">'+(d.regime||'Neutral')+'</span>' +
          '<div class="rbar"><div class="t"><span>Risk-off</span><span>'+rs+'</span><span>Risk-on</span></div>' +
          '<div class="track"><i style="width:'+rs+'%"></i></div></div></div>';
      }
      H += '</div>';
    }
    // USER-SCOPED
    H += '<div class="section">'+secHd(IC.book,"Your plays",'<span class="scope">you</span>')+playsSection(d)+'</div>';
    H += '<div class="section">'+secHd(IC.wallet,"Your position",'<span class="scope">you</span>')+positionsSection(d)+'</div>';
    H += '<div class="section">'+secHd(IC.alert,"Your alerts",'<span class="scope">you</span>')+alertsSection(d)+'</div>';
    H += '<div class="section">'+secHd(IC.pen,"Your drawings",'<span class="scope">you</span>')+drawingsSection(d)+'</div>';
    // non-advice
    H += '<div class="nonadvice">'+IC.info+'<span>This bundle is information and analysis to evaluate yourself — observations, never a recommendation to buy, sell, or hold. Markets carry risk. DYOR.</span></div>';

    var body = document.getElementById("cd-drawer-body");
    if (body) body.innerHTML = H;
    bindDrawer(sym);
  }

  function bindDrawer(sym){
    var c = document.getElementById("drClose");
    if(c) c.onclick = closeDrawer;
    var chartBtn = document.getElementById("drChart");
    if(chartBtn) chartBtn.onclick = function(){
      closeDrawer();
      // openAdvancedChartModal routes correctly on BOTH layouts: inline split panel
      // on desktop, fullscreen modal on mobile. selectAssetInPanel is desktop-only
      // (guarded by chartPanelReady) and is a no-op on mobile — calling it directly
      // left this button dead on phones.
      if (typeof openAdvancedChartModal === 'function') openAdvancedChartModal(sym);
      else if (typeof selectAssetInPanel === 'function') selectAssetInPanel(sym);
      else {
        // Neither global exists — the chart app isn't loaded (desk.html). Fall back
        // to a hard navigation to the standalone chart view.
        if (window.posthog) posthog.capture('desk_chart_out', { symbol: sym });
        location.href = '/chart/' + sym + '/1D';
      }
    };
    var copyBtn = document.getElementById("drCopy");
    if(copyBtn) copyBtn.onclick = function(){
      fetch('/api/asset/'+encodeURIComponent(sym)+'/detail?format=compact', {credentials:'same-origin'})
        .then(function(r){ return r.json(); })
        .then(function(j){ return navigator.clipboard.writeText(JSON.stringify(j,null,2)); })
        .then(function(){ showToast('Detail bundle copied for AI', 'success'); })
        .catch(function(){ showToast('Copy failed', 'error'); });
    };
    var starBtn = document.getElementById("drStar");
    if(starBtn) starBtn.onclick = function(){
      if (typeof isInWatchlist === 'function') {
        if (isInWatchlist(sym)) {
          if (typeof removeFromWatchlist === 'function') removeFromWatchlist(sym);
        } else {
          if (typeof addToWatchlist === 'function') addToWatchlist(sym);
        }
        if (typeof updateStarIcons === 'function') updateStarIcons();
        starBtn.classList.toggle('on', typeof isInWatchlist === 'function' && isInWatchlist(sym));
      }
    };
    var drawerEl = document.getElementById('cd-drawer');
    if (drawerEl) {
      drawerEl.querySelectorAll("[data-cpr]").forEach(function(b){
        b.onclick = function(){
          state.cprScope = b.dataset.cpr;
          if (state.lastDetail) renderDrawer(sym, state.lastDetail);
        };
      });
      drawerEl.querySelectorAll(".retry").forEach(function(r){
        r.onclick = function(){ openDrawer(sym); };
      });
    }
  }

  /* ---- open/close ----------------------------------------------------- */
  function openDrawer(sym){
    // desk.html doesn't load the chart app, so nothing there is guaranteed to have
    // called initDetails() before the first click — self-heal here. initDetails
    // is idempotent (guarded by _initDone), so this is a no-op when main.js's
    // CarracDetails.init() (or desk.js's own call to it) already ran.
    if (!_initDone) initDetails();
    state.openSym = sym;
    renderDrawerSkeleton(sym);
    drawer.classList.add('open');
    scrim.classList.add('show');
    scrim.hidden = false;
    var app = document.querySelector('.app') || document.body;
    app.classList.add('cd-drawer-open');
    drawer.setAttribute('aria-hidden','false');
    fetch('/api/asset/'+encodeURIComponent(sym)+'/detail', {credentials:'same-origin'})
      .then(function(r){
        if(!r.ok) throw new Error(String(r.status));
        return r.json();
      })
      .then(function(json){
        var mapped = mapDetail(sym, json);
        state.lastDetail = mapped;
        renderDrawer(sym, mapped);
      })
      .catch(function(e){ renderDrawerError(sym, e); });
  }

  function closeDrawer(){
    state.openSym = null;
    state.lastDetail = null;
    if (drawer) {
      drawer.classList.remove('open');
      drawer.setAttribute('aria-hidden','true');
    }
    if (scrim) {
      scrim.classList.remove('show');
      scrim.hidden = true;
    }
    var app = document.querySelector('.app') || document.body;
    app.classList.remove('cd-drawer-open');
  }

  /* ---- init (called from main.js and desk.js after DOM ready) --------------------- */
  function initDetails(){
    if (_initDone) return;
    _initDone = true;
    drawer = document.getElementById('cd-drawer');
    scrim = document.getElementById('cd-scrim');
    if (!drawer || !scrim) {
      console.warn('[CarracDetails] Drawer DOM not found; skipping init');
      return;
    }
    scrim.onclick = closeDrawer;
    document.addEventListener('keydown', function(e){ if(e.key === 'Escape' && state.openSym) closeDrawer(); });
  }

  /* ---- public API ----------------------------------------------------- */
  window.CarracDetails = { init: initDetails, open: openDrawer };
  window.openDetailsDrawer = openDrawer;

})();
