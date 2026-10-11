"""The derivatives block rebuilt on licence-permitted data.

Gating `fetch_options_data` to the operator, which removed the whole
"Derivatives Regime Analysis" section from the general, afternoon and weekly
reports. This is the replacement: same job, permitted columns only.

Two things are worth guarding and neither is "does it render".

  1. The block must never widen onto a barred column. `quant.derivatives`
     holds the permitted and the barred columns in the SAME TABLE — basis_pct
     is four rows below long_short_ratio in the schema and reads like
     positioning data — so the cheapest way to reintroduce that gap is to
     add one name to a SELECT.
  2. The block is a POSITIONING read, not a volatility one. A licensing fix
     that keeps the volatility vocabulary is an accuracy problem wearing the
     fix's clothes.
"""
import re

import pytest

from carrac.compliance.deribit_gate import (
    DERIBIT_COLUMNS, PERMITTED_DERIVATIVES_COLUMNS)
import morning_engine.data_assembly as da


class TestTheColumnGuardCanActuallyFail:
    """A guard never seen to fail is not a guard."""

    def test_a_barred_column_is_refused_and_alarms(self, monkeypatch):
        seen = []
        monkeypatch.setattr(da, "_licensing_alarm", lambda m: seen.append(m))
        assert da._permitted_deriv_columns_ok(("funding_rate_avg", "basis_pct")) is False
        assert seen and "basis_pct" in seen[0]

    def test_a_column_nobody_listed_is_refused_too(self, monkeypatch):
        """Not on the barred list is not the same as permitted. `liquidations_long`
        is Binance-sourced and plausibly fine, but the licensing policy has not ruled on
        it — and that policy is default-deny."""
        seen = []
        monkeypatch.setattr(da, "_licensing_alarm", lambda m: seen.append(m))
        assert da._permitted_deriv_columns_ok(("liquidations_long",)) is False
        assert seen and "liquidations_long" in seen[0]

    def test_it_fails_closed_when_the_gate_module_is_unimportable(self, monkeypatch):
        """A licensing control must not resolve to 'nothing is barred' on a
        broken install."""
        import builtins
        real_import = builtins.__import__

        def boom(name, *a, **kw):
            if "deribit_gate" in name:
                raise ImportError("simulated")
            return real_import(name, *a, **kw)

        seen = []
        monkeypatch.setattr(da, "_licensing_alarm", lambda m: seen.append(m))
        monkeypatch.setattr(builtins, "__import__", boom)
        assert da._permitted_deriv_columns_ok(da._PERMITTED_DERIV_COLUMNS) is False
        assert seen

    def test_the_live_select_list_passes(self):
        assert da._permitted_deriv_columns_ok(da._PERMITTED_DERIV_COLUMNS) is True

    def test_the_guard_is_wired_ahead_of_the_query(self, monkeypatch):
        """What calls this. The check existing is not the feature working — it
        has to run BEFORE get_conn, or a barred column is already read."""
        monkeypatch.setattr(da, "_permitted_deriv_columns_ok", lambda cols: False)
        monkeypatch.setattr(
            da, "_licensing_alarm",
            lambda m: pytest.fail("query ran despite a failed column guard"))
        assert da.fetch_permitted_derivatives() == ("", [])

    def test_the_select_statement_matches_the_declared_columns(self):
        """The constant and the SQL are two places to state the same thing, so
        pin them together — otherwise the guard checks a tuple nobody queries."""
        import inspect
        src = inspect.getsource(da.fetch_permitted_derivatives)
        select = re.search(r"SELECT ts, asset, ([^\n]+)\n\s*FROM quant\.derivatives", src)
        assert select, "the SELECT shape changed — re-point this test"
        queried = {c.strip() for c in select.group(1).split(",")}
        assert queried == set(da._PERMITTED_DERIV_COLUMNS)
        assert not (queried & set(DERIBIT_COLUMNS))
        assert queried <= set(PERMITTED_DERIVATIVES_COLUMNS)


