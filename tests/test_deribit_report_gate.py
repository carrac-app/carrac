"""The report pipeline must not publish Deribit-derived data.

The earlier Deribit gate covered the MCP tools, the formula/DSL vars, quant/bridge.py and the
templates. It did NOT touch morning_engine/, which is what actually writes the
`general` report published at /brief — a page with no login. So the most public
surface Carrac has was narrating DVOL/GEX/skew every morning while every other
door was shut.

These tests guard the two paths that were open, in the two places a future
change would most plausibly reopen them.
"""
import pytest

from carrac.compliance.deribit_gate import (
    DERIBIT_COLUMNS, deribit_leadlag_metrics, leadlag_metric_allowed)


class TestOptionsBlockIsGated:
    def test_withheld_without_an_admin_caller(self):
        """Reports run from cron — no MCP contextvar, no Flask request — so
        deribit_visible() resolves False and the whole block is withheld.
        Empty string, not a placeholder: a note would invite the writer model
        to narrate its own absence."""
        from morning_engine.data_assembly import fetch_options_data
        md, claims = fetch_options_data()
        assert md == ""
        assert claims == []  # no prose, no claims — the tripwire has nothing to check

    def test_admin_still_gets_it(self, monkeypatch):
        """Guard against 'fixing' the leak by breaking the feature. The rule is
        collect freely / expose to admin only, not collect-and-discard."""
        import carrac.compliance.deribit_gate as gate
        monkeypatch.setattr(gate, "deribit_visible", lambda: True)
        from morning_engine.data_assembly import fetch_options_data
        md, claims = fetch_options_data()
        assert md != ""
        assert isinstance(claims, list)  # the grounding claims ride with the prose


class TestLeadLagCorrelationsAreGated:
    def test_gate_actually_recognises_the_deribit_metrics(self):
        """The failure mode that makes a gate decoration: it runs, returns
        True for everything, and guards nothing. These names are what appear
        in quant.correlations and on /brief's correlation cards."""
        gated = deribit_leadlag_metrics()
        assert gated, "empty gate set means nothing is filtered"
        for name in ("DVOL_BTC", "SKEW_25D_BTC", "GEX_BTC", "IV_RV_BTC", "BASIS_BTC"):
            assert name in gated, f"{name} is Deribit-backed but not gated"
            assert leadlag_metric_allowed(name) is False

    def test_permitted_metrics_are_not_over_blocked(self):
        """Binance/Hyperliquid data is licence-permitted — this is a
        licensing boundary, not a paywall."""
        for name in ("RVOL_BTC", "FUNDING_BTC", "OI_BTC", "BTC", "SPX"):
            assert leadlag_metric_allowed(name) is True

    def test_correlation_shifts_drop_deribit_pairs(self, monkeypatch):
        """Pins that `DVOL_BTC / RVOL_BTC` never renders on the public /brief.
        Filtering happens before the top-N cut so a dropped pair cannot eat a
        slot and silently shrink the module."""
        import carrac.facts.correlations as corr

        ts_now, ts_prior = "2026-08-05", "2026-07-29"
        rows = []
        for ts in (ts_now, ts_prior):
            rows += [
                {"metric_a": "DVOL_BTC", "metric_b": "RVOL_BTC",
                 "correlation": 0.9 if ts == ts_now else 0.1,
                 "q_value": 0.01, "significant": True, "ts": ts},
                {"metric_a": "BTC", "metric_b": "SPX",
                 "correlation": 0.8 if ts == ts_now else 0.1,
                 "q_value": 0.01, "significant": True, "ts": ts},
            ]

        captured = {}

        def fake_compute(today, prior, **kw):
            captured["pairs"] = {(r["metric_a"], r["metric_b"]) for r in today}
            return []

        monkeypatch.setattr(corr, "_compute_shifts", fake_compute)

        class _Cur:
            def __init__(self): self._q = 0
            def execute(self, *a, **k): self._q += 1
            def fetchone(self):
                return {"ts": ts_now} if self._q == 1 else {"ts": ts_prior}
            def fetchall(self): return rows

        class _Conn:
            def cursor(self): return _Cur()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(corr, "get_conn", lambda: _Conn())
        corr.get_correlation_shifts()

        assert ("BTC", "SPX") in captured["pairs"], "permitted pair was dropped"
        assert ("DVOL_BTC", "RVOL_BTC") not in captured["pairs"], \
            "Deribit-backed correlation reached the public /brief renderer"


