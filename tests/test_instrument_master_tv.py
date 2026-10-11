"""The one Carrac-ticker <-> TradingView-symbol map.

Venue-exact: a TradingView exchange prefix names a venue, and a
crypto chart symbol resolves to THAT venue's market or raises — it never falls
back to another venue. CRYPTOCAP / TVC / SP / CME_MINI (and the equity, FX and
index prefixes) are TradingView-only: no backup feed.

Offline by construction: ccxt market lists come from
``tests/fixtures/tv_venue_markets.json`` through the module's own ``markets``
injection point (a captured snapshot). Nothing here monkeypatches
``instrument_master``.
"""
import json
from pathlib import Path

import pytest

import database
import instrument_master as im

_FIXTURE = Path(__file__).parent / 'fixtures' / 'tv_venue_markets.json'
_MARKETS = {k: v for k, v in json.loads(_FIXTURE.read_text()).items()
            if not k.startswith('_')}


def fixture_markets(ccxt_id):
    """The ``markets`` loader the module takes: ccxt id -> {unified: {id, tick}}."""
    return _MARKETS.get(ccxt_id, {})


# A sample TradingView watchlist.
SAMPLE_WATCHLIST = {'BTC', 'ETH', 'SOL', 'LTC', 'TOTAL', 'TOTAL2', 'TOTAL3',
                   'BTC.D', 'GOLD', 'DXY', 'SPX', 'ES'}

# Universe tickers with no single TradingView symbol, each for a stated reason
# (instrument_master.TV_UNMAPPABLE). Adding one must be a conscious edit here.
EXPECTED_UNMAPPABLE = {'ALTBTC', 'TOTAL3BTC', 'KPEPE', 'KBONK', 'BEAM'}


def scanner_universe():
    """Exactly what the scanner scores: precompute._compute_assets_response
    ('htf') takes db.get_all_symbols() and drops delisted symbols. Read LIVE,
    not from instrument_master's snapshot, so a coin added to crypto_map
    without a map entry fails here by name."""
    db = database.OHLCVDatabase()
    return {s for s in db.get_all_symbols() if not im.is_delisted(s)}


def coverage_set():
    return scanner_universe() | SAMPLE_WATCHLIST


# ---------------------------------------------------------------------------
# Every watched symbol maps to exactly one TradingView symbol
# ---------------------------------------------------------------------------

class TestCoverage:
    def test_scanner_universe_is_not_empty(self):
        # An empty universe would make every coverage test below vacuous.
        assert len(scanner_universe()) > 300

    def test_every_symbol_maps_or_is_explicitly_unmappable(self):
        unmapped = []
        for t in sorted(coverage_set()):
            if t in im.TV_UNMAPPABLE:
                continue
            try:
                im.to_tv_symbol(t)
            except im.TvSymbolError as e:
                unmapped.append(f'{t} ({e})')
        assert not unmapped, (
            f'{len(unmapped)} watched ticker(s) have no TradingView symbol — add '
            f'them to instrument_master._TV_SYMBOL_EXPLICIT: {unmapped}')

    def test_no_two_tickers_share_a_tradingview_symbol(self):
        by_tv = {}
        for t, tv in im.tv_symbol_map().items():
            by_tv.setdefault(tv, []).append(t)
        dupes = {tv: sorted(ts) for tv, ts in by_tv.items() if len(ts) > 1}
        assert not dupes, f'TradingView symbols claimed by more than one ticker: {dupes}'

    def test_unmappable_list_is_exactly_the_reviewed_one(self):
        assert set(im.TV_UNMAPPABLE) == EXPECTED_UNMAPPABLE, (
            f'unmappable changed: +{sorted(set(im.TV_UNMAPPABLE) - EXPECTED_UNMAPPABLE)} '
            f'-{sorted(EXPECTED_UNMAPPABLE - set(im.TV_UNMAPPABLE))}')

    def test_unmappable_entries_are_real_universe_tickers_with_reasons(self):
        universe = scanner_universe()
        stale = sorted(t for t in im.TV_UNMAPPABLE if t not in universe)
        assert not stale, f'unmappable entries no longer in the universe: {stale}'
        assert all(len(r) > 20 for r in im.TV_UNMAPPABLE.values())

    def test_unmappable_ticker_raises_with_its_reason(self):
        with pytest.raises(im.UnmappedSymbolError, match='perp'):
            im.to_tv_symbol('KPEPE')

    def test_unknown_ticker_raises_named_error(self):
        with pytest.raises(im.UnmappedSymbolError, match='NOSUCHCOIN'):
            im.to_tv_symbol('NOSUCHCOIN')

    def test_owner_examples(self):
        assert im.to_tv_symbol('BTC') == 'BINANCE:BTCUSDT'
        assert im.to_tv_symbol('LTC') == 'COINBASE:LTCUSD'
        assert im.to_tv_symbol('SPX') == 'SP:SPX'
        assert im.to_tv_symbol('GOLD') == 'TVC:GOLD'
        assert im.to_tv_symbol('DXY') == 'TVC:DXY'
        assert im.to_tv_symbol('TOTAL') == 'CRYPTOCAP:TOTAL'
        assert im.to_tv_symbol('ES') == 'CME_MINI:ES1!'

    def test_ticker_lookup_is_case_insensitive(self):
        assert im.to_tv_symbol('btc.d') == 'CRYPTOCAP:BTC.D'


