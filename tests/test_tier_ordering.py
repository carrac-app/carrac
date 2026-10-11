"""A higher tier never caps a numeric limit below a lower one.

Premium once allowed 20 plays while Member allowed 999, so upgrading cut a
member's play limit. Compare every numeric limit tier by tier.
"""
import pytest

from carrac.auth.decorators import TIER_LIMITS

ORDER = ['free', 'member', 'premium', 'admin']


@pytest.mark.parametrize('lower,higher', list(zip(ORDER, ORDER[1:])))
def test_higher_tier_limits_are_not_lower(lower, higher):
    low, high = TIER_LIMITS[lower], TIER_LIMITS[higher]
    regressions = {
        key: (low[key], high.get(key))
        for key, value in low.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        and isinstance(high.get(key), (int, float)) and high[key] < value
    }
    assert not regressions, f'{higher} caps below {lower}: {regressions}'