class TestPromptsDoNotAskForWithheldFields:
    @pytest.mark.parametrize("token", ["DVOL", "GEX flip", "skew shifts", "IV-RV premium direction"])
    def test_writer_is_not_instructed_to_produce_them(self, token):
        """With the data withheld, an instruction to 'track DVOL trend' is an
        invitation to invent one — precisely the earlier failure, where a
        published report stated a GEX flip point 2.6x spot."""
        from morning_engine import prompts
        src = open(prompts.__file__, encoding="utf-8").read()
        for line in src.splitlines():
            if token in line and "OPTIONS-DATA-ABSENT" not in line and "never" not in line.lower():
                pytest.fail(f"prompt still asks for withheld field {token!r}: {line.strip()[:120]}")


class TestSnapshotJsonIslandIsGated:
    """The leak the rendered-card fix missed.

    templates/brief_reader.html serialises the WHOLE report object into
    `window.__briefData` via `report | tojson`. So even with the correlation
    cards filtered, the JSON beside them still contained
    {"metric": "GEX_BTC"} and {"leader": "SKEW_25D_BTC"} — verified live on
    prod after the first fix promoted. Hiding a card while the payload next to
    it names the metric is not a gate.
    """

    def test_gated_metrics_are_stripped_from_both_shapes(self):
        from quant.api.routes import _strip_gated_metrics

        corr = [{"metric": "GEX_BTC"}, {"metric": "SPX"},
                {"metric": "SKEW_25D_BTC"}, {"metric": "RVOL_BTC"}]
        assert [e["metric"] for e in _strip_gated_metrics(corr, "metric")] == \
            ["SPX", "RVOL_BTC"]

        ll = [{"leader": "SKEW_25D_BTC"}, {"leader": "DXY"}, {"leader": "DVOL_BTC"}]
        assert [e["leader"] for e in _strip_gated_metrics(ll, "leader")] == ["DXY"]

    def test_tolerates_empty_and_missing(self):
        """build_snapshot hands these straight through on the quiet paths.

        An entry with no metric name is KEPT, deliberately. The gate exists to
        stop a barred metric NAME being published; an entry that names nothing
        cannot leak one, and dropping it would silently discard a differently
        shaped record for no licensing benefit. Fail-closed belongs on the
        caller-identity question (deribit_visible), not here.
        """
        from quant.api.routes import _strip_gated_metrics
        assert _strip_gated_metrics([], "metric") == []
        assert _strip_gated_metrics(None, "metric") is None
        assert _strip_gated_metrics([{"other": "x"}], "metric") == [{"other": "x"}]

    def test_snapshot_wires_the_filter_to_all_three_lists(self):
        """What calls this — the helper existing is not the feature working.
        All three list keys carry raw metric names and all three are published.
        """
        import inspect
        from quant.api import routes
        # The gate lives in _gate_snapshot: same three keys, same filter,
        # applied on the way out instead of before the cache write.
        gsrc = inspect.getsource(routes._gate_snapshot)
        for key in ("out['top_correlations'] = _strip_gated_metrics(",
                    "out['leadlag_signals'] = _strip_gated_metrics(",
                    "out['leadlag_signals_weekly'] = _strip_gated_metrics("):
            assert key in gsrc, f"snapshot no longer filters {key}"


