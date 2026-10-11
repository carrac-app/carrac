from carrac.composite.assemblers import _extract_technicals, _extract_levels


def _fake_asset():
    return {
        'consensio': {'total': {'positions': 5, 'max_positions': 6},
                      '1D': {'signal': 2}, '3D': {'signal': 1}, '1W': {'signal': 2}},
        'weekly_rsi': 58.7, 'ema_ribbon': {'trend': 'bull'}, 'adx': {'adx': 24.0},
        'structure': 'HH/HL',
        'bb': {'squeeze_pct': 42.0, 'width_pct': 3.1, 'avg_width_pct': 7.4},
        'cpr':        {'pivot': 100, 'tc': 101,   'bc': 99,   'r1': 103, 'r2': 105, 'r3': 108, 's1': 97, 's2': 95, 's3': 92},
        'weekly_cpr': {'pivot': 100, 'tc': 101,   'bc': 99,   'r1': 104, 'r2': 107, 'r3': 110, 's1': 96, 's2': 93, 's3': 90},
        'daily_cpr':  {'pivot': 100, 'tc': 100.5, 'bc': 99.5, 'r1': 101, 'r2': 102, 'r3': 103, 's1': 99, 's2': 98, 's3': 97},
        'pdh': 102, 'pdl': 98, 'pwh': 106, 'pwl': 94,
    }


def test_technicals_includes_bb():
    t = _extract_technicals('BTC', {'BTC': _fake_asset()})
    assert t['bb']['squeeze_pct'] == 42.0


def test_levels_includes_full_sr_bands():
    lv = _extract_levels('BTC', {'BTC': _fake_asset()})
    assert lv['cpr']['r3'] == 108
    assert lv['cpr']['s3'] == 92
    assert lv['weekly_cpr']['r1'] == 104
    assert lv['daily_cpr']['s2'] == 98


def test_levels_still_has_prior_and_pivot():
    lv = _extract_levels('BTC', {'BTC': _fake_asset()})
    assert lv['cpr']['pivot'] == 100
    assert lv['pdh'] == 102 and lv['pwl'] == 94


def test_extractors_tolerate_missing():
    assert _extract_levels('X', {'X': {}}) is None
    t = _extract_technicals('X', {'X': {}})
    assert t is None or 'bb' not in t
