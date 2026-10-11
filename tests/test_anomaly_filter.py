"""Degenerate/dead-feed anomalies must be dropped from the feed."""
from datetime import datetime, timezone
from quant.anomaly_filter import is_degenerate_anomaly, filter_anomalies

NOW = datetime(2026, 6, 22, tzinfo=timezone.utc)


def test_extreme_zscore_is_degenerate():
    # FACTOR_LIQUIDITY -31.71σ — the junk row that ranked #1 in report #272.
    assert is_degenerate_anomaly({"zscore": -31.71, "date": "2026-05-26"}, now=NOW) is True


def test_stale_dead_feed_is_dropped():
    assert is_degenerate_anomaly({"zscore": 2.4, "date": "2026-04-01"}, now=NOW) is True  # 82d


def test_valid_monthly_macro_is_kept():
    # PPI: 11 days old, sane z — monthly data, must survive.
    assert is_degenerate_anomaly({"zscore": 2.21, "date": "2026-06-11"}, now=NOW) is False


def test_datetime_ts_key_supported():
    # build_snapshot rows carry an ISO 'date'; MCP rows too — but be robust to a
    # raw datetime under either key.
    fresh = datetime(2026, 6, 18, tzinfo=timezone.utc)
    assert is_degenerate_anomaly({"zscore": 3.0, "ts": fresh}, now=NOW) is False


def test_filter_preserves_order_and_drops_junk():
    rows = [
        {"metric": "FACTOR_LIQUIDITY", "zscore": -31.71, "date": "2026-05-26"},
        {"metric": "PPI", "zscore": 2.21, "date": "2026-06-11"},
        {"metric": "JAPAN_10Y", "zscore": 5.99, "date": "2026-06-15"},
    ]
    out = [r["metric"] for r in filter_anomalies(rows, now=NOW)]
    assert out == ["PPI", "JAPAN_10Y"]


# --- display staleness suffix ---------------------------

from quant.anomaly_filter import format_anomaly_date_suffix, DISPLAY_STALE_DAYS


def test_fresh_anomaly_gets_plain_date():
    # 2 days old — under the 7-day display threshold, no "old" callout.
    suffix = format_anomaly_date_suffix({"date": "2026-06-20"}, now=NOW)
    assert suffix == " (as of 2026-06-20)"


def test_stale_anomaly_gets_explicit_age_callout():
    # JAPAN_10Y-style case: a 2026-07-16 observation is 23 days stale as of
    # the fixed `now` below.
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    suffix = format_anomaly_date_suffix({"date": "2026-07-16"}, now=now)
    assert suffix == " (as of 2026-07-16 — 23d old)"


def test_exactly_at_threshold_is_not_flagged_stale():
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)  # exactly 7 days after 06-22
    suffix = format_anomaly_date_suffix({"date": "2026-06-22"}, now=now)
    assert "old" not in suffix
    assert suffix == " (as of 2026-06-22)"


def test_accepts_ts_key_when_date_is_absent():
    suffix = format_anomaly_date_suffix({"ts": "2026-06-20"}, now=NOW)
    assert suffix == " (as of 2026-06-20)"


def test_accepts_a_raw_date_value_not_just_a_dict():
    suffix = format_anomaly_date_suffix("2026-06-20", now=NOW)
    assert suffix == " (as of 2026-06-20)"


def test_missing_date_returns_empty_string_silently():
    assert format_anomaly_date_suffix({}, now=NOW) == ""
    assert format_anomaly_date_suffix(None, now=NOW) == ""


def test_unparseable_date_returns_empty_string():
    assert format_anomaly_date_suffix({"date": "not-a-date"}, now=NOW) == ""


def test_default_stale_threshold_is_seven_days():
    assert DISPLAY_STALE_DAYS == 7