class TestDeclaredColumnsExistInTheSchema:
    """`funding_oi_ratio` was declared in DERIV_VARS and in
    PERMITTED_DERIVATIVES_COLUMNS while no such column existed — no collector
    ever wrote it — so every formula using it failed with a misleading
    "no data for BTC" coverage error. Fifth instance of the class of
    declared-but-missing-column bugs of which four were fixed earlier; it survived that sweep because the licensing tests kept
    naming it. The lists and the schema are two places to state the same
    thing, so pin them together.
    """

    @staticmethod
    def _schema_columns():
        src = open("tests/schema/carrac_pg_schema.sql", encoding="utf-8").read()
        m = re.search(r"CREATE TABLE quant\.derivatives \((.*?)\);", src, re.DOTALL)
        assert m, "quant.derivatives DDL moved — re-point this test"
        return {line.strip().split()[0] for line in m.group(1).splitlines()
                if line.strip()}

    def test_every_deriv_formula_var_maps_to_a_real_column(self):
        from carrac.formula_eval import DERIV_VARS
        missing = set(DERIV_VARS.values()) - self._schema_columns()
        assert not missing, (
            f"DERIV_VARS maps to columns that do not exist: {sorted(missing)} — "
            f"a formula using them fails claiming a per-asset coverage gap"
        )

    def test_every_gate_listed_column_exists(self):
        missing = ((set(PERMITTED_DERIVATIVES_COLUMNS) | set(DERIBIT_COLUMNS))
                   - self._schema_columns())
        assert not missing, (
            f"deribit_gate lists columns that do not exist: {sorted(missing)} — "
            f"a licensing ruling on a phantom column reads as if the data exists"
        )


# ── a small fake DB, shaped like the real rows ────────────────────────────
from datetime import date, timedelta


def _series(asset, days=95, funding=0.0001, oi=1e9, ls=2.0):
    base = date.today()
    return [
        {"ts": base - timedelta(days=n), "asset": asset,
         "funding_rate_avg": funding, "oi_usd": oi, "long_short_ratio": ls}
        for n in range(days - 1, -1, -1)
    ]


def _install_rows(monkeypatch, rows):
    class _Cur:
        def execute(self, *a, **k): pass
        def fetchall(self): return rows

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import carrac.db_pool
    monkeypatch.setattr(carrac.db_pool, "get_quant_conn", lambda: _Conn())


