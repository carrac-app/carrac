"""ROWS COLLAPSED fires forever on legitimate corrections.

The high-water mark is deliberately sticky, so a correction that REMOVES bogus
rows reads as data loss and pages every morning. Real daily findings of
this kind (3 of 33 in one run):

    FTM: ROWS COLLAPSED 2094 -> 1498   <- delisted; exemption covered STALE only
    PI:  ROWS COLLAPSED  714 ->  552   <- correct: 552 rows / 552-day span
    SKY: ROWS COLLAPSED  659 ->  343   <- correct: 343 rows / 343-day span

Two mechanisms fix it: the existing delisted exemption (Check F's) applied to
Check B too, and a curated one-shot baseline correction with author/date/note
(the LIFECYCLE_OVERRIDES shape) that replaces a known-inflated mark. The
correction is keyed to the exact inflated value so it can never clamp a mark
that later grows legitimately.
"""
from scripts.cache_integrity_check import (
    BASELINE_CORRECTIONS,
    rows_collapsed_findings,
)


class TestARealCollapseStillFires:
    def test_shrink_below_the_alert_share_is_flagged(self):
        baseline = {'BTC': 1000}
        f = rows_collapsed_findings({'BTC': 700}, baseline)
        assert f == ['BTC: ROWS COLLAPSED 1000 -> 700 (70% of high-water mark)']

    def test_the_mark_stays_sticky_after_a_collapse(self):
        baseline = {'BTC': 1000}
        rows_collapsed_findings({'BTC': 700}, baseline)
        assert baseline['BTC'] == 1000

    def test_growth_raises_the_mark(self):
        baseline = {'BTC': 1000}
        assert rows_collapsed_findings({'BTC': 1200}, baseline) == []
        assert baseline['BTC'] == 1200


class TestDelistedExemption:
    def test_a_delisted_symbols_trim_is_not_a_finding(self):
        # FTM's delisting trim already suppresses its STALE
        # finding; ROWS must follow the same curated decision.
        baseline = {'FTM': 2094}
        f = rows_collapsed_findings({'FTM': 1498}, baseline, delisted={'FTM'})
        assert f == []

    def test_no_store_means_no_exemption(self):
        # delisted=None is "cannot know", and the contract (see
        # delisted_from_records) is to fail LOUD: findings come back rather
        # than being silently suppressed.
        baseline = {'FTM': 2094}
        f = rows_collapsed_findings({'FTM': 1498}, baseline, delisted=None)
        assert len(f) == 1

    def test_a_live_symbols_collapse_is_not_masked_by_anothers_delisting(self):
        baseline = {'BTC': 1000}
        f = rows_collapsed_findings({'BTC': 700}, baseline, delisted={'FTM'})
        assert len(f) == 1


class TestCuratedRebaseline:
    def test_corrections_carry_provenance(self):
        for sym in ('PI', 'SKY'):
            corr = BASELINE_CORRECTIONS[sym]
            assert corr['author']
            assert corr['effective_date']
            assert corr['note']

    def test_a_corrected_mark_silences_the_false_positive(self):
        baseline = {'PI': 714}
        assert rows_collapsed_findings({'PI': 552}, baseline) == []
        # the mark is re-baselined and grows naturally from here
        assert baseline['PI'] == 552

    def test_a_correction_does_not_mask_a_further_real_collapse(self):
        baseline = {'PI': 714}
        f = rows_collapsed_findings({'PI': 300}, baseline)
        # judged against the CORRECTED mark, not the inflated one
        assert f == ['PI: ROWS COLLAPSED 552 -> 300 (54% of high-water mark)']

    def test_a_correction_is_inert_once_the_mark_has_moved_on(self):
        # If the series legitimately regrows past the corrected value, the
        # one-shot correction must not clamp the newer, larger mark.
        baseline = {'PI': 600}
        f = rows_collapsed_findings({'PI': 400}, baseline)
        assert f == ['PI: ROWS COLLAPSED 600 -> 400 (67% of high-water mark)']
