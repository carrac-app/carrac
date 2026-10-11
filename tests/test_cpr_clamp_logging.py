"""The CPR negative-support clamp must not fire silently.

``_cpr_clamp_negative`` maps a non-positive computed support level to
``None``. When TOTAL/market-cap constituents are mixed at differing coverage
the summed series can distort enough to push a support below zero — and the
clamp must not swallow that silently, which would mask upstream data
corruption. The clamp emits one ``logger.warning`` per payload build,
tagged with the symbol, which levels were clamped, and the input values.
"""

import logging

import pytest

from carrac.charts import routes as charts_routes
from carrac.charts import service


# Both strangler sides call the one clamp-with-logging helper, which lives in
# the service module; the route populates the shared cache /api/chart-internal
# reads, so it must log too.
_HELPERS = [
    (service, "carrac.charts.service"),
    (charts_routes, "carrac.charts.service"),
]


@pytest.mark.parametrize("mod,logger_name", _HELPERS)
def test_clamp_logs_one_warning_across_strangler_sides(mod, logger_name, caplog):
    with caplog.at_level(logging.WARNING, logger=logger_name):
        out = mod._cpr_supports_with_logging(
            "TOTAL",
            -1.0,   # s1 raw — clamps
            -0.5,   # s2 raw — clamps
            2.0,    # s3 raw — survives
            prev_high=7.4556, prev_low=0.7639, prev_close=1.0, pivot=3.0,
        )
    assert out["s1"] is None
    assert out["s2"] is None
    assert out["s3"] == 2.0
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "clamp" in r.getMessage().lower()
    ]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "TOTAL" in msg
    assert "s1" in msg and "s2" in msg
    assert "7.4556" in msg and "0.7639" in msg


def test_clamp_logs_one_warning_with_symbol_and_levels(caplog):
    with caplog.at_level(logging.WARNING, logger="carrac.charts.service"):
        out = service._cpr_supports_with_logging(
            "TOTAL",
            -1.0,   # s1 raw — clamps
            -0.5,   # s2 raw — clamps
            2.0,    # s3 raw — survives
            prev_high=7.4556,
            prev_low=0.7639,
            prev_close=1.0,
            pivot=3.0,
        )

    assert out["s1"] is None
    assert out["s2"] is None
    assert out["s3"] == 2.0

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "clamp" in r.getMessage().lower()
    ]
    assert len(warnings) == 1, "exactly one clamp warning per payload build"
    msg = warnings[0].getMessage()
    assert "TOTAL" in msg
    assert "s1" in msg and "s2" in msg
    # The offending inputs must be logged so the distortion is diagnosable.
    assert "7.4556" in msg and "0.7639" in msg


def test_no_warning_when_all_supports_positive(caplog):
    with caplog.at_level(logging.WARNING, logger="carrac.charts.service"):
        out = service._cpr_supports_with_logging(
            "BTC",
            100.0, 90.0, 80.0,
            prev_high=110.0, prev_low=95.0, prev_close=100.0, pivot=101.0,
        )

    assert out == {"s1": 100.0, "s2": 90.0, "s3": 80.0}
    assert [r for r in caplog.records if "clamp" in r.getMessage().lower()] == []