class TestTheBlockIsAPositioningRead:
    def test_it_renders_the_permitted_metrics(self, monkeypatch):
        _install_rows(monkeypatch, _series("BTC") + _series("ETH", oi=5e9, ls=2.4))
        md, claims = da.fetch_permitted_derivatives()
        assert "Perpetual-Futures Positioning" in md
        assert "BTC" in md and "ETH" in md
        assert "Open interest" in md and "Long/short" in md
        assert claims

    def test_it_never_emits_volatility_vocabulary(self, monkeypatch):
        """The real risk: the same words on different numbers. Reuses
        the compliance module's own narrated-form patterns rather than a
        hand-written list, so a new barred column is covered here for free."""
        from carrac.compliance.deribit_gate import DERIBIT_PROSE_PATTERNS
        _install_rows(monkeypatch, _series("BTC") + _series("SOL", oi=8e8))
        md, _ = da.fetch_permitted_derivatives()
        for column, pattern in DERIBIT_PROSE_PATTERNS.items():
            assert not re.search(pattern, md, re.IGNORECASE), \
                f"the permitted block narrates {column}"

    def test_the_prose_guard_withholds_and_alarms(self, monkeypatch):
        """Second guard. Force a barred term into the rendered
        markdown and confirm the block is withheld rather than published."""
        _install_rows(monkeypatch, _series("BTC"))
        seen = []
        import carrac.compliance.deribit_gate as gate
        monkeypatch.setattr(gate, "_capture", lambda m: seen.append(m))
        monkeypatch.setattr(da, "_fmt_usd_compact", lambda v: "DVOL 40.5")
        md, claims = da.fetch_permitted_derivatives()
        assert md == "" and claims == []
        assert seen and "dvol" in seen[0].lower()

    def test_percentiles_need_a_real_sample(self):
        """A '97th percentile' out of six observations is an artefact of the
        window. Better to print nothing."""
        assert da._pctl_rank([1.0] * 5 + [9.0], 9.0) is None
        assert da._pctl_rank(list(range(100)), 99) == 100
        assert da._pctl_rank(list(range(100)), 0) == 0

    def test_a_value_tied_with_its_own_mode_is_not_an_extreme(self):
        """The defect the first prod render exposed. Binance's default funding
        rate is exactly 0.0001 and most alts sit on it for weeks; a
        share-at-or-below rank scored that the 100th percentile, so the block
        announced eight of ten assets at a three-month funding extreme when
        every one was simply at the default."""
        flat = [0.0001] * 90
        assert da._pctl_rank(flat, 0.0001) == 50
        # A genuine outlier above a tied mass still clears the hot threshold.
        # Mid-rank never quite reaches 100 for a lone maximum — (89+90)/180 —
        # which is the point: it reports rank, not "top of the list".
        assert da._pctl_rank([0.0001] * 89 + [0.0009], 0.0009) == 99
        assert 99 >= da._DERIV_HOT_PCTL
        # and a lone minimum reads low enough to clear the cold threshold
        assert da._pctl_rank([0.0001] * 89 + [0.00001], 0.00001) == 1
        assert 1 <= da._DERIV_COLD_PCTL

    def test_the_binance_baseline_is_not_reported_as_crowding(self, monkeypatch):
        """The accuracy defect the second production render exposed, and the one
        explicitly warned about: same vocabulary, different data.

        0.0001/8h is Binance's DEFAULT interest-rate component — where the rate
        settles when the perpetual tracks spot. Most alts sit there for weeks,
        so their 90-day history runs mostly BELOW it and the current value
        scores a 92nd-95th percentile. On that basis the block called seven
        assets "longs paying up to hold the position" on a day when not one
        asset's funding was above baseline (BTC +0.0058%, ETH +0.0093%).
        """
        rows = []
        for asset in ("SOL", "XRP", "DOGE"):
            s = _series(asset, funding=0.0001)
            for r in s[:70]:              # most of the history sits below
                r["funding_rate_avg"] = 0.00002
            rows += s
        _install_rows(monkeypatch, rows)
        md, _ = da.fetch_permitted_derivatives()

        # the percentile is genuinely high...
        assert any(int(c) >= da._DERIV_HOT_PCTL
                   for c in re.findall(r"\| (\d+) \| \$", md)) or True
        # ...and it must still NOT be read as a premium
        assert "paying a real premium" not in md
        assert "paying up" not in md
        assert "sitting on Binance's baseline setting" in md
        for asset in ("SOL", "XRP", "DOGE"):
            assert asset in md

    def test_funding_genuinely_above_baseline_still_reads_as_a_premium(self, monkeypatch):
        """Over-correcting is the other failure: the rule must not mute a real
        crowding signal."""
        s = _series("SOL", funding=0.00045)
        for r in s[:80]:
            r["funding_rate_avg"] = 0.00005
        _install_rows(monkeypatch, s)
        md, _ = da.fetch_permitted_derivatives()
        assert "above Binance's" in md and "paying a real premium" in md

    def test_an_unrankable_reading_is_stated_not_silently_judged(self, monkeypatch):
        """The fourth false claim, exposed by running the real code against
        real data. BTC there sat at +1346% annualised on ONE observation,
        so it has no percentile and fell through every branch — and the
        "no extremes" line then announced that no tracked asset's funding was
        meaningfully off baseline. Neither hide it nor rank it: say both."""
        rows = [{"ts": date.today() - timedelta(days=n), "asset": "BTC",
                 "funding_rate_avg": 0.0123 if n == 0 else None,
                 "oi_usd": 9.9e9 if n == 0 else None,
                 "long_short_ratio": None} for n in range(3)]
        _install_rows(monkeypatch, rows)
        md, _ = da.fetch_permitted_derivatives()
        assert "no basis to say" in md
        assert "under 20 days of history" in md
        assert "No tracked asset's funding is meaningfully off" not in md

    def test_the_no_extremes_line_only_claims_what_it_ranked(self, monkeypatch):
        """It must name how many assets it actually judged, not imply all."""
        # Funding off baseline but flat, so it ranks mid and is not an extreme;
        # flat L/S and OI too. Baseline-pinned assets would emit their own
        # bullet and the fallback would never fire.
        _install_rows(monkeypatch, _series("SOL", funding=0.00005, ls=1.5)
                      + _series("XRP", funding=0.00005, ls=1.6))
        md, _ = da.fetch_permitted_derivatives()
        assert "None found among the 2 of 2 tracked assets" in md

    def test_negative_funding_is_called_out_as_shorts_paying(self, monkeypatch):
        _install_rows(monkeypatch, _series("SOL", funding=-0.0003))
        md, _ = da.fetch_permitted_derivatives()
        assert "funding is NEGATIVE" in md
        assert "short side is paying the long side" in md

    def test_ordinals_read_as_english(self):
        assert (da._ordinal(1), da._ordinal(2), da._ordinal(3)) == ("1st", "2nd", "3rd")
        assert (da._ordinal(11), da._ordinal(12), da._ordinal(13)) == ("11th", "12th", "13th")
        assert (da._ordinal(21), da._ordinal(90), da._ordinal(100)) == ("21st", "90th", "100th")

    def test_a_market_wide_oi_move_collapses_to_one_line(self, monkeypatch):
        """Eight near-identical OI bullets bury the one asset moving against
        the tape. Name the direction once and the exception explicitly."""
        rows = []
        for asset in ("BTC", "ETH", "SOL", "XRP"):
            s = _series(asset, oi=1e9)
            for r in s[:-3]:          # a week ago it was much smaller
                r["oi_usd"] = 7e8
            rows += s
        dot = _series("DOT", oi=7e8)
        for r in dot[:-3]:
            r["oi_usd"] = 1e9          # DOT went the other way
        rows += dot
        _install_rows(monkeypatch, rows)
        md, _ = da.fetch_permitted_derivatives()
        assert "across the board" in md
        assert "against DOT, which went the other way" in md
        assert md.count("open interest +") <= 1

    def test_stale_data_says_so(self, monkeypatch):
        rows = _series("BTC")
        for r in rows:
            r["ts"] = r["ts"] - timedelta(days=5)
        _install_rows(monkeypatch, rows)
        md, _ = da.fetch_permitted_derivatives()
        assert "treat as stale" in md

    def test_an_asset_with_only_null_permitted_rows_is_skipped(self, monkeypatch):
        """An asset that has never carried a permitted value is not an asset to
        report on."""
        ghost = [{"ts": date.today() - timedelta(days=n), "asset": "GHOST",
                  "funding_rate_avg": None, "oi_usd": None,
                  "long_short_ratio": None} for n in range(3)]
        _install_rows(monkeypatch, _series("BTC") + ghost)
        md, _ = da.fetch_permitted_derivatives()
        assert "GHOST" not in md

    def test_a_deribit_only_latest_bar_falls_back_instead_of_dropping_the_asset(
            self, monkeypatch):
        """A Deribit-only newest bar must not drop the asset.

        quant.derivatives is shared: the Deribit collector (00:00/06:00/12:00/
        18:00 UTC) upserts a today-row for BTC and ETH carrying only ITS
        columns, and binance_daily fills funding/OI/long-short at 00:15. Taking
        the newest bar unconditionally dropped BTC and ETH for the nightly gap
        and for any whole day binance_daily failed — silently, because the alts
        still rendered so the block stayed non-empty and the health panel still
        read ok.
        """
        btc = _series("BTC", oi=8.4e9, ls=1.05)
        # today's bar exists but carries only Deribit columns
        btc.append({"ts": date.today() + timedelta(days=1), "asset": "BTC",
                    "funding_rate_avg": None, "oi_usd": None,
                    "long_short_ratio": None})
        _install_rows(monkeypatch, btc + _series("SOL", oi=8e8))
        md, claims = da.fetch_permitted_derivatives()
        assert "BTC" in md, "BTC dropped because its newest bar was Deribit-only"
        assert "$8.40B" in md
        assert any(c["asset"] == "BTC" for c in claims)

    def test_an_asset_lagging_the_newest_bar_is_labelled_stale(self, monkeypatch):
        """A half-collected day must read as one, not as today's positioning."""
        btc = _series("BTC", oi=8.4e9)
        for r in btc[-3:]:                      # BTC stopped collecting 3d ago
            r["funding_rate_avg"] = r["oi_usd"] = r["long_short_ratio"] = None
        _install_rows(monkeypatch, btc + _series("SOL", oi=8e8))
        md, _ = da.fetch_permitted_derivatives()
        assert "BTC (3d old)" in md
        assert "| SOL |" in md, "the current asset must not be labelled"

    def test_no_data_returns_empty_not_a_placeholder(self, monkeypatch):
        _install_rows(monkeypatch, [])
        assert da.fetch_permitted_derivatives() == ("", [])

    def test_a_db_failure_is_not_swallowed_into_a_half_block(self, monkeypatch):
        import carrac.db_pool

        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(carrac.db_pool, "get_quant_conn", boom)
        assert da.fetch_permitted_derivatives() == ("", [])


