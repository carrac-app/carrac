"""Symbol-lookup normalization (carrac/search/routes.py).

The Yahoo search fallback must not strip the exchange suffix from
results ("005930.KS" -> "005930"), which would make every non-US listing
unloadable — bare "005930" is Not Found on Yahoo's chart API, so the
recommended chart errored immediately. US symbols from Yahoo never carry
a dot suffix (class shares use dashes, e.g. BRK-B), so the dot-strip
only ever damaged results. The caret/=X strips stay: '^GSPC' and
'EURUSD=X' forms are rejected by _SAFE_SYMBOL_RE downstream.
"""

import pytest

from carrac.search.routes import _normalize_yahoo_symbol


@pytest.mark.parametrize("yahoo_sym,expected", [
    # Foreign listings keep their exchange suffix — chartable on Yahoo
    ("005930.KS", "005930.KS"),   # Samsung Electronics, Korea Exchange
    ("BC94.L", "BC94.L"),         # Samsung GDR, London
    ("7203.T", "7203.T"),         # Toyota, Tokyo
    ("SAP.DE", "SAP.DE"),         # SAP, Xetra
    # US symbols pass through untouched
    ("AAPL", "AAPL"),
    ("BRK-B", "BRK-B"),
    # Existing normalizations preserved (these forms fail _SAFE_SYMBOL_RE)
    ("^GSPC", "GSPC"),
    ("EURUSD=X", "EURUSD"),
    # Uppercasing preserved
    ("sap.de", "SAP.DE"),
])
def test_normalize_yahoo_symbol(yahoo_sym, expected):
    assert _normalize_yahoo_symbol(yahoo_sym) == expected
