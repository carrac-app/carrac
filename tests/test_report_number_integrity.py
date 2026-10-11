"""Published numbers must agree with themselves and with their source.

Four defects, fixed as one sweep because they are one
failure: a figure reaches the reader that was computed somewhere other than
where the reader thinks. The four differ only in where the second version
came from — a second algorithm, an unfiltered fallback, a later snapshot, or
the model's own arithmetic.

These are mostly CONTAINMENT tests. Nothing crashes when a report prints a
wrong percentage, so there is no behaviour to assert on; the only symptom is
a human reading a false number on the public brief. That makes the guard's
job to stop the shape from coming back, not to check a return value.
"""

import re
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────────────────────
# One RSI, not three
# ─────────────────────────────────────────────────────────────

class TestDailyRSIPair:
    def _series(self, values):
        idx = pd.date_range('2026-01-01', periods=len(values), freq='D')
        return pd.Series(values, index=idx, dtype='float64')

    def test_agrees_with_the_canonical_calculator(self):
        """The whole point: the crossing is judged on the number the page
        prints, which is Wilder's RSI from consensio/indicators/rsi.py."""
        from carrac.reports.routes import _daily_rsi_pair
        from consensio.indicators.rsi import RSICalculator

        close = self._series([100 + (i % 7) - (i % 3) * 2 for i in range(120)])
        _prev, cur = _daily_rsi_pair(close)
        canonical = RSICalculator(14).calculate(pd.DataFrame({'close': close}))
        assert cur == canonical

    def test_differs_from_the_sma_version_that_shipped_the_false_claim(self):
        """If these two ever agree the test proves nothing, so assert they
        genuinely diverge — SMA-RSI is a different indicator."""
        from carrac.reports.routes import _daily_rsi_pair

        # A downtrend with real two-way movement: both gains and losses must
        # be non-zero in the final window or the SMA formula degenerates to
        # 100 and the comparison proves nothing.
        close = self._series([100 - i * 0.5 + (3.0 if i % 3 == 0 else -1.0)
                              for i in range(120)])
        _prev, cur = _daily_rsi_pair(close)

        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        assert gain.iloc[-1] > 0 and loss.iloc[-1] > 0, "degenerate fixture"
        sma_rsi = round(100 - (100 / (1 + gain.iloc[-1] / loss.iloc[-1])), 1)

        assert cur != sma_rsi, (
            f"Wilder and SMA agreed ({cur}) on this fixture — the fixture is "
            f"not discriminating, not the implementations identical"
        )

    def test_both_ends_come_from_one_series(self):
        """Previous must not be the WEEKLY value from the snapshot
        (database.py stores asset['weekly_rsi']) while current is daily —
        two different timeframes could manufacture a crossing."""
        from carrac.reports.routes import _daily_rsi_pair
        from consensio.indicators.rsi import RSICalculator

        close = self._series([100 + (i % 11) - (i % 5) for i in range(120)])
        prev, cur = _daily_rsi_pair(close)
        full = RSICalculator(14).calculate_series(pd.DataFrame({'close': close}))
        assert prev == round(float(full.iloc[-2]), 1)
        assert cur == round(float(full.iloc[-1]), 1)

    def test_short_series_is_none_not_a_guess(self):
        from carrac.reports.routes import _daily_rsi_pair
        assert _daily_rsi_pair(self._series([100.0] * 5)) == (None, None)
        assert _daily_rsi_pair(None) == (None, None)


class TestNoHandRolledRSISurvives:
    """No SMA-based RSI copy exists in the report code.

    A grep guard rather than a behaviour test, because the failure mode is
    someone adding another copy, which no unit test of the existing
    implementations can see. `rolling(14).mean()` over clipped deltas is SMA-RSI; the
    canonical Wilder implementation uses ewm/recursive smoothing.
    """

    SMA_RSI = re.compile(r"clip\(lower=0\)\.rolling\(14\)\.mean\(\)")

    @pytest.mark.parametrize("relpath", [
        "carrac/reports/routes.py",
        "carrac/charts/routes.py",
        "morning_engine/data_assembly.py",
        "morning_engine/signal_extractor.py",
    ])
    def test_no_sma_rsi_in_report_or_chart_paths(self, relpath):
        text = (REPO / relpath).read_text(encoding="utf-8")
        code = "\n".join(
            ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
        )
        assert not self.SMA_RSI.search(code), (
            f"{relpath} hand-rolls an SMA-based RSI. Use "
            f"consensio.indicators.rsi.RSICalculator — a second RSI means the "
            f"report can narrate a crossing the chart does not show."
        )


