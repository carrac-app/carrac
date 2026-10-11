"""The weekly cycle endpoint describes where BTC sits against its 200-week
moving average. Its zone names and reasons must stay descriptive: they are
served by /api/weekly-signal and must never read as an instruction."""
import re

import pytest

from weekly_signal_engine import WeeklySignalEngine

_INSTRUCTION = re.compile(
    r"\b(accumulat\w*|distribut\w*|hold|buy|sell|wait|pullback|should)\b", re.I)


def _calc(mm, z=None, pm=None):
    eng = WeeklySignalEngine(db=None)
    return eng.calculate(
        'BTC',
        mvrv_data={'zscore': z},
        mayer_data={'mayer_multiple': mm, 'ma_200w': 50000.0},
        puell_data={'puell_multiple': pm},
    )


@pytest.mark.parametrize('mm,z,pm,zone', [
    (0.7, None, None, 'DISCOUNT'),
    (0.9, None, None, 'DISCOUNT'),
    (1.1, -0.5, None, 'DISCOUNT'),
    (1.1, 1.0, None, 'NEUTRAL'),
    (1.5, 1.0, None, 'NEUTRAL'),
    (1.5, 3.0, None, 'NEUTRAL'),
    (2.2, None, None, 'EXTENDED'),
    (2.6, 5.0, 5.0, 'EXTENDED'),
])
def test_zones_follow_the_200_week_multiple(mm, z, pm, zone):
    out = _calc(mm, z, pm)
    assert out['signal'] == zone
    assert not _INSTRUCTION.search(out['signal'])
    assert not _INSTRUCTION.search(out['reason']), out['reason']


def test_reason_states_the_distance_from_the_average():
    assert _calc(1.35)['reason'].startswith('Price is 35% above the 200-week MA')
    assert _calc(0.88)['reason'].startswith('Price is 12% below the 200-week MA')


def test_missing_multiple_is_no_data():
    assert _calc(None)['signal'] == 'NO_DATA'
