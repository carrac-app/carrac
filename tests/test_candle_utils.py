from morning_engine.candle_utils import volume_ratio_closed, tf_to_seconds

DAY = 86400
DAY_MS = DAY * 1000

def _series(values, last_open_ms, step_ms=DAY_MS):
    n = len(values)
    start = last_open_ms - (n - 1) * step_ms
    return [{"time": start + i * step_ms, "value": v} for i, v in enumerate(values)]

def test_last_candle_in_progress_is_dropped():
    # 21 closed days at vol 100, then today's in-progress candle at vol 5
    vals = [100.0] * 21 + [5.0]
    today_open = 1_000 * DAY_MS  # arbitrary day boundary
    now = today_open + 3600_000  # 1h into the in-progress candle
    series = _series(vals, last_open_ms=today_open)
    out = volume_ratio_closed(series, timeframe_seconds=DAY, now_ms=now, period=20)
    assert out["candle_complete"] is False
    assert out["used_index"] == len(series) - 2          # the last CLOSED candle
    assert abs(out["ratio"] - 1.0) < 0.001               # 100 / mean(prior 20 == 100)

def test_last_candle_already_closed_is_used():
    # last candle is Friday's completed session (open + 1 day < now) at vol 1600
    vals = [100.0] * 20 + [1600.0]
    fri_open = 1_000 * DAY_MS
    now = fri_open + 3 * DAY_MS  # Monday — Friday candle long closed
    series = _series(vals, last_open_ms=fri_open)
    out = volume_ratio_closed(series, timeframe_seconds=DAY, now_ms=now, period=20)
    assert out["candle_complete"] is True
    assert out["used_index"] == len(series) - 1
    assert abs(out["ratio"] - 16.0) < 0.001              # 1600 / 100

def test_missing_times_falls_back_to_last_index_no_crash():
    series = [{"value": v} for v in [100.0] * 21 + [5.0]]  # no "time"
    out = volume_ratio_closed(series, timeframe_seconds=DAY, now_ms=0, period=20)
    assert out["ratio"] is not None                       # degrades gracefully

def test_tf_to_seconds_maps_common_timeframes():
    assert tf_to_seconds("1D") == 86400
    assert tf_to_seconds("4H") == 14400
    assert tf_to_seconds("1W") == 604800
    assert tf_to_seconds("4h") == 14400       # case-insensitive
    assert tf_to_seconds("unknown") == 86400  # safe fallback
    assert tf_to_seconds("") == 86400
    assert tf_to_seconds(None) == 86400

def test_weekly_partial_bar_dropped_with_correct_tf():
    # A 1W series whose last bar opened 2 days ago is still in progress; with the
    # correct weekly timeframe it must be treated as NOT closed (the daily 86400
    # would have wrongly marked it closed).
    WEEK_MS = 604800 * 1000
    vals = [100.0] * 21 + [5.0]
    last_open = 1_000 * WEEK_MS
    now = last_open + 2 * DAY_MS  # 2 days into the weekly bar
    series = _series(vals, last_open_ms=last_open, step_ms=WEEK_MS)
    out = volume_ratio_closed(series, timeframe_seconds=tf_to_seconds("1W"), now_ms=now, period=20)
    assert out["candle_complete"] is False
    assert out["used_index"] == len(series) - 2

def test_clean_volume_reliable_with_median_baseline():
    vals = [100.0] * 20 + [160.0]
    open_ms = 1000 * DAY_MS
    out = volume_ratio_closed(_series(vals, open_ms), timeframe_seconds=DAY,
                              now_ms=open_ms + 3 * DAY_MS, period=20)
    assert out["reliable"] is True
    assert abs(out["ratio"] - 1.6) < 0.01            # 160 / median(100)

def test_junk_forwardfill_series_unreliable():
    # silver-like: real ~19000 interleaved with placeholder ~22 (>25% near-zero)
    vals = ([19000.0, 22.0, 19500.0, 22.0, 19000.0, 22.0, 19200.0, 22.0] * 3) + [19000.0, 19500.0]
    open_ms = 1000 * DAY_MS
    out = volume_ratio_closed(_series(vals, open_ms), timeframe_seconds=DAY,
                              now_ms=open_ms + 3 * DAY_MS, period=20)
    assert out["reliable"] is False

def test_tiny_last_candle_unreliable():
    # corn-like: healthy window, last closed candle is near-zero junk
    vals = [200000.0] * 20 + [3000.0]
    open_ms = 1000 * DAY_MS
    out = volume_ratio_closed(_series(vals, open_ms), timeframe_seconds=DAY,
                              now_ms=open_ms + 3 * DAY_MS, period=20)
    assert out["reliable"] is False