# ─────────────────────────────────────────────────────────────
# The fallback filters too
# ─────────────────────────────────────────────────────────────

class TestRSFallbackRegistryFilter:
    def test_non_registry_symbols_are_dropped(self, monkeypatch):
        import morning_engine.data_assembly as da
        import carrac.assets.registry as registry

        monkeypatch.setattr(registry, "public_symbol_allowlist", lambda: {"BTC", "SPX"})
        payload = {"assets": [{"symbol": "BTC"}, {"symbol": "TSLA"}, {"symbol": "SPX"}]}
        out = da._format_rs_registry_only(payload)
        assert "TSLA" not in out
        assert "BTC" in out and "SPX" in out

    def test_unreadable_registry_passes_through_rather_than_blanking(self, monkeypatch):
        """public_symbol_allowlist returns None (never an empty set) when the
        registry cannot be read; blanking the section would be a worse
        failure than an unfiltered one."""
        import morning_engine.data_assembly as da
        import carrac.assets.registry as registry

        monkeypatch.setattr(registry, "public_symbol_allowlist", lambda: None)
        payload = {"assets": [{"symbol": "BTC"}, {"symbol": "TSLA"}]}
        assert "BTC" in da._format_rs_registry_only(payload)

    def test_the_legacy_task_map_uses_the_filtered_formatter(self):
        """The defect was a bare _format_json_compact in the fallback task
        map — reachable only when preassembly fails, so no behaviour test
        exercises it in normal operation."""
        src = (REPO / "morning_engine/data_assembly.py").read_text(encoding="utf-8")
        line = next(ln for ln in src.splitlines()
                    if '"rs_rankings": ("/api/relative-strength"' in ln)
        assert "_format_rs_registry_only" in line, (
            "the legacy RS fallback lost its registry filter again"
        )


# ─────────────────────────────────────────────────────────────
# The GEX snapshot is stamped
# ─────────────────────────────────────────────────────────────

class TestDerivativesTimestampLabel:
    def test_naive_datetime_is_treated_as_utc(self):
        from datetime import datetime
        from morning_engine.data_assembly import _derivatives_ts_label
        assert _derivatives_ts_label(datetime(2026, 8, 5, 12, 31)) == "2026-08-05 12:31 UTC"

    def test_aware_datetime_is_converted_to_utc(self):
        from datetime import datetime, timedelta, timezone
        from morning_engine.data_assembly import _derivatives_ts_label
        ts = datetime(2026, 8, 5, 14, 31, tzinfo=timezone(timedelta(hours=2)))
        assert _derivatives_ts_label(ts) == "2026-08-05 12:31 UTC"

    def test_missing_value_yields_no_stamp_rather_than_a_guess(self):
        from morning_engine.data_assembly import _derivatives_ts_label
        assert _derivatives_ts_label(None) == ""
        assert _derivatives_ts_label("") == ""


# ─────────────────────────────────────────────────────────────
# The report must not contradict its own numbers
# ─────────────────────────────────────────────────────────────