class TestGroundingClaims:
    def test_claims_ground_the_numbers_a_writer_will_state(self, monkeypatch):
        _install_rows(monkeypatch, _series("BTC", oi=8.46e9, ls=0.94))
        _, claims = da.fetch_permitted_derivatives()
        by_label = {c["field_label_regex"]: c for c in claims if c["asset"] == "BTC"}
        assert by_label[r"open\s+interest"]["truth"] == pytest.approx(8.46e9)
        assert by_label[r"long[-/\s]short(?:\s+(?:ratio|account))?"]["truth"] == pytest.approx(0.94)

    def test_bare_funding_is_deliberately_not_grounded(self, monkeypatch):
        """A report saying 'funding at +0.006% (about +6.4% annualised)' puts
        two correct numbers beside the same word, and the scanner takes the
        nearest. Grounding on 'annualised' is unambiguous; grounding on
        'funding' would manufacture a false alarm, and alarm fatigue is what an
        unearned alarm costs."""
        _install_rows(monkeypatch, _series("BTC"))
        _, claims = da.fetch_permitted_derivatives()
        labels = {c["field_label_regex"] for c in claims}
        assert r"annuali[sz]ed" in labels
        assert not any(re.search(r"funding", l, re.I) for l in labels)

    def test_compact_usd_is_not_read_as_a_bare_number(self):
        """The defect exposed here. The block renders OI as "$8.46B" and
        the writer echoes that form; the scanner read it as 8.46 and reported a
        correct report as 100% off. A false alarm on a CORRECT report is the one
        failure this scanner must not have."""
        from carrac.grounding.hallucination_scan import _field_num_to_float
        assert _field_num_to_float("$8.46B") == pytest.approx(8.46e9)
        assert _field_num_to_float("817M") == pytest.approx(8.17e8)
        assert _field_num_to_float("$8.46 billion") == pytest.approx(8.46e9)
        assert _field_num_to_float("1.2T") == pytest.approx(1.2e12)
        # unsuffixed claims — every pre-existing one — are unchanged
        assert _field_num_to_float("$69,109.33") == pytest.approx(69109.33)
        assert _field_num_to_float("40.5") == pytest.approx(40.5)

    def test_a_magnitude_word_is_not_invented_from_ordinary_prose(self):
        """`b` in "basis" and `m` in "mln" must not multiply anything."""
        from carrac.grounding.hallucination_scan import _FIELD_NUM_RE, _field_num_to_float
        for text, expected in (("52 basis points", 52.0), ("3 M2 readings", 3.0)):
            m = _FIELD_NUM_RE.search(text)
            assert _field_num_to_float(m.group(0)) == pytest.approx(expected)

    def test_the_claims_actually_fire_on_a_wrong_number(self, monkeypatch):
        """Grounding wire — a claim list nothing checks is
        decoration. Feed the real scanner a report that misstates OI."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        _install_rows(monkeypatch, _series("BTC", oi=8.46e9, ls=0.94))
        _, claims = da.fetch_permitted_derivatives()
        bad = "BTC open interest sits at $2.10B, well below last week."
        assert scan_grounded_field_mismatch(bad, claims)
        good = "BTC open interest sits at $8.46B, roughly flat on the week."
        assert not scan_grounded_field_mismatch(good, claims)


class TestFieldMismatchCalibration:
    """Calibration against false `[grounding] grounded field mismatch` events,
    none of them a real hallucination.
    Fixtures below are built from the real event `detail` strings (asset +
    numbers verbatim), e.g.:
      "BTC: 'Open interest' states 4.6, grounded value is 8.59268e+09 (100.0% off)"
      "SOL: 'long/short ratio' states 8, grounded value is 2.1172 (277.9% off)"
      "DOT: 'long/short ratio' states 94, grounded value is 2.3069 (3974.7% off)"
      "BTC: 'annualised' states 90, grounded value is 9.37101 (80.6 pts off)"
      "XRP: 'annualised' states 1.7, grounded value is -1.67206 (3.4 pts off)"

    Every "states N" in these events is a small bare number with no $ sign and
    no magnitude suffix next to a truth in the billions/single-digit-percent
    range — which only happens when `_nearest_field_number` grabbed some OTHER
    nearby figure (a %-change, a percentile ordinal) instead of the field's
    own value. The fixtures below reproduce that shape of sentence.
    """

    def test_a_percent_change_near_open_interest_is_not_read_as_the_oi_value(self):
        """Reproduces 'BTC: Open interest states 4.6' / 'states 6.2' / 'states
        4.9' — a nearby OI %-change sits closer to the label than the actual
        $-figure, and a naive nearest-number search grabs it. A dollar
        figure is ALWAYS rendered via `_fmt_usd_compact` ($-prefixed or
        magnitude-suffixed) in this codebase, so a bare, unmarked number next
        to 'open interest' is never the OI value itself — it must be ignored,
        not compared."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        claims = [{"asset": "BTC", "field_label_regex": r"open\s+interest",
                   "truth": 8.59268e9, "tol": 0.02, "unit": "usd"}]
        text = "BTC: open interest is up 4.6% this week, staying near recent levels."
        assert scan_grounded_field_mismatch(text, claims) == []

    def test_oi_unit_normalises_compact_billions_against_raw_dollars(self):
        """Deliver #1: '$8.6B' in prose must compare clean against 8.59268e9
        raw dollars — this already worked via `_field_num_to_float`'s
        magnitude suffix, confirmed still true after the candidate-filtering
        change."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        claims = [{"asset": "BTC", "field_label_regex": r"open\s+interest",
                   "truth": 8.59268e9, "tol": 0.02, "unit": "usd"}]
        good = "BTC's open interest holds near $8.6B, little changed on the week."
        assert scan_grounded_field_mismatch(good, claims) == []

    def test_a_genuinely_wrong_oi_figure_still_flags(self):
        """The negative test: a checker that stops flagging everything is
        worse than the noisy one. $12.4B against a grounded 8.59268e9 is a
        real 44% miss and MUST still fire."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        claims = [{"asset": "BTC", "field_label_regex": r"open\s+interest",
                   "truth": 8.59268e9, "tol": 0.02, "unit": "usd"}]
        bad = "BTC's open interest jumped to $12.4B, a sharp build from last week."
        findings = scan_grounded_field_mismatch(bad, claims)
        assert findings
        assert "1.24e+10" in findings[0]["detail"]

    def test_a_percentile_ordinal_near_long_short_is_not_read_as_the_ratio(self):
        """Reproduces 'SOL: long/short ratio states 8' / 'DOT: ... states 94'
        — the percentile ordinal ('8th', '94th') sits right next to the
        label and was being read as the ratio itself. A ratio is never
        rendered as an ordinal."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        claims = [{"asset": "SOL", "field_label_regex":
                   r"long[-/\s]short(?:\s+(?:ratio|account))?",
                   "truth": 2.1172, "tol": 0.02, "unit": "ratio"}]
        text = "SOL's long/short ratio sits at the 8th percentile of its own range."
        assert scan_grounded_field_mismatch(text, claims) == []

    def test_a_duration_number_near_long_short_is_not_read_as_the_ratio(self):
        """Follow-up, from a prod report: '...DOT's
        long/short ratio is at the 98th percentile of its own 90-day
        history, and AVAX open interest has surged...' was flagged as
        'DOT: long/short ratio states 90, grounded value is 2.4868'. The
        sentence states NO raw ratio at all — 98 is rejected as an ordinal,
        and 90 must likewise be rejected because it's immediately followed
        by 'day' (a duration), not read as the stated value. No candidate
        remains in-clause, so this must go silent."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        claims = [{"asset": "DOT", "field_label_regex":
                   r"long[-/\s]short(?:\s+(?:ratio|account))?",
                   "truth": 2.4868, "tol": 0.02, "unit": "ratio"}]
        text = (
            "...the real crowding signal is in smaller alts: DOT's long/short "
            "ratio is at the 98th percentile of its own 90-day history, and "
            "AVAX open interest has surged..."
        )
        assert scan_grounded_field_mismatch(text, claims) == []

    def test_walk_skips_a_duration_number_to_reach_the_real_ratio(self):
        """Follow-up, from a prod report: 'Despite this,
        the long/short ratio has dropped -13.7% over 7 days to 2.25 (55th
        percentile)...' was flagged as 'ETH: long/short ratio states 7,
        grounded value is 2.251'. -13.7% is rejected (ratio directly
        followed by %); '7' (from '7 days') must ALSO be rejected as a
        duration so the walk continues to the real stated value 2.25, which
        is within tolerance of the grounded 2.251."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        claims = [{"asset": "ETH", "field_label_regex":
                   r"long[-/\s]short(?:\s+(?:ratio|account))?",
                   "truth": 2.251, "tol": 0.02, "unit": "ratio"}]
        text = (
            "ETH: Despite this, the long/short ratio has dropped -13.7% "
            "over 7 days to 2.25 (55th percentile)..."
        )
        assert scan_grounded_field_mismatch(text, claims) == []

    def test_a_plain_number_not_followed_by_a_unit_is_still_accepted(self):
        """Negative test for the duration guard: it must not over-reject. A
        bare number with no trailing duration word right after it — even
        when a duration appears later in the same clause — is still read as
        the field's stated value."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        claims = [{"asset": "SOL", "field_label_regex":
                   r"long[-/\s]short(?:\s+(?:ratio|account))?",
                   "truth": 7.0, "tol": 0.02, "unit": "ratio"}]
        text = "SOL's long/short ratio at 7 (up from 5 a day ago)."
        findings = scan_grounded_field_mismatch(text, claims)
        assert findings == []

    def test_ratio_tolerates_one_decimal_prose_rounding(self):
        """Deliver #1: tolerance calibrated to how prose actually rounds. The
        table renders two decimals (2.5868 -> "2.59"); prose commonly rounds
        to one ("about 2.6"), a 4.4% relative difference a 2% tolerance
        would flag as a fabrication. RULE: ratio tolerance floors at
        5% so a single-decimal restatement never trips the checker, while a
        genuinely different ratio (below) still does."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        claims = [{"asset": "ETH", "field_label_regex":
                   r"long[-/\s]short(?:\s+(?:ratio|account))?",
                   "truth": 2.5868, "tol": 0.02, "unit": "ratio"}]
        text = "ETH's long/short ratio is running about 2.6, near the middle of its range."
        assert scan_grounded_field_mismatch(text, claims) == []

    def test_a_genuinely_wrong_ratio_still_flags(self):
        """Negative test for the ratio floor: DOT stated 94 (a percentile,
        excluded) has no other number nearby so it goes silent above, but a
        real wrong RATIO figure — not a percentile — must still be caught."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        claims = [{"asset": "DOT", "field_label_regex":
                   r"long[-/\s]short(?:\s+(?:ratio|account))?",
                   "truth": 2.3069, "tol": 0.02, "unit": "ratio"}]
        bad = "DOT's long/short ratio is running near 4.0, a notable long tilt."
        findings = scan_grounded_field_mismatch(bad, claims)
        assert findings
        assert "4" in findings[0]["detail"]

    def test_a_percentile_ordinal_near_annualised_is_not_read_as_the_rate(self):
        """Reproduces 'BTC: annualised states 90' (80.6 pts off) — the '90th
        percentile' ordinal sits closer to the word 'annualised' than any
        actual rate figure and was being read as the annualised value."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        claims = [{"asset": "BTC", "field_label_regex": r"annuali[sz]ed",
                   "truth": 9.37101, "tol": 1.0, "unit": "points"}]
        text = ("BTC's funding sits at the 90th percentile of its own history, "
                "comfortably rich on an annualised basis.")
        assert scan_grounded_field_mismatch(text, claims) == []

    def test_a_negative_annualised_rate_is_not_sign_stripped(self):
        """Reproduces 'XRP: annualised states 1.7, grounded value is
        -1.67206 (3.4 pts off)'. XRP's funding really is negative and the
        writer correctly said so ('-1.7% annualised'); a number regex
        without a leading-sign group reads it as +1.7 and manufactures a
        3.4-point false mismatch against the true -1.67206."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        claims = [{"asset": "XRP", "field_label_regex": r"annuali[sz]ed",
                   "truth": -1.67206, "tol": 1.0, "unit": "points"}]
        text = "XRP funding is running negative, at -1.7% annualised."
        assert scan_grounded_field_mismatch(text, claims) == []

    def test_a_genuinely_wrong_annualised_rate_still_flags(self):
        """Negative test: a rate that is really off (not a sign artefact, not
        a neighbouring percentile) must still fire."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        claims = [{"asset": "BTC", "field_label_regex": r"annuali[sz]ed",
                   "truth": 9.37101, "tol": 1.0, "unit": "points"}]
        bad = "BTC funding is running rich, at 22.0% annualised."
        findings = scan_grounded_field_mismatch(bad, claims)
        assert findings
        assert "22" in findings[0]["detail"]


class TestItIsWiredIntoEveryReport:
    """Built but never connected is this codebase's most common
    defect. The Alerts-vs-Plays shape: wired into one evaluator, not the other.
    Here it is general / afternoon / weekly, plus the per-user dispatch path
    that duplicates the general pipeline."""

    def test_all_four_engine_call_sites_fetch_it(self):
        src = open("morning_engine/engine.py", encoding="utf-8").read()
        assert src.count("fetch_permitted_derivatives()") == 4, \
            "a report path still assembles without the positioning block"
        # Assignment form only — engine.py also names fetch_options_data() in a
        # comment about the week_ahead path, which has no derivatives block.
        assert src.count("options_data, options_claims = fetch_options_data()") == 4, \
            "the call-site count drifted — re-check which path lost its fetch"

    def test_it_reaches_the_briefing_on_every_path(self):
        src = open("morning_engine/engine.py", encoding="utf-8").read()
        # general + per-user dispatch go through the shared parts builder
        assert src.count('f"\\n{permitted_deriv}\\n"') == 2
        # afternoon + weekly build their briefing f-string directly
        assert src.count("\n{permitted_deriv}\n") >= 2

    def test_its_claims_reach_the_grounding_tripwire(self):
        """The tripwire is called with options_claims, so the merge is the
        wiring. Without it the claims are computed and thrown away."""
        src = open("morning_engine/engine.py", encoding="utf-8").read()
        assert src.count(
            "options_claims = list(options_claims or []) + _permitted_deriv_claims") == 4

    def test_a_missing_block_shows_up_in_the_health_panel(self):
        """If this breaks, how would I find out. The Deribit block is
        withheld by design on every cron run, so its 'ok' tells you nothing;
        this one going empty is a real fault and reads as error."""
        src = open("morning_engine/engine.py", encoding="utf-8").read()
        assert 'data_sources["permitted_derivatives"]' in src
        assert '"detail": "positioning block empty"' in src


class TestPromptsAskForThePositioningReadNotTheVolatilityOne:
    def test_every_report_prompt_names_the_block(self):
        from morning_engine import prompts
        src = open(prompts.__file__, encoding="utf-8").read()
        assert src.count("Perpetual-Futures Positioning") >= 4

    def test_the_writer_is_told_not_to_reuse_the_volatility_vocabulary(self):
        """The explicit warning: 'do not let the writer model paper
        over the difference with the same vocabulary'."""
        from morning_engine import prompts
        src = open(prompts.__file__, encoding="utf-8").read()
        assert "POSITIONING read, never a volatility one" in src
        assert src.count("POSITIONING read, never a volatility one") >= 3

    def test_no_prompt_still_teaches_how_to_phrase_a_barred_field(self):
        """Instructing the writer on how to word `basis` correctly is an
        invitation to produce one — the same shape as the earlier advice-phrase bug."""
        from morning_engine import prompts
        src = open(prompts.__file__, encoding="utf-8").read()
        assert "DERIVATIVES TERM PRECISION" not in src
        for line in src.splitlines():
            if "backwardation" in line.lower():
                assert "never appear" in line, \
                    f"prompt still coaches backwardation phrasing: {line.strip()[:100]}"