class TestAnomaliesAreGated:
    """Anomalies are the worst of the three lists: each row carries the metric
    name AND `value`, the raw column number. One ungated row publishes the
    exact figure the licence bars, not merely the fact that we hold it."""

    def test_both_spellings_and_the_domain_are_blocked(self):
        from quant.api.routes import _strip_gated_anomalies

        rows = [
            {"metric": "dvol", "domain": "derivatives_options", "value": 37.5},
            {"metric": "DVOL_BTC", "domain": "derivatives", "value": 37.5},
            {"metric": "gex_net", "domain": "derivatives_options", "value": 1e9},
            {"metric": "funding_rate_avg", "domain": "derivatives", "value": 0.0001},
            {"metric": "RVOL_BTC", "domain": "volatility", "value": 0.4},
            {"metric": "CROSS_DISPERSION", "domain": "breadth", "value": 1.2},
            {"metric": "not_yet_listed", "domain": "derivatives_options", "value": 9},
        ]
        kept = {e["metric"] for e in _strip_gated_anomalies(rows)}
        assert kept == {"funding_rate_avg", "RVOL_BTC", "CROSS_DISPERSION"}

    def test_raw_column_spelling_alone_is_not_enough(self):
        """quant.zscores stores the COLUMN (dvol) while the correlation tables
        store the composed name (DVOL_BTC). Gating one spelling is how a filter
        looks correct and leaks anyway."""
        from quant.api.routes import _strip_gated_anomalies
        assert _strip_gated_anomalies([{"metric": "dvol", "domain": "x"}]) == []
        assert _strip_gated_anomalies([{"metric": "DVOL_BTC", "domain": "x"}]) == []

    def test_count_matches_what_is_shown(self):
        """anomaly_count must be derived from the FILTERED list — a headline
        advertising rows the caller cannot see is its own small lie."""
        import inspect
        from quant.api import routes
        inspect.getsource(routes)
        # The gate lives in _gate_snapshot so the cached payload stays
        # caller-independent; the count is derived
        # from the filtered list, never from the raw one.
        gsrc = inspect.getsource(routes._gate_snapshot)
        assert "out['anomaly_count'] = len(out['anomalies'] or [])" in gsrc
        assert "out['anomalies'] = _strip_gated_anomalies(" in gsrc


class TestTransmissionChainsAreGated:
    """The sibling this sweep kept missing.

    After top_correlations, leadlag_signals, leadlag_signals_weekly and
    anomalies were all filtered, the assembled report briefing STILL carried:

        Chain [derivatives_leverage] strength=0.001: weakest FUNDING_BTC→BASIS_BTC

    because morning_engine/data_assembly.py::fetch_quant_context reads snap['chains'] and that
    key was never filtered. deribit_gate.py's own docstring had flagged this
    chain as "built almost entirely from" Deribit metrics.
    """

    def test_derivatives_leverage_chain_is_dropped(self):
        from quant.api.routes import _strip_gated_chains

        rows = [
            {"chain": "monetary_policy", "weakest_link": {"from": "SPX", "to": "BTC"}},
            {"chain": "derivatives_leverage",
             "weakest_link": {"from": "FUNDING_BTC", "to": "BASIS_BTC"}},
            {"chain": "risk_appetite", "weakest_link": {"from": "VIX", "to": "HY_SPREAD"}},
        ]
        kept = [c["chain"] for c in _strip_gated_chains(rows)]
        assert kept == ["monetary_policy", "risk_appetite"]

    def test_the_chain_really_is_deribit_backed(self):
        """If someone rewires this chain onto permitted metrics the test above
        would pass for the wrong reason. Pin why it is gated."""
        from quant.engine.config import TRANSMISSION_CHAINS
        from carrac.compliance.deribit_gate import leadlag_metric_allowed

        links = TRANSMISSION_CHAINS["derivatives_leverage"]["links"]
        flat = {m for pair in links for m in pair}
        assert not all(leadlag_metric_allowed(m) for m in flat), \
            "derivatives_leverage no longer touches a gated metric — re-check the filter"

    def test_whole_chain_goes_not_just_the_label(self):
        """chain_strength is a geometric mean ACROSS the links, so the number
        is derived from withheld data even with no barred name printed."""
        from quant.api.routes import _strip_gated_chains
        rows = [{"chain": "derivatives_leverage", "chain_strength": 0.42,
                 "weakest_link": {"from": "RVOL_BTC", "to": "BTC"}}]
        assert _strip_gated_chains(rows) == [], \
            "chain survived because its weakest_link happened to be permitted"

    def test_snapshot_wires_it(self):
        import inspect
        from quant.api import routes
        assert "out['chains'] = _strip_gated_chains(" in inspect.getsource(routes._gate_snapshot)