# ---------------------------------------------------------------------------
# A backup feed (exchange + pair) or an explicit no-backup marker
# ---------------------------------------------------------------------------

class TestBackup:
    def test_every_mapped_symbol_has_a_backup_or_explicit_no_backup(self, capsys):
        no_backup, missing = [], []
        for t, tv in sorted(im.tv_symbol_map().items()):
            inst = im.from_tv_symbol(tv, markets=fixture_markets)
            if inst.backup == im.NO_BACKUP:
                no_backup.append(t)
            elif not (isinstance(inst.backup, im.BackupFeed)
                      and inst.backup.exchange and inst.backup.pair):
                missing.append(t)
        assert not missing, f'no backup and not marked no_backup: {missing}'
        # The test LISTS the no-backup symbols.
        with capsys.disabled():
            print(f'\n[instrument-master] {len(no_backup)} TradingView-only (no backup): '
                  f'{" ".join(no_backup)}')
        assert {'TOTAL', 'TOTAL2', 'TOTAL3', 'BTC.D', 'GOLD', 'DXY', 'SPX', 'ES'} <= set(no_backup)

    def test_crypto_backup_is_the_same_venue(self):
        inst = im.from_tv_symbol('COINBASE:LTCUSD', markets=fixture_markets)
        assert inst.backup == im.BackupFeed(exchange='coinbase', pair='LTC-USD')

    @pytest.mark.parametrize('tv', ['CRYPTOCAP:TOTAL', 'TVC:GOLD', 'SP:SPX',
                                    'CME_MINI:ES1!', 'NASDAQ:AAPL'])
    def test_tradingview_only_prefixes_have_no_backup(self, tv):
        assert im.from_tv_symbol(tv, markets=fixture_markets).backup == im.NO_BACKUP


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------

class TestRoundTrip:
    @pytest.mark.parametrize('ticker,tv', [
        ('BTC', 'BINANCE:BTCUSDT'),
        ('ETHBTC', 'BINANCE:ETHBTC'),
        ('LTC', 'COINBASE:LTCUSD'),
        ('XMR', 'KRAKEN:XMRUSD'),
        ('TOTAL3', 'CRYPTOCAP:TOTAL3'),
        ('BTC.D', 'CRYPTOCAP:BTC.D'),
        ('GOLD', 'TVC:GOLD'),
        ('DXY', 'TVC:DXY'),
        ('SPX', 'SP:SPX'),
        ('ES', 'CME_MINI:ES1!'),
    ])
    def test_canonical_round_trip(self, ticker, tv):
        assert im.to_tv_symbol(ticker) == tv
        assert im.from_tv_symbol(tv, markets=fixture_markets).ticker == ticker

    @pytest.mark.parametrize('ticker,venue,tv', [
        ('BTC', 'COINBASE', 'COINBASE:BTCUSD'),
        ('BTC', 'KRAKEN', 'KRAKEN:BTCUSD'),
        ('LTC', 'BINANCE', 'BINANCE:LTCUSDT'),
    ])
    def test_venue_round_trip(self, ticker, venue, tv):
        assert im.to_tv_symbol(ticker, venue=venue, markets=fixture_markets) == tv
        inst = im.from_tv_symbol(tv, markets=fixture_markets)
        assert (inst.ticker, inst.exchange) == (ticker, venue)

    def test_every_mapped_symbol_round_trips(self):
        bad = []
        for t, tv in im.tv_symbol_map().items():
            got = im.from_tv_symbol(tv, markets=fixture_markets).ticker
            if got != t:
                bad.append((t, tv, got))
        assert not bad, f'from_tv_symbol(to_tv_symbol(t)) != t: {bad}'


