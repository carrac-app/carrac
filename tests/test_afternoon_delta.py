from morning_engine.afternoon_delta import build_delta


def test_regime_held_and_fg_up():
    d = build_delta(
        morning_ctx={"regime": "RISK_OFF", "fear_greed": 15},
        current_ctx={"regime": "RISK_OFF", "fear_greed": 19, "levels_tested": [], "signal_flips": [], "new_prints": []},
    )
    assert d["regime"] == {"was": "RISK_OFF", "now": "RISK_OFF", "changed": False}
    assert d["fear_greed"] == {"was": 15, "now": 19, "dir": "up"}


def test_new_print_passthrough_and_flat_fg():
    d = build_delta(
        morning_ctx={"regime": "RISK_OFF", "fear_greed": 15},
        current_ctx={"regime": "RISK_ON", "fear_greed": 15,
                     "levels_tested": [{"asset":"SPX","level":7354.0,"outcome":"reclaimed"}],
                     "signal_flips": [{"asset":"BTC","from":"bear","to":"neutral"}],
                     "new_prints": [{"event":"Jobless Claims","expected":"230k","actual":"221k","ts":"13:30"}]},
    )
    assert d["regime"]["changed"] is True
    assert d["fear_greed"]["dir"] == "flat"
    assert d["levels_tested"][0]["outcome"] == "reclaimed"
    assert d["signal_flips"][0]["to"] == "neutral"
    assert d["new_prints"][0]["actual"] == "221k"


def test_missing_keys_do_not_raise():
    """build_delta must never raise even with empty/None contexts."""
    d = build_delta(morning_ctx={}, current_ctx={})
    assert "regime" in d
    assert "fear_greed" in d
    assert d["levels_tested"] == []
    assert d["signal_flips"] == []
    assert d["new_prints"] == []


def test_none_fear_greed_defaults_to_flat():
    d = build_delta(
        morning_ctx={"regime": "RISK_ON", "fear_greed": None},
        current_ctx={"regime": "RISK_ON", "fear_greed": None},
    )
    assert d["fear_greed"]["dir"] == "flat"


def test_regime_change_detected():
    d = build_delta(
        morning_ctx={"regime": "RISK_ON"},
        current_ctx={"regime": "RISK_OFF"},
    )
    assert d["regime"]["changed"] is True
    assert d["regime"]["was"] == "RISK_ON"
    assert d["regime"]["now"] == "RISK_OFF"


class TestFlipEntryClassifier:
    """_flip_entry: field-aware classification of /api/report/diff changes
    (sma_crossover emits STRINGS 'golden'/'death'; a numeric `> 0`
    comparison would raise TypeError and silently truncate the ledger)."""

    def test_sma_crossover_strings(self):
        from morning_engine.engine import _flip_entry
        e = _flip_entry({'symbol': 'BTC', 'field': 'sma_crossover',
                         'old_value': 'death', 'new_value': 'golden'})
        assert e == {'asset': 'BTC', 'from': 'bearish', 'to': 'bullish'}

    def test_consensio_signed_numbers(self):
        from morning_engine.engine import _flip_entry
        e = _flip_entry({'symbol': 'ETH', 'field': 'consensio_1D',
                         'old_value': -1, 'new_value': 2})
        assert e == {'asset': 'ETH', 'from': 'bearish', 'to': 'bullish'}

    def test_unknown_string_shape_skipped_not_raised(self):
        from morning_engine.engine import _flip_entry
        assert _flip_entry({'symbol': 'SOL', 'field': 'weird',
                            'old_value': 'x', 'new_value': 'y'}) is None

    def test_none_values_skipped(self):
        from morning_engine.engine import _flip_entry
        assert _flip_entry({'symbol': 'SOL', 'field': 'rsi_14',
                            'old_value': None, 'new_value': 55}) is None