class TestPublicationFilterIgnoresCallerIdentity:
    """The root cause the first four fixes all missed.

    build_snapshot() is correctly gated — but the gate answers "may this CALLER
    see it", and the report engine reaches the API with X-Internal-Key.
    carrac/auth/decorators.py::get_request_tier resolves any valid key to 'admin', so
    deribit_visible() returns True and the gate opens exactly as designed. The
    engine then wrote what it saw into the `general` report, published at
    /brief with no login.

    A published report is public no matter who ran the job, so the report
    assembly filters unconditionally rather than asking who is calling.
    """

    def _snap(self):
        return {
            "top_correlations": [{"metric": "GEX_BTC"}, {"metric": "SPX"}],
            "leadlag_signals": [{"leader": "BASIS_BTC"}, {"leader": "DXY"}],
            "leadlag_signals_weekly": [{"leader": "SKEW_25D_BTC"}, {"leader": "M2"}],
            "chains": [{"chain": "derivatives_leverage"}, {"chain": "monetary_policy"}],
            "anomalies": [{"metric": "dvol", "domain": "derivatives_options"},
                          {"metric": "LS_RATIO_ADA", "domain": "derivatives"}],
            "anomaly_count": 2,
        }

    def test_strips_everything_even_as_admin(self, monkeypatch):
        """The decisive case: admin caller, published artifact. Must still strip."""
        import carrac.compliance.deribit_gate as gate
        monkeypatch.setattr(gate, "deribit_visible", lambda: True)

        from morning_engine.data_assembly import _strip_deribit_for_publication
        out = _strip_deribit_for_publication(self._snap())

        assert [e["metric"] for e in out["top_correlations"]] == ["SPX"]
        assert [e["leader"] for e in out["leadlag_signals"]] == ["DXY"]
        assert [e["leader"] for e in out["leadlag_signals_weekly"]] == ["M2"]
        assert [c["chain"] for c in out["chains"]] == ["monetary_policy"]
        assert [e["metric"] for e in out["anomalies"]] == ["LS_RATIO_ADA"]
        assert out["anomaly_count"] == 1

    def test_wired_into_the_briefing_fetch(self):
        """What calls this. The filter existing is not the feature working —
        it has to sit on the path fetch_quant_context actually takes."""
        import inspect
        from morning_engine import data_assembly
        src = inspect.getsource(data_assembly.fetch_quant_context)
        assert "_strip_deribit_for_publication(snap)" in src

    def test_fails_closed_if_the_gate_module_is_unimportable(self, monkeypatch):
        """A licensing control must not fail OPEN on a broken install."""
        import builtins
        real_import = builtins.__import__

        def boom(name, *a, **kw):
            if "deribit_gate" in name:
                raise ImportError("simulated")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", boom)
        from morning_engine.data_assembly import _strip_deribit_for_publication
        out = _strip_deribit_for_publication(self._snap())

        assert out["top_correlations"] == []
        assert out["chains"] == []
        assert out["anomalies"] == []
        assert out["anomaly_count"] == 0