# ---------------------------------------------------------------------------
# Venue-exact: exchange + venue symbol + tick size; hard error otherwise
# ---------------------------------------------------------------------------

class TestVenueExact:
    def test_coinbase_ltc_carries_venue_symbol_and_tick(self):
        inst = im.from_tv_symbol('COINBASE:LTCUSD', markets=fixture_markets)
        assert inst.exchange == 'COINBASE'
        assert inst.venue_symbol == 'LTC-USD'
        assert inst.tick_size == 0.001
        assert inst.tv_symbol == 'COINBASE:LTCUSD'

    def test_binance_ltc_is_a_different_market(self):
        inst = im.from_tv_symbol('BINANCE:LTCUSDT', markets=fixture_markets)
        assert (inst.venue_symbol, inst.tick_size) == ('LTCUSDT', 0.01)

    def test_kraken_uses_the_venue_own_pair_name(self):
        assert im.from_tv_symbol('KRAKEN:BTCUSD', markets=fixture_markets).venue_symbol == 'XXBTZUSD'

    def test_every_crypto_mapping_resolves_to_a_real_venue_market(self):
        """Every exchange-listed mapping must name a market ccxt can read,
        with a tick size — this is what the watch will price against."""
        bad = []
        for t, tv in im.tv_symbol_map().items():
            try:
                inst = im.from_tv_symbol(tv, markets=fixture_markets)
            except im.TvSymbolError as e:
                bad.append(f'{t} -> {tv}: {e}')
                continue
            if inst.backup != im.NO_BACKUP and not inst.tick_size:
                bad.append(f'{t} -> {tv}: no tick size')
        assert not bad, bad

    def test_made_up_venue_is_refused_by_name(self):
        with pytest.raises(im.UnsupportedVenueError, match='FAKEXCHANGE'):
            im.from_tv_symbol('FAKEXCHANGE:BTCUSDT', markets=fixture_markets)

    def test_made_up_venue_is_refused_by_to_tv_symbol_too(self):
        with pytest.raises(im.UnsupportedVenueError, match='FAKEXCHANGE'):
            im.to_tv_symbol('BTC', venue='FAKEXCHANGE', markets=fixture_markets)

    def test_known_venue_without_the_market_never_falls_back(self):
        """HYPE has no Binance spot market. Asking for it on Binance must fail
        — not quietly answer with Coinbase's HYPE."""
        with pytest.raises(im.UnsupportedVenueError, match='HYPE/USDT'):
            im.from_tv_symbol('BINANCE:HYPEUSDT', markets=fixture_markets)

    def test_perpetual_contracts_are_refused(self):
        with pytest.raises(im.UnsupportedVenueError, match='perpetual'):
            im.from_tv_symbol('BINANCE:BTCUSDT.P', markets=fixture_markets)

    def test_unmapped_symbol_on_a_tradingview_only_prefix(self):
        with pytest.raises(im.UnmappedSymbolError, match='TVC:NOSUCH'):
            im.from_tv_symbol('TVC:NOSUCH', markets=fixture_markets)

    def test_malformed_symbol(self):
        with pytest.raises(im.TvSymbolError):
            im.from_tv_symbol('BTCUSDT', markets=fixture_markets)

    def test_errors_are_value_errors(self):
        # Callers that already catch ValueError keep working.
        assert issubclass(im.UnsupportedVenueError, ValueError)
        assert issubclass(im.UnmappedSymbolError, ValueError)