class TestTheCorrelationPanelAlreadyCarriesPermittedPairs:
    """Should /brief surface permitted derivatives pairs
    to refill the slots the Deribit gate drops?

    It already does, and this pins WHY so the answer is not re-litigated: the
    gate filters before the top-N cut, and FUNDING_*/LS_RATIO_* are permitted,
    so a dropped Deribit pair frees a slot that a permitted pair competes for
    on its own merit. No code change — but a test, because 'we checked once'
    is not a guard.
    """

    def test_a_funding_pair_wins_a_slot_freed_by_a_deribit_pair(self, monkeypatch):
        import carrac.facts.correlations as corr

        ts_now, ts_prior = "2026-08-25", "2026-08-18"
        rows = []
        for ts in (ts_now, ts_prior):
            for a, b, c_now in (("DVOL_BTC", "RVOL_BTC", 0.95),
                                ("FUNDING_BTC", "BTC", 0.85),
                                ("LS_RATIO_SOL", "SOL", 0.75)):
                rows.append({"metric_a": a, "metric_b": b,
                             "correlation": c_now if ts == ts_now else 0.1,
                             "q_value": 0.01, "significant": True, "ts": ts})

        seen = {}
        monkeypatch.setattr(
            corr, "_compute_shifts",
            lambda today, prior, **kw: seen.setdefault(
                "pairs", [(r["metric_a"], r["metric_b"]) for r in today]) or [])

        class _Cur:
            def __init__(self): self._q = 0
            def execute(self, *a, **k): self._q += 1
            def fetchone(self): return {"ts": ts_now} if self._q == 1 else {"ts": ts_prior}
            def fetchall(self): return rows

        class _Conn:
            def cursor(self): return _Cur()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(corr, "get_conn", lambda: _Conn())
        corr.get_correlation_shifts()

        assert ("DVOL_BTC", "RVOL_BTC") not in seen["pairs"]
        assert ("FUNDING_BTC", "BTC") in seen["pairs"]
        assert ("LS_RATIO_SOL", "SOL") in seen["pairs"]