class TestInternalArithmetic:
    """Measured against 60 stored production reports: 0 findings after tightening.

    Before the clause-break guard the only hit on that corpus was a false
    positive ("28 to 22 (Extreme Fear) while open interest rose +4.2%"),
    i.e. 100% noise — which is why this alarm notifies only in its tightened
    form.
    """

    @pytest.mark.parametrize("text,implied", [
        ("Oil fell from $84 to $71.90 (-22.8%) over the period.", -14.4),
        ("Oil 84 -> 71.90, a -22.8% slide", -14.4),
        ("Gold ran from 4,095 to 4,326 (+11.0%)", 5.6),
    ])
    def test_catches_a_percentage_its_own_prices_refute(self, text, implied):
        from carrac.grounding.hallucination_scan import scan_internal_arithmetic
        findings = scan_internal_arithmetic(text)
        assert findings, f"missed a real contradiction: {text}"
        assert f"{implied:+.1f}%" in findings[0]["detail"]

    @pytest.mark.parametrize("text", [
        "Oil fell from $84 to $71.90 (-14.4%) over the period.",
        "XLE rose from 91.20 to 94.12 (+3.2%)",
        "BTC moved 64,790 -> 69,109 (+6.7%)",
        "NATGAS went from 2.68 to 2.66, roughly -0.7% on the week",
    ])
    def test_correct_arithmetic_is_silent(self, text):
        from carrac.grounding.hallucination_scan import scan_internal_arithmetic
        assert scan_internal_arithmetic(text) == []

    @pytest.mark.parametrize("text", [
        "Fear and Greed slid 28 to 22 (Extreme Fear) while open interest rose +4.2%",
        "BTC ran 64,000 to 69,000 as funding flipped +0.9%",
        "Gold 4,095 to 4,326 with skew at +12.2%",
    ])
    def test_a_conjunction_means_the_percentage_is_about_something_else(self, text):
        """The real false positive this scanner produced in production data. A
        percentage on the far side of 'while'/'as'/'with' is a different
        subject, so the arithmetic was never claimed."""
        from carrac.grounding.hallucination_scan import scan_internal_arithmetic
        assert scan_internal_arithmetic(text) == []

    def test_rounding_in_prose_does_not_fire(self):
        from carrac.grounding.hallucination_scan import scan_internal_arithmetic
        assert scan_internal_arithmetic("Oil 84 -> 71.90 (-14%)") == []

    def test_the_tripwire_notifies_a_human(self):
        """Six grounding tripwires log to a host file nobody
        reads. This one must reach _notify, or a caught contradiction still
        ships unseen."""
        src = (REPO / "morning_engine/engine.py").read_text(encoding="utf-8")
        body = src.split("def _internal_arithmetic_tripwire", 1)[1].split("\ndef ", 1)[0]
        assert "_notify(" in body

    @pytest.mark.parametrize("label", [
        '"general report"', '"afternoon report"', '"weekly report"',
    ])
    def test_wired_into_every_report_path(self, label):
        """Found on the weekly; nothing about it is weekly-specific."""
        src = (REPO / "morning_engine/engine.py").read_text(encoding="utf-8")
        calls = [ln for ln in src.splitlines()
                 if "_internal_arithmetic_tripwire(" in ln and "def " not in ln]
        assert any(label in ln for ln in calls), f"not wired for {label}"


# ─────────────────────────────────────────────────────────────
# A stated field must agree with its own ground truth
#
# BTC's GEX flip point was published as 165,694 when quant.derivatives held
# 69,109.33 for that day (2.6x off) — a real, verified hallucination in a
# live, user-facing report. No existing scanner had a rule for this class:
# _confluence_tripwire is scoped to the Levels-in-Focus section only, and
# this claim sat in ordinary narrative prose.
# ─────────────────────────────────────────────────────────────