class TestAnomalyGateCoverage:
    """Anomaly gating coverage.

    The public /brief page must never serve SKEW_25D_ETH, SKEW_25D_BTC, GEX_ETH or OPTIONS_IV_ETH to anonymous visitors.
    Three separate causes, one per test below. The first test is the one that
    matters long-term: it makes the guard able to see what it is guarding.
    """

    @staticmethod
    def _registry():
        """(prefix, column) pairs per derivatives domain, from the SQL itself."""
        import re
        from quant.normalize import DOMAIN_QUERIES
        pat = re.compile(
            r"'(\w+?)_'\s*\|\|\s*asset\s+as\s+metric,\s*asset,\s*(\w+)\s+as\s+value",
            re.I | re.S)
        return {d: pat.findall(q) for d, q in DOMAIN_QUERIES.items()
                if d.startswith('derivatives')}

    def test_domain_split_matches_the_sql_registry(self):
        """A domain is barred iff its SQL reads a barred column.
        """
        from carrac.compliance.deribit_gate import (
            DERIBIT_ANOMALY_DOMAINS, PERMITTED_ANOMALY_DOMAINS,
            PERMITTED_DERIVATIVES_COLUMNS)
        reg = self._registry()
        assert reg, 'registry parse found nothing — the SQL shape changed'
        for domain, pairs in reg.items():
            cols = {c for _, c in pairs}
            if cols & set(DERIBIT_COLUMNS):
                assert domain in DERIBIT_ANOMALY_DOMAINS, (
                    f'{domain} reads Deribit columns {cols & set(DERIBIT_COLUMNS)} '
                    f'but is not gated')
                assert not (cols - set(DERIBIT_COLUMNS)), (
                    f'{domain} mixes barred and permitted columns — the '
                    f'domain-level rule no longer holds for it')
            else:
                assert domain in PERMITTED_ANOMALY_DOMAINS, (
                    f'{domain} is neither gated nor listed as permitted')
                assert cols <= set(PERMITTED_DERIVATIVES_COLUMNS), (
                    f'{domain} reads {cols}, not all of which are licence-permitted')

    def test_every_barred_prefix_is_registered(self):
        from carrac.compliance.deribit_gate import (
            DERIBIT_ANOMALY_DOMAINS, DERIBIT_METRIC_PREFIXES)
        expected = {p for d, pairs in self._registry().items()
                    if d in DERIBIT_ANOMALY_DOMAINS for p, _ in pairs}
        assert expected == set(DERIBIT_METRIC_PREFIXES), (
            f'prefix set drifted from the SQL: missing {expected - set(DERIBIT_METRIC_PREFIXES)}, '
            f'stale {set(DERIBIT_METRIC_PREFIXES) - expected}')

    def test_the_collector_writes_no_column_the_barred_list_omits(self):
        """options_iv_atm was written by the Deribit collector and absent from
        DERIBIT_COLUMNS, which is why OPTIONS_IV_<asset> was never barred."""
        cols = {c for pairs in self._registry().values() for _, c in pairs}
        from quant.collectors import deribit_options
        import inspect
        src = inspect.getsource(deribit_options)
        written = {c for c in cols if c in src}
        assert written <= set(DERIBIT_COLUMNS) | {'funding_rate_avg', 'long_short_ratio'}, (
            f'the Deribit collector writes {written - set(DERIBIT_COLUMNS)} '
            f'but DERIBIT_COLUMNS does not name it')
        assert 'options_iv_atm' in DERIBIT_COLUMNS

    def test_eth_metrics_are_barred_not_just_btc(self):
        """The leak. deribit_leadlag_metrics() registers only BTC variants."""
        from carrac.compliance.deribit_gate import is_deribit_anomaly
        for metric, domain in [('SKEW_25D_ETH', 'derivatives_advanced'),
                               ('GEX_ETH', 'derivatives_advanced'),
                               ('OPTIONS_IV_ETH', 'derivatives_options'),
                               ('SKEW_25D_BTC', 'derivatives_advanced')]:
            assert is_deribit_anomaly({'metric': metric, 'domain': domain}), \
                f'{metric} would be published'

    def test_a_barred_metric_is_caught_without_its_domain(self):
        """Belt-and-braces: a row whose domain is absent or renamed."""
        from carrac.compliance.deribit_gate import is_deribit_anomaly
        assert is_deribit_anomaly({'metric': 'GEX_ETH', 'domain': ''})
        assert is_deribit_anomaly({'metric': 'DVOL_SOL', 'domain': 'something_new'})

    def test_permitted_binance_and_bybit_rows_survive(self):
        """Over-gating silently drops data the licensing policy allows — equally a bug."""
        from carrac.compliance.deribit_gate import is_deribit_anomaly
        for metric, domain in [('FUNDING_BNB', 'derivatives_funding'),
                               ('LS_RATIO_AVAX', 'derivatives_ls'),
                               ('FUNDING_BTC_BYBIT', 'sentiment'),
                               ('COT_BTC_SPEC_NET', 'sentiment'),
                               ('PMI_CN', 'macro')]:
            assert not is_deribit_anomaly({'metric': metric, 'domain': domain}), \
                f'{metric} was dropped but is permitted'

    def test_cluster_is_filtered_and_its_counts_recomputed(self):
        """Leaving the counts would advertise exactly the withheld rows."""
        from carrac.compliance.deribit_gate import strip_gated_anomaly_cluster
        cluster = {
            'date': '2026-08-22',
            'extreme_count': 3,
            'domains_affected': 3,
            'metrics': [
                {'domain': 'derivatives_advanced', 'metric': 'SKEW_25D_ETH', 'zscore': -3.26},
                {'domain': 'derivatives_funding', 'metric': 'FUNDING_BNB', 'zscore': 4.93},
                {'domain': 'macro', 'metric': 'PMI_CN', 'zscore': -4.07},
            ],
        }
        out = strip_gated_anomaly_cluster(cluster)
        assert [m['metric'] for m in out['metrics']] == ['FUNDING_BNB', 'PMI_CN']
        assert out['extreme_count'] == 2, 'count still advertises the withheld row'
        assert out['domains_affected'] == 2
        assert cluster['extreme_count'] == 3, 'input was mutated'

    def test_both_return_sites_use_the_shared_rule(self):
        """Built but not connected is the defect class here."""
        import inspect
        from quant.api import routes
        inspect.getsource(routes)
        gsrc = inspect.getsource(routes._gate_snapshot)
        assert "out['anomaly_cluster'] = _strip_gated_cluster(" in gsrc
        assert "out['anomalies'] = _strip_gated_anomalies(" in gsrc
        import morning_engine.data_assembly as da
        dsrc = inspect.getsource(da)
        assert 'strip_gated_anomaly_cluster(snap[' in dsrc
        assert "strip_gated_anomalies(_named(snap.get('anomalies'), 'metric'))" in dsrc


