"""The Flask route and the runtime-agnostic service share ONE CPR implementation."""

import pytest

from carrac.charts import routes, service

_NAMES = [
    "_cpr_round",
    "_cpr_supports_with_logging",
    "_calculate_cpr_history",
    "_calculate_weekly_cpr_history",
    "_calculate_daily_cpr_history",
]


@pytest.mark.parametrize("name", _NAMES)
def test_routes_and_service_use_the_same_function_object(name):
    assert getattr(routes, name) is getattr(service, name)
