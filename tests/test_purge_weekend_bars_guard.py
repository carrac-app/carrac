"""The purge script must not delete REAL weekend bars.

``scripts/purge_weekend_bars.py`` removes phantom weekend-dated rows from
weekday-only instruments. It exempted crypto by asking
``OHLCVDatabase._is_crypto`` / ``_is_crypto_metric``. That single check fails
in two whole classes:

  * the script recovers symbols from FILENAMES, and ``CSVCache.get_path``
    rewrites '.' to '_' — so it asked about ``BTC_D``, where
    ``_is_crypto_metric`` is False, while ``BTC.D`` is True. All three
    dominance series were unprotected;
  * ``_is_crypto`` is a live-universe lookup, and CRO, JST, DEXE, OKB, KCS,
    WBTC and ~20 more can drop out of it — real crypto, answered False.

Relying on that check alone, a dry run would propose deleting thousands of
legitimate rows — every FX major and all three dominance series — one
``--execute`` away.

So there is a second, independent exemption on the series' own weekend SHARE,
requiring BOTH to pass. These tests pin the separation that relies on.
"""
import numpy as np
import pandas as pd
import pytest

# The rule's canonical home is csv_cache — CSVCache.save (which
# drops phantom rows on the way in) and the purge script (which deletes
# accumulated ones) both import it from there, so it cannot drift between them.
from csv_cache import WEEKEND_TRADER_RATIO, trades_weekends


def _frame(index):
    return pd.DataFrame(
        {'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0, 'volume': 1.0},
        index=pd.DatetimeIndex(index),
    )


def _weekday_series(n=400, end='2026-08-07'):
    return pd.bdate_range(end=end, periods=n)


def _every_day(n=400, end='2026-08-07'):
    return pd.date_range(end=end, periods=n, freq='D')


class TestRealWeekendTradersAreExempt:
    """The 35 symbols the classifier missed. Ratios are the measured production
    values — if the guard stops covering these, history gets deleted."""

    @pytest.mark.parametrize('label,weekend_share', [
        ('BTC.D / USDT.D / OTHERS.D (dominance)', 0.287),
        ('CRO / JST / DEXE / OKB (delisted-but-real crypto)', 0.286),
        ('WBTC / METH / MNT / FDUSD', 0.285),
        ('EURUSD (FX, Sunday-evening open)', 0.1195),
        ('USDJPY / GBPUSD / AUDUSD (FX)', 0.125),
        ('SKY (lowest real series measured)', 0.140),
    ])
    def test_a_genuine_weekend_trader_is_not_purged(self, label, weekend_share):
        days = _every_day(2000)
        # Keep every weekday, plus enough weekend days to hit the share.
        weekdays = [d for d in days if d.weekday() < 5]
        weekends = [d for d in days if d.weekday() >= 5]
        want = int(len(weekdays) * weekend_share / (1 - weekend_share))
        idx = sorted(weekdays + weekends[:want])
        assert trades_weekends(_frame(idx)), (
            f'{label}: a real weekend-trading series would have been purged'
        )


class TestPhantomSeriesAreStillPurged:
    """The fix must not neuter the script — otherwise the phantom bars stay
    and Monday's published 1-bar change keeps being measured against a
    session that never happened."""

    @pytest.mark.parametrize('label,weekend,total', [
        ('DXY', 9, 10380),
        ('OIL', 11, 5944),
        ('GOLD / SILVER', 11, 3682),
        ('U', 2, 507),
        ('PLATINUM', 9, 568),
        ('COPPER / NATGAS', 11, 570),
        ('GT', 13, 562),
        ('GRAM (highest phantom measured)', 13, 552),
    ])
    def test_the_phantom_signature_is_not_exempt(self, label, weekend, total):
        weekdays = list(_weekday_series(total - weekend))
        extra = [d for d in _every_day(400) if d.weekday() >= 5][:weekend]
        assert not trades_weekends(_frame(sorted(weekdays + extra))), (
            f'{label}: the phantom bars would no longer be purged'
        )


class TestTheThresholdSitsInTheEmptyBand:
    def test_measured_populations_do_not_overlap(self):
        """0.0236 (worst phantom, GRAM) .. 0.1195 (best real, EURUSD).
        The threshold must sit strictly inside that gap, or one population
        gets misjudged."""
        assert 0.0236 < WEEKEND_TRADER_RATIO < 0.1195

    def test_a_pure_weekday_series_is_not_a_weekend_trader(self):
        assert not trades_weekends(_frame(_weekday_series(400)))

    def test_a_pure_247_series_is_a_weekend_trader(self):
        assert trades_weekends(_frame(_every_day(400)))


class TestRefuseWhenItCannotJudge:
    def test_a_short_series_is_treated_as_a_weekend_trader(self):
        """This code path DELETES. With too few rows to judge, the safe
        answer is 'exempt' — a phantom bar left in place is a wrong number;
        a wrongly purged series is lost history."""
        idx = list(_weekday_series(10)) + [pd.Timestamp('2026-08-02')]
        assert trades_weekends(_frame(sorted(idx)))

    def test_the_short_series_would_otherwise_look_like_a_phantom(self):
        """Proves the previous test is actually exercising the length guard
        and not just agreeing with the ratio."""
        idx = sorted(list(_weekday_series(10)) + [pd.Timestamp('2026-08-02')])
        weekend_share = np.mean([d.weekday() >= 5 for d in idx])
        assert weekend_share < WEEKEND_TRADER_RATIO