class TestSnapshotCacheCannotBePoisoned:
    """The snapshot cache must not store a caller-specific payload.

    ``build_snapshot`` caches under ``snapshot:{asset}`` — no caller identity —
    in a Redis-backed cross-worker SharedTTLCache (ttl 300). Gating BEFORE the
    cache write stores whichever caller missed first. An admin missing first
    (precompute, an MCP call, the operator opening Pulse) puts the ungated
    payload in the shared entry, and the public /brief render serves it for the
    next five minutes.

    So the cache holds the UNGATED payload and caller identity is applied on
    the way out.
    """

    @staticmethod
    def _payload():
        return {
            'asset': 'BTC',
            'anomalies': [
                {'domain': 'derivatives_advanced', 'metric': 'SKEW_25D_ETH', 'value': -4.74},
                {'domain': 'derivatives_funding', 'metric': 'FUNDING_BNB', 'value': 0.0002},
            ],
            'anomaly_count': 2,
            'anomaly_cluster': {
                'extreme_count': 2, 'domains_affected': 2,
                'metrics': [
                    {'domain': 'derivatives_advanced', 'metric': 'GEX_ETH', 'zscore': 2.45},
                    {'domain': 'macro', 'metric': 'PMI_CN', 'zscore': -4.07},
                ],
            },
            'top_correlations': [], 'leadlag_signals': [],
            'leadlag_signals_weekly': [], 'chains': [],
        }

    def test_a_cached_admin_payload_is_still_gated_for_an_anonymous_reader(self,
                                                                          monkeypatch):
        """The poisoning scenario, end to end through the gate."""
        import carrac.compliance.deribit_gate as gate
        from quant.api.routes import _gate_snapshot
        monkeypatch.setattr(gate, 'deribit_visible', lambda: False)
        out = _gate_snapshot(self._payload())
        assert [a['metric'] for a in out['anomalies']] == ['FUNDING_BNB']
        assert out['anomaly_count'] == 1
        assert [m['metric'] for m in out['anomaly_cluster']['metrics']] == ['PMI_CN']

    def test_admin_still_sees_everything(self, monkeypatch):
        import carrac.compliance.deribit_gate as gate
        from quant.api.routes import _gate_snapshot
        monkeypatch.setattr(gate, 'deribit_visible', lambda: True)
        out = _gate_snapshot(self._payload())
        assert [a['metric'] for a in out['anomalies']] == ['SKEW_25D_ETH', 'FUNDING_BNB']

    def test_the_gate_does_not_mutate_the_cached_object(self, monkeypatch):
        """The cache holds this dict by reference — mutating it would gate the
        shared entry permanently, breaking admins instead of the public."""
        import carrac.compliance.deribit_gate as gate
        from quant.api.routes import _gate_snapshot
        monkeypatch.setattr(gate, 'deribit_visible', lambda: False)
        payload = self._payload()
        _gate_snapshot(payload)
        assert len(payload['anomalies']) == 2, 'the cached payload was mutated'
        assert payload['anomaly_cluster']['extreme_count'] == 2

    def test_what_is_cached_is_ungated_and_what_is_returned_is_gated(self):
        """The ordering itself — the defect was invisible in behaviour
        because both paths return a dict that looks right in isolation."""
        import inspect
        from quant.api import routes
        src = inspect.getsource(routes.build_snapshot)
        assert "return _gate_snapshot(cached)" in src, 'cache-hit path is ungated'
        assert "_qe_cache.set(cache_key, result)\n        return _gate_snapshot(result)" in src
        assert "'anomalies': anomalies," in src, 'a gated list is being cached'
        assert "'chains': chains," in src
        assert "_strip_gated_anomalies(anomalies)" not in src, \
            'gating moved back before the cache write'


