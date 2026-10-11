"""A fresh tail must not hide a hole behind it.

`get_ohlcv_data`'s non-crypto path read ONLY `cached_df.index[-1]` and skipped the
fetch entirely while `days_old < 2`. So a weekday that failed to land was
welded in permanently: the check never looked further back than the last row.

Example: nine commodity/FX caches (COPPER EURHUF EURUSD GOLD NATGAS OIL
PLATINUM SILVER USDCHF) missing one weekday would make the free public /brief
publish "Gold surged +5.6% today to $4,326" off the 08-04 -> 08-06 pair, when
Gold had moved +0.72%.

Dates below are real: 2026-08-05 was a Wednesday, 2026-08-01/02 a weekend.
"""
import pandas as pd
import pytest

import database
from database import _missing_business_days, _GAP_LOOKBACK


def _df(dates):
    idx = pd.to_datetime(list(dates))
    return pd.DataFrame({"close": range(len(idx))}, index=idx)


# --- the detector -------------------------------------------------------

def test_the_real_gold_hole_is_detected():
    """A real GOLD cache tail (fixture dates) — 08-05 absent."""
    assert _missing_business_days(_df([
        "2026-07-30", "2026-07-31", "2026-08-03", "2026-08-04", "2026-08-06",
    ])) == 1


def test_a_complete_series_is_clean():
    """BTC's tail over the same window — the control that was fine."""
    assert _missing_business_days(_df([
        "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06",
    ])) == 0


def test_weekends_are_free():
    """Fri 07-31 -> Mon 08-03 must not look like a hole."""
    assert _missing_business_days(_df(["2026-07-31", "2026-08-03"])) == 0


def test_a_longer_hole_counts_every_missing_day():
    """Mon -> Fri with Tue/Wed/Thu absent."""
    assert _missing_business_days(_df(["2026-08-03", "2026-08-07"])) == 3


def test_only_the_recent_window_is_inspected():
    """An ancient hole is history, not something to refetch forever."""
    old = ["2026-01-05", "2026-03-02"]           # a huge gap, long ago
    recent = list(pd.bdate_range("2026-07-27", periods=_GAP_LOOKBACK + 1).strftime("%Y-%m-%d"))
    assert _missing_business_days(_df(old + recent)) == 0


@pytest.mark.parametrize("bad", [None, "not-a-frame", 42])
def test_malformed_input_never_raises(bad):
    """This sits on the price-lookup hot path; it must not break a chart."""
    assert _missing_business_days(bad) == 0


def test_single_row_is_not_a_gap():
    assert _missing_business_days(_df(["2026-08-06"])) == 0


def test_holiday_is_a_known_false_positive():
    """Documented cost, asserted so it is a decision and not a surprise.

    Distinguishing a public holiday from a real hole needs an exchange
    calendar this codebase does not have. A false positive costs ONE extra
    fetch per symbol per _GAP_REPAIR_TTL; a false negative costs a fabricated
    daily move on the public page. The trade is deliberate.
    """
    # 2026-01-01 was a Thursday: Wed 12-31 -> Fri 01-02 with the holiday between
    assert _missing_business_days(_df(["2025-12-31", "2026-01-02"])) == 1


# --- the backoff, which is what keeps a holiday from storming Stooq ------

def test_repair_is_attempted_once_per_symbol(monkeypatch):
    """Stooq caps ~50 requests/day. A holiday makes every symbol look gappy at
    once, so the second attempt inside the TTL must be suppressed."""
    store = {}

    class _FakeRedis:
        def get(self, k):
            return store.get(k)

        def set(self, k, v, ex=None):
            store[k] = v

    monkeypatch.setattr(database, "_get_cc_redis", lambda: _FakeRedis())

    assert database._is_gap_repair_backed_off("GOLD") is False
    database._set_gap_repair_attempted("GOLD")
    assert database._is_gap_repair_backed_off("GOLD") is True
    assert database._is_gap_repair_backed_off("SILVER") is False   # per-symbol


def test_backoff_fails_open_without_redis(monkeypatch):
    """No Redis must mean 'repair allowed', not 'repair blocked' — the same
    fail-open stance as the neighbouring rebuild backoff."""
    monkeypatch.setattr(database, "_get_cc_redis", lambda: None)
    database._set_gap_repair_attempted("GOLD")          # must not raise
    assert database._is_gap_repair_backed_off("GOLD") is False


# --- wiring -------------------------------------------------------------

def test_freshness_shortcut_consults_the_gap_check():
    """Built-but-not-connected is this codebase's most common defect."""
    import inspect
    src = inspect.getsource(database.OHLCVDatabase.get_ohlcv_data)
    assert "_missing_business_days(cached_df)" in src
    assert "_is_gap_repair_backed_off(symbol)" in src


