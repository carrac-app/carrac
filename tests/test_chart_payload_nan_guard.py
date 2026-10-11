"""Chart payloads must never serialize literal NaN/Infinity.

A transient data glitch during a build (e.g. a cache-warm race leaving an
empty month in a synthetic index) produced ``"bc": NaN`` inside the monthly
CPR history — invalid JSON that hard-crashed the frontend's JSON.parse once
frozen into the Redis chart cache. Non-finite floats must become ``null`` at
the payload boundary.
"""
import json


from carrac.caches import sanitize_nonfinite, set_cached_chart, get_cached_chart


class TestSanitizeNonfinite:
    def test_nan_and_inf_become_none(self):
        obj = {
            'cpr': {'bc': float('nan'), 'tc': 2.5,
                    'history': [{'pivot': float('inf'), 'bc': float('-inf')}, {'bc': 1.0}]},
            'ohlcv': [{'close': float('nan'), 'open': 4.6}],
        }
        out = sanitize_nonfinite(obj)
        assert out['cpr']['bc'] is None
        assert out['cpr']['tc'] == 2.5
        assert out['cpr']['history'][0]['pivot'] is None
        assert out['cpr']['history'][0]['bc'] is None
        assert out['cpr']['history'][1]['bc'] == 1.0
        assert out['ohlcv'][0]['close'] is None
        assert out['ohlcv'][0]['open'] == 4.6

    def test_output_is_strict_json_serializable(self):
        out = sanitize_nonfinite({'a': [float('nan'), {'b': float('inf')}], 'c': 'x'})
        json.dumps(out, allow_nan=False)  # must not raise

    def test_untouched_types_pass_through(self):
        obj = {'s': 'txt', 'i': 7, 'f': 1.25, 'n': None, 'b': True, 'l': [1, 2]}
        assert sanitize_nonfinite(obj) == obj


class TestSetCachedChartGuards:
    def test_cached_payload_has_no_nan(self):
        payload = {'symbol': 'TESTNAN', 'cpr': {'bc': float('nan'), 'pivot': 2.0}}
        set_cached_chart('TESTNAN', '1D', False, payload)
        cached = get_cached_chart('TESTNAN', '1D', False)
        assert cached is not None
        data, _fresh = cached
        assert data['cpr']['bc'] is None
        assert data['cpr']['pivot'] == 2.0
        # The full round-trip must be strict-JSON clean.
        json.dumps(data, allow_nan=False)

    def test_caller_dict_sanitized_in_place(self):
        # Flask's strangler chart route serves the same dict it passed in —
        # the caller-visible payload must be clean too, not just the cache copy.
        payload = {'symbol': 'TESTNAN2', 'cpr': {'bc': float('nan')}}
        set_cached_chart('TESTNAN2', '1D', False, payload)
        assert payload['cpr']['bc'] is None
        assert '_cached_at' in payload
        json.dumps(payload, allow_nan=False)