class TestProsePatternsAgainstChartLanguage:
    """Prose patterns must not match chart-indicator language.

    Measured against 350 archived reports (279 genuine options matches, 12
    chart-indicator false positives for the flip pattern; 115/17 for term
    structure). The false positives were not cosmetic: the render-time scrub
    would delete
    legitimate PSAR/SuperTrend sentences from reader-facing afternoon reports,
    and the write-time alarm would cry wolf on every one.
    """

    def _scrub(self, text):
        from carrac.compliance.deribit_gate import scrub_deribit_prose
        return scrub_deribit_prose(text)

    def _alarm_hits(self, text):
        from carrac.compliance.deribit_gate import DERIBIT_PROSE_PATTERNS
        import re
        return sorted(k for k, p in DERIBIT_PROSE_PATTERNS.items()
                      if re.search(p, text, re.IGNORECASE))

    # -- chart-indicator language must survive (all verbatim from production reports)

    def test_psar_flip_level_is_kept(self):
        s = ("Gold sits between the AutoFib uptrend base ($4,448) and the "
             "PSAR short flip level ($4,671). The daily PSAR flipped short.")
        assert self._scrub(s) == s
        assert self._alarm_hits(s) == []

    def test_supertrend_flip_point_is_kept(self):
        s = "BTC is compressing below the 4H SuperTrend flip point at $65,934."
        assert self._scrub(s) == s
        assert self._alarm_hits(s) == []

    def test_signal_flip_level_is_kept(self):
        s = ("Worth monitoring for a daily close above the signal flip level "
             "with volume confirmation.")
        assert self._scrub(s) == s

    def test_coin_flip_level_is_kept(self):
        s = "SPX sitting on the monthly pivot — this is a coin-flip level."
        assert self._scrub(s) == s

    def test_short_term_structure_is_kept(self):
        s = ("A break below $77,216 (4H SuperTrend) on elevated volume would "
             "shift the short-term structure.")
        assert self._scrub(s) == s
        assert self._alarm_hits(s) == []

    def test_near_and_medium_term_structure_are_kept(self):
        for q in ("near-term", "medium-term", "long-term", "short term"):
            s = f"The {q} structure remains constructive."
            assert self._scrub(s) == s, q

    # -- genuine Deribit narration must still be caught (guards)

    def test_gex_flip_point_still_scrubbed(self):
        s = ("Dealers are in long-gamma above the $79,759 GEX flip point, "
             "actively dampening moves.")
        assert "GEX flip point" not in self._scrub(s)
        assert "gex_flip_price" in self._alarm_hits(s)

    def test_gamma_flip_still_scrubbed(self):
        s = "The further price moves from the gamma flip, the more dealers hedge."
        assert "gamma flip" not in self._scrub(s)

    def test_bare_term_structure_still_scrubbed(self):
        s = "The term structure is in contango (7d=36% vs 90d=40%)."
        assert "term structure" not in self._scrub(s)
        assert "term_structure_slope" in self._alarm_hits(s)

    def test_week_ahead_leak_fragment_still_caught(self):
        # verbatim shape of a week_ahead leak
        s = ("Skew moved from -1.9 to +3.21, while IV collapsed from 40.03 to "
             "34.82 and GEX dropped from 335M to 192M.")
        hits = self._alarm_hits(s)
        assert "iv_30d" in hits and "gex_net" in hits


class TestWeekAheadBriefingHasNoDerivatives:
    """The week_ahead pipeline must not build a quant.derivatives trajectory
    block (skew_25d, iv_30d, gex_net...): it would walk around the licensing
    gate. The write-time alarm is the backstop."""

    def test_builder_never_emits_a_derivatives_section(self):
        from morning_engine.data_assembly import build_week_ahead_shared_briefing
        ctx = {
            'catalyst_calendar': '| Date | Event |',
            'setup_watch': '- BTC: STRONG_LONG',
            'derivatives_trajectory': 'skew=-1.9 iv30=40.03 gex=335M',
        }
        joined = "\n".join(build_week_ahead_shared_briefing(ctx))
        assert 'Derivatives Trajectory' not in joined
        assert 'skew' not in joined

    def test_assembly_no_longer_queries_quant_derivatives(self):
        import inspect
        from morning_engine import data_assembly
        src = inspect.getsource(data_assembly.assemble_week_ahead_context)
        assert 'quant.derivatives' not in src
        assert 'skew_25d' not in src

    def test_week_ahead_prompt_carries_the_bar(self):
        from morning_engine.prompts import week_ahead_shared_prompt
        p = week_ahead_shared_prompt()
        assert 'OPTIONS-DATA-ABSENT' in p
        assert 'derivatives regime signal' not in p