def test_repair_fetch_reaches_behind_the_hole():
    """Refetching from the tail alone would never fill an interior gap."""
    import inspect
    src = inspect.getsource(database.OHLCVDatabase.get_ohlcv_data)
    assert "fetch_from = cached_df.index[-(_GAP_LOOKBACK + 1)]" in src


# --- repair OUTCOMES ---------------------------
#
# Measured on real data: five FX pairs stayed gapped for a full day while the repair
# had already run, and Stooq had the missing bar the whole time. The repair
# burned its 20h budget on a fetch that never reached upstream, and the only
# evidence it ran at all was a stdout warning that `make promote` destroyed.


class _FakeRedis:
    """Enough of the Redis surface for the backoff + outcome bookkeeping."""

    def __init__(self):
        self.store = {}
        self.ttls = {}
        self.hashes = {}

    def get(self, k):
        return self.store.get(k)

    def set(self, k, v, ex=None):
        self.store[k] = v
        self.ttls[k] = ex

    def setex(self, k, ttl, v):
        self.store[k] = v
        self.ttls[k] = ttl

    def hincrby(self, name, field, amount=1):
        self.hashes.setdefault(name, {})
        self.hashes[name][field] = self.hashes[name].get(field, 0) + amount


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(database, "_get_cc_redis", lambda: r)
    return r


def test_a_fetch_that_never_reached_upstream_retries_soon(fake_redis):
    """stooq.fetch returns an EMPTY frame from
    _yahoo_fallback when the yfinance breaker is open — that is "no answer",
    not "upstream has no such bar". Holding the hole for 20h on that is what
    kept EURUSD/EURHUF/USDCHF gapped while the bar sat upstream."""
    database._set_gap_repair_attempted("EURUSD")
    assert fake_redis.ttls[database._gap_repair_key("EURUSD")] == database._GAP_REPAIR_TTL

    database._record_gap_repair_outcome("EURUSD", "nofetch")

    assert fake_redis.ttls[database._gap_repair_key("EURUSD")] == database._GAP_REPAIR_RETRY_TTL
    assert database._GAP_REPAIR_RETRY_TTL < database._GAP_REPAIR_TTL
    # Still backed off — the budget guarantee holds, the wait is just shorter.
    assert database._is_gap_repair_backed_off("EURUSD") is True


def test_upstream_genuinely_missing_the_bar_keeps_the_full_backoff(fake_redis):
    """A public holiday is indistinguishable from a hole without a trading
    calendar. Upstream WAS reached and does not have the bar, so retrying
    changes nothing — keep the full day."""
    database._set_gap_repair_attempted("GOLD")
    database._record_gap_repair_outcome("GOLD", "unfilled")
    assert fake_redis.ttls[database._gap_repair_key("GOLD")] == database._GAP_REPAIR_TTL


def test_a_successful_repair_keeps_the_full_backoff(fake_redis):
    """Nothing left to repair, so no reason to come back sooner."""
    database._set_gap_repair_attempted("SILVER")
    database._record_gap_repair_outcome("SILVER", "filled")
    assert fake_redis.ttls[database._gap_repair_key("SILVER")] == database._GAP_REPAIR_TTL


def test_outcomes_survive_a_container_recreate(fake_redis):
    """Repair outcomes must outlive a container recreate,
    because a stdout warning dies with the container. Outcomes go to Redis
    with a 7-day TTL, and a tally accumulates for whoever comes looking."""
    database._record_gap_repair_outcome("EURUSD", "nofetch")
    database._record_gap_repair_outcome("GBPUSD", "nofetch")
    database._record_gap_repair_outcome("GOLD", "filled")

    assert fake_redis.store["mcd:gap:outcome:EURUSD"] == "nofetch"
    assert fake_redis.ttls["mcd:gap:outcome:GOLD"] == 7 * 24 * 3600
    assert fake_redis.hashes["mcd:gap:outcomes"] == {"nofetch": 2, "filled": 1}


def test_outcome_recording_fails_open_without_redis(monkeypatch):
    monkeypatch.setattr(database, "_get_cc_redis", lambda: None)
    database._record_gap_repair_outcome("GOLD", "nofetch")   # must not raise


def test_the_repair_path_records_every_outcome():
    """Built-but-never-connected is this codebase's most common defect, and
    an outcome nobody records is the same as no outcome at all."""
    import inspect
    src = inspect.getsource(database.OHLCVDatabase.get_ohlcv_data)
    for outcome in ("'filled'", "'unfilled'", "'nofetch'"):
        assert outcome in src, f"{outcome} is never recorded from the repair path"
    assert "_record_gap_repair_outcome" in src


def test_filled_vs_unfilled_is_decided_by_rechecking_the_series():
    """'Fetched some rows' is not 'the missing session arrived'. The outcome
    must come from re-running the detector on the MERGED frame, not from the
    fetch being non-empty."""
    import inspect
    src = inspect.getsource(database.OHLCVDatabase.get_ohlcv_data)
    assert "_missing_business_days(combined_df)" in src