class TestGroundedFieldMismatch:
    """A literal report sentence, and the scoping rules that keep this
    scanner from becoming noise: a number belongs to a label only if it is
    in the same clause (the clause-break lesson) and the label's
    line names the claimed asset most recently (the confluence scanner's
    _asset_spans idiom) — otherwise the mismatch is left unflagged rather
    than guessed at.
    """

    # A report line as published.
    GEX_SENTENCE = (
        "BTC's DVOL is at 36 - cycle lows - with spot in the short-gamma "
        "zone below the GEX flip point at 165,694."
    )

    def _gex_claim(self, truth=69109.33, tol=0.02, asset="BTC"):
        return [{
            "asset": asset, "field_label_regex": r"GEX\s+flip\s+point",
            "truth": truth, "tol": tol, "unit": "usd",
        }]

    def test_catches_the_gex_flip_sentence_mismatch(self):
        """The exact sentence against the real gex_flip_price —
        must be exactly one finding, not zero (missed) and not several
        (a second scanner tripping on the same DVOL/other numbers nearby)."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        findings = scan_grounded_field_mismatch(self.GEX_SENTENCE, self._gex_claim())
        assert len(findings) == 1
        assert "BTC" in findings[0]["detail"]
        assert "type" in findings[0] and "detail" in findings[0]

    def test_correct_value_is_silent(self):
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        text = ("BTC's DVOL is at 36 - cycle lows - with spot in the "
                "short-gamma zone below the GEX flip point at 69,109.33.")
        assert scan_grounded_field_mismatch(text, self._gex_claim()) == []

    def test_number_in_a_different_clause_is_silent(self):
        """A conjunction ('while') between the label and the number means
        the number is about something else — the clause-break lesson reused
        here via the same _CLAUSE_BREAK_RE."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        text = "BTC's GEX flip point held firm while spot slid to 165,694."
        assert scan_grounded_field_mismatch(text, self._gex_claim()) == []

    @pytest.mark.parametrize("text", [
        "BTC's GEX flip point sits at $165,694 today.",
        "BTC's GEX flip point sits at 165,694 today.",
        "BTC's GEX flip point sits at 165694 today.",
    ])
    def test_comma_and_dollar_formats_are_all_caught(self, text):
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        findings = scan_grounded_field_mismatch(text, self._gex_claim())
        assert len(findings) == 1

    def test_asset_scoping_does_not_misattribute_across_assets(self):
        """ETH's own (correct) GEX flip number must not be checked against a
        BTC-scoped claim just because the label text matches."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        text = "ETH's GEX flip point sits at 3,400 today."
        assert scan_grounded_field_mismatch(text, self._gex_claim()) == []

    def test_point_tolerance_for_a_points_denominated_claim(self):
        """DVOL claims use an absolute point tolerance, not a fraction —
        2 points off must be silent (boundary), 4 points off must fire."""
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        claim = [{"asset": "BTC", "field_label_regex": r"\bDVOL\b",
                  "truth": 34.0, "tol": 2, "unit": "points"}]
        assert scan_grounded_field_mismatch("BTC's DVOL is at 36.", claim) == []
        findings = scan_grounded_field_mismatch("BTC's DVOL is at 38.", claim)
        assert len(findings) == 1

    def test_empty_or_missing_claims_is_a_noop(self):
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        assert scan_grounded_field_mismatch(self.GEX_SENTENCE, []) == []
        assert scan_grounded_field_mismatch(self.GEX_SENTENCE, None) == []
        assert scan_grounded_field_mismatch("", self._gex_claim()) == []

    def test_the_tripwire_is_non_blocking_and_reaches_sentry_not_notify(self):
        """The first version of this class of scanner has not yet
        been replayed against the stored prod corpus — the earlier clause-break
        scanner's first pass was 100% false positives on that corpus before the
        clause-break guard existed. So this one reaches Sentry for
        a human to eyeball, but does NOT go through _notify (ntfy/Telegram)
        yet."""
        src = (REPO / "morning_engine/engine.py").read_text(encoding="utf-8")
        body = src.split("def _grounded_field_tripwire", 1)[1].split("\ndef ", 1)[0]
        assert "capture_message(" in body
        assert "_notify(" not in body

    @pytest.mark.parametrize("label", [
        '"general report"', '"afternoon report"', '"weekly report"',
    ])
    def test_wired_into_every_report_path(self, label):
        src = (REPO / "morning_engine/engine.py").read_text(encoding="utf-8")
        calls = [ln for ln in src.splitlines()
                 if "_grounded_field_tripwire(" in ln and "def " not in ln]
        assert any(label in ln for ln in calls), f"not wired for {label}"

    def test_fetch_options_data_returns_a_markdown_claims_tuple(self, monkeypatch):
        """The gate-withheld path (the common case — cron
        runs have no admin caller) must still return the (markdown, claims)
        tuple shape, or every caller unpacking it raises."""
        import carrac.compliance.deribit_gate as gate
        monkeypatch.setattr(gate, "deribit_visible", lambda: False)
        from morning_engine.data_assembly import fetch_options_data
        markdown, claims = fetch_options_data()
        assert markdown == ""
        assert claims == []


class TestGroundedFieldReplayLessons:
    """A corpus replay produced false positives from two shapes. Each is pinned here; the real firing case stays live."""

    def _dvol_claim(self, truth=34.4):
        return [{"asset": "BTC", "field_label_regex": r"\bDVOL\b(?![\w\s]{0,12}[–—-]\s*Rvol)",
                 "truth": truth, "tol": 2.0, "unit": "points"}]

    def test_dvol_rvol_ratio_row_is_not_a_dvol_claim(self):
        text = "BTC lead-lag: Dvol Btc–Rvol Btc  0.45 → 0.74  (+0.29)"
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        assert scan_grounded_field_mismatch(text, self._dvol_claim()) == []

    def test_a_number_in_the_previous_sentence_does_not_bleed_in(self):
        # BTC must appear in the text: without it the asset-scoping rule makes
        # the scanner silent for a different reason and the test proves nothing.
        text = "BTC Fear & Greed reads 27. DVOL sits at cycle lows — positioning is complacent."
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        assert scan_grounded_field_mismatch(text, self._dvol_claim()) == []

    def test_a_colon_between_label_and_value_is_not_a_clause_break(self):
        text = "BTC options: DVOL: 55.0 stands out."
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        f = scan_grounded_field_mismatch(text, self._dvol_claim(truth=34.4))
        assert len(f) == 1  # the value after the colon is read, and it mismatches

    def test_a_decimal_point_is_not_a_clause_break(self):
        text = "BTC DVOL at 34.4 holding steady."
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        assert scan_grounded_field_mismatch(text, self._dvol_claim(truth=34.4)) == []

    def test_the_real_mismatch_still_fires_after_the_tightening(self):
        text = "BTC DVOL at 55.0 is elevated."
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        f = scan_grounded_field_mismatch(text, self._dvol_claim(truth=34.4))
        assert len(f) == 1
