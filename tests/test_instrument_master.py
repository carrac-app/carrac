"""Instrument master foundation.

Covers: files-vs-manifest reconciliation (including the dot-mangling false-
orphan bug: BTC.D vs its cache file BTC_D_daily.csv), an
orphan file, a reverse orphan (no cache file — BUX/HSTECH/RHM/SQ are the real
measured examples), knowledge-date update semantics
(first_observed set once per status value, last_confirmed advances),
Binance status-string mapping, the injectable exchangeInfo ingestion helper,
authoritative_source derivation, and the atomic store round-trip.

No test here monkeypatches ``instrument_master`` itself — the Binance network
call is exercised only through its ``fetch`` injection point, and the store
tests use real temp-directory files rather than patching the filesystem.
"""
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

import instrument_master as im


def _healthy_exchange_info(*entries):
    """An exchangeInfo payload big enough to be BELIEVED.

    Delisting-gate precondition: `is_healthy_exchange_info` refuses payloads
    below `EXCHANGE_INFO_MIN_SYMBOLS`, because a SHORT payload would otherwise
    map every absent ticker to 'unknown' and silently erase the venue evidence
    a delisted-gate is about to depend on. So a test that wants its payload
    ingested has to hand over a plausible one — padding here rather than
    lowering the floor, so these tests keep exercising the production rule
    instead of a relaxed version of it.
    """
    padding = [{'symbol': f'PAD{i}USDT', 'status': 'TRADING'}
               for i in range(im.EXCHANGE_INFO_MIN_SYMBOLS)]
    return {'symbols': list(entries) + padding}


# ---------------------------------------------------------------------------
# _cache_stem — the dot-mangling canonicalisation
# ---------------------------------------------------------------------------

class TestCacheStem:
    def test_plain_ticker_unchanged(self):
        assert im._cache_stem('btc') == 'BTC'

    def test_dot_mangled_to_underscore(self):
        # csv_cache.CSVCache.get_path: symbol.upper().replace('.', '_')...
        assert im._cache_stem('BTC.D') == 'BTC_D'
        assert im._cache_stem('others.d') == 'OTHERS_D'
        assert im._cache_stem('USDT.D') == 'USDT_D'

    def test_caret_stripped(self):
        assert im._cache_stem('^VIX') == 'VIX'


# ---------------------------------------------------------------------------
# Reconciliation — files vs manifest
# ---------------------------------------------------------------------------

def _write_csv(path: Path, dates):
    with open(path, 'w', newline='') as fh:
        fh.write('timestamp,open,high,low,close,volume\n')
        for d in dates:
            fh.write(f'{d},1,1,1,1,100\n')


@pytest.fixture
def fixture_cache_dir(tmp_path):
    """A cache dir + manifest reproducing the exact shapes the
    comments measured: a dot-symbol, an orphan file, and a reverse orphan.
    """
    cache_dir = tmp_path
    _write_csv(cache_dir / 'BTC_daily.csv', ['2026-08-01', '2026-08-02'])
    # BTC.D — the manifest keeps the dotted ticker, the file is mangled.
    _write_csv(cache_dir / 'BTC_D_daily.csv', ['2026-08-01', '2026-08-02', '2026-08-03'])
    # A file with no manifest entry at all. A fictional ticker, not a real
    # one: a real ticker stand-in can later be curated as delisted and rot.
    _write_csv(cache_dir / 'ZZORPHAN_daily.csv', ['2026-08-01'])
    # A file that must NOT be picked up (wrong suffix).
    (cache_dir / 'NOTES_hourly.csv').write_text('timestamp,open\n2026-08-01,1\n')

    manifest = {
        'symbols': ['BTC', 'BTC.D', 'BUX'],  # BUX: tracked, no file (reverse orphan)
        'written_at': '2026-08-12T00:00:00',
    }
    (cache_dir / im.TRACKED_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    return cache_dir


class TestReconcile:
    def test_every_file_and_every_manifest_entry_gets_a_record(self, fixture_cache_dir):
        records = im.reconcile(cache_dir=fixture_cache_dir, data_dir=fixture_cache_dir)
        assert set(records) == {'BTC', 'BTC_D', 'ZZORPHAN', 'BUX'}

    def test_hourly_suffix_file_is_ignored(self, fixture_cache_dir):
        records = im.reconcile(cache_dir=fixture_cache_dir, data_dir=fixture_cache_dir)
        assert 'NOTES' not in records

    def test_tracked_symbol_with_file(self, fixture_cache_dir):
        records = im.reconcile(cache_dir=fixture_cache_dir, data_dir=fixture_cache_dir)
        btc = records['BTC']
        assert btc.tracking_state == 'tracked'
        assert btc.ticker == 'BTC'
        assert btc.id == 'BTC'
        assert btc.first_bar_date == '2026-08-01'
        assert btc.last_bar_date == '2026-08-02'
        assert btc.row_count == 2
        assert btc.authoritative_source == 'binance'

    def test_dot_symbol_resolves_to_mangled_file_not_a_false_orphan(self, fixture_cache_dir):
        records = im.reconcile(cache_dir=fixture_cache_dir, data_dir=fixture_cache_dir)
        assert 'BTC_D' in records
        rec = records['BTC_D']
        # This is the BTC.D orphan bug: without canonicalisation, BTC.D (in the
        # manifest) and BTC_D_daily.csv (on disk) look unrelated and BTC.D
        # gets misreported as a reverse orphan. Canonicalising both sides on
        # the mangled stem must make them the SAME record, tracked.
        assert rec.tracking_state == 'tracked'
        assert rec.ticker == 'BTC.D'
        assert rec.cache_stem == 'BTC_D'
        assert rec.row_count == 3
        assert rec.authoritative_source == 'derived'  # crypto_metric_map series

    def test_orphan_file_with_no_manifest_entry(self, fixture_cache_dir):
        records = im.reconcile(cache_dir=fixture_cache_dir, data_dir=fixture_cache_dir)
        orphan = records['ZZORPHAN']
        assert orphan.tracking_state == 'orphaned'
        assert orphan.ticker == 'ZZORPHAN'  # best-effort: no manifest form to recover
        assert orphan.row_count == 1

    def test_orphaned_dot_symbol_keeps_its_derived_source(self, tmp_path):
        """An orphaned BTC.D must still classify as 'derived', not 'unknown'.

        Proven in production: `authoritative_source == 'derived'`
        reproduces Check F's hardcoded DERIVED_SERIES exactly, which is what
        makes the reference-data swap safe. That equivalence rests on the dotted ticker,
        and an orphan has no manifest entry to supply one — so without the
        routing-map reverse index it falls back to the mangled stem 'BTC_D',
        which derives to 'unknown'. Check E (frozen_derived_series) would then
        silently stop covering a dominance series, which is the BTC.D orphan
        dot-mangling bug in a second disguise.
        """
        _write_csv(tmp_path / 'BTC_D_daily.csv', ['2026-08-01', '2026-08-02'])
        # Manifest deliberately does NOT contain BTC.D — that is the scenario.
        (tmp_path / im.TRACKED_MANIFEST_FILENAME).write_text(
            json.dumps({'symbols': ['BTC'], 'written_at': '2026-08-12T00:00:00'}))

        records = im.reconcile(cache_dir=tmp_path, data_dir=tmp_path)
        rec = records['BTC_D']
        assert rec.tracking_state == 'orphaned'
        assert rec.ticker == 'BTC.D', 'dotted form must be recovered from the routing maps'
        assert rec.authoritative_source == 'derived'

    def test_reverse_orphan_tracked_but_no_file(self, fixture_cache_dir):
        records = im.reconcile(cache_dir=fixture_cache_dir, data_dir=fixture_cache_dir)
        bux = records['BUX']
        assert bux.tracking_state == 'reverse_orphan'
        assert bux.ticker == 'BUX'
        assert bux.first_bar_date is None
        assert bux.last_bar_date is None
        assert bux.row_count is None
        assert bux.authoritative_source == 'stooq'  # index_map member

    def test_default_status_is_unknown_never_trading(self, fixture_cache_dir):
        records = im.reconcile(cache_dir=fixture_cache_dir, data_dir=fixture_cache_dir)
        assert all(r.status == 'unknown' for r in records.values())

    def test_missing_cache_dir_only_reverse_orphans_from_manifest(self, tmp_path):
        data_dir = tmp_path / 'data'
        data_dir.mkdir()
        manifest = {'symbols': ['ETH'], 'written_at': '2026-08-12T00:00:00'}
        (data_dir / im.TRACKED_MANIFEST_FILENAME).write_text(json.dumps(manifest))
        missing_cache_dir = tmp_path / 'does-not-exist'
        records = im.reconcile(cache_dir=missing_cache_dir, data_dir=data_dir)
        assert set(records) == {'ETH'}
        assert records['ETH'].tracking_state == 'reverse_orphan'


# ---------------------------------------------------------------------------
# Knowledge-date semantics: first_observed set once,
# last_confirmed advances.
# ---------------------------------------------------------------------------

class TestKnowledgeDates:
    def test_reconcile_first_call_sets_both_to_now(self, fixture_cache_dir):
        t1 = datetime(2026, 8, 12, 10, 0, 0)
        records = im.reconcile(cache_dir=fixture_cache_dir, data_dir=fixture_cache_dir, now=t1)
        btc = records['BTC']
        assert btc.first_observed == btc.last_confirmed == t1.isoformat(timespec='seconds')

    def test_reconcile_second_call_advances_last_confirmed_keeps_first_observed(self, fixture_cache_dir):
        t1 = datetime(2026, 8, 12, 10, 0, 0)
        t2 = datetime(2026, 8, 13, 9, 0, 0)
        first = im.reconcile(cache_dir=fixture_cache_dir, data_dir=fixture_cache_dir, now=t1)
        second = im.reconcile(cache_dir=fixture_cache_dir, data_dir=fixture_cache_dir, now=t2, existing=first)
        btc = second['BTC']
        assert btc.first_observed == t1.isoformat(timespec='seconds')
        assert btc.last_confirmed == t2.isoformat(timespec='seconds')

    def test_apply_status_updates_first_change_resets_both(self):
        t0 = datetime(2026, 8, 1, 0, 0, 0)
        t1 = datetime(2026, 8, 12, 10, 0, 0)
        rec = im.InstrumentRecord(
            id='BTC', ticker='BTC', cache_stem='BTC', status='unknown',
            first_observed=t0.isoformat(timespec='seconds'),
            last_confirmed=t0.isoformat(timespec='seconds'),
        )
        updated = im.apply_status_updates({'BTC': rec}, {'BTC': 'trading'}, now=t1)
        btc = updated['BTC']
        assert btc.status == 'trading'
        assert btc.first_observed == t1.isoformat(timespec='seconds')
        assert btc.last_confirmed == t1.isoformat(timespec='seconds')

    def test_apply_status_updates_unchanged_status_only_advances_last_confirmed(self):
        t1 = datetime(2026, 8, 12, 10, 0, 0)
        t2 = datetime(2026, 8, 13, 9, 0, 0)
        rec = im.InstrumentRecord(
            id='BTC', ticker='BTC', cache_stem='BTC', status='trading',
            first_observed=t1.isoformat(timespec='seconds'),
            last_confirmed=t1.isoformat(timespec='seconds'),
        )
        updated = im.apply_status_updates({'BTC': rec}, {'BTC': 'trading'}, now=t2)
        btc = updated['BTC']
        assert btc.status == 'trading'
        assert btc.first_observed == t1.isoformat(timespec='seconds')  # unchanged
        assert btc.last_confirmed == t2.isoformat(timespec='seconds')  # advanced

    def test_apply_status_updates_second_change_resets_first_observed_again(self):
        t1 = datetime(2026, 8, 12, 10, 0, 0)
        t3 = datetime(2026, 8, 20, 0, 0, 0)
        rec = im.InstrumentRecord(
            id='BTC', ticker='BTC', cache_stem='BTC', status='trading',
            first_observed=t1.isoformat(timespec='seconds'),
            last_confirmed=t1.isoformat(timespec='seconds'),
        )
        updated = im.apply_status_updates({'BTC': rec}, {'BTC': 'suspended'}, now=t3)
        btc = updated['BTC']
        assert btc.status == 'suspended'
        assert btc.first_observed == t3.isoformat(timespec='seconds')
        assert btc.last_confirmed == t3.isoformat(timespec='seconds')

    def test_apply_status_updates_ignores_unmatched_tickers(self):
        rec = im.InstrumentRecord(id='BTC', ticker='BTC', cache_stem='BTC', status='unknown')
        updated = im.apply_status_updates({'BTC': rec}, {'ETH': 'trading'})
        assert updated['BTC'] == rec  # untouched
        assert set(updated) == {'BTC'}  # not added to


# ---------------------------------------------------------------------------
# Binance status mapping + injectable ingestion
# ---------------------------------------------------------------------------

class TestBinanceStatusMapping:
    @pytest.mark.parametrize('raw,expected', [
        ('TRADING', 'trading'),
        ('trading', 'trading'),
        ('BREAK', 'suspended'),
        ('HALT', 'suspended'),
        ('AUCTION_MATCH', 'unknown'),
        (None, 'unknown'),
        ('', 'unknown'),
    ])
    def test_map_binance_status(self, raw, expected):
        assert im.map_binance_status(raw) == expected

    def test_binance_status_by_ticker_maps_known_and_missing_pairs(self):
        exchange_info = {
            'symbols': [
                {'symbol': 'BTCUSDT', 'status': 'TRADING'},
                {'symbol': 'ETHUSDT', 'status': 'BREAK'},
            ]
        }
        out = im.binance_status_by_ticker(exchange_info, ['BTC', 'ETH', 'GHOST'])
        assert out == {'BTC': 'trading', 'ETH': 'suspended', 'GHOST': 'unknown'}

    def test_fetch_injectable_returns_fetch_result(self):
        sentinel = {'symbols': [{'symbol': 'BTCUSDT', 'status': 'TRADING'}]}
        result = im.fetch_binance_exchange_info(fetch=lambda: sentinel)
        assert result == sentinel

    def test_fetch_never_raises_on_failure(self):
        def boom():
            raise ConnectionError('no route to host')
        assert im.fetch_binance_exchange_info(fetch=boom) == {}

    def test_fetch_never_raises_on_bad_shape(self):
        assert im.fetch_binance_exchange_info(fetch=lambda: ['not', 'a', 'dict']) == {}


# ---------------------------------------------------------------------------
# authoritative_source derivation
# ---------------------------------------------------------------------------

class TestDeriveAuthoritativeSource:
    @pytest.mark.parametrize('ticker,expected', [
        ('BTC', 'binance'),
        ('ETH', 'binance'),
        ('PURR', 'hyperliquid'),      # HL-native, in HL_COINS
        ('CHILLGUY', 'hyperliquid'),  # moved off Binance to HL
        ('AKT', 'cryptocompare'),     # BinanceClient.UNAVAILABLE, not on HL
        ('XMR', 'cryptocompare'),
        ('BTC.D', 'derived'),
        ('TOTAL3BTC', 'derived'),
        ('ALTBTC', 'derived'),
        ('GOLD', 'yahoo'),            # WRONG_SCALE_ON_STOOQ
        ('EURUSD', 'yahoo'),
        ('AAPL', 'stooq'),
        ('SPX', 'stooq'),
        ('ZZZNOTREAL', 'unknown'),
        ('', 'unknown'),
    ])
    def test_derive(self, ticker, expected):
        assert im.derive_authoritative_source(ticker) == expected

    def test_case_insensitive(self):
        assert im.derive_authoritative_source('btc') == 'binance'


# ---------------------------------------------------------------------------
# Atomic store — write/load round trip
# ---------------------------------------------------------------------------

class TestStore:
    def test_write_then_load_round_trips(self, tmp_path):
        rec = im.InstrumentRecord(
            id='BTC', ticker='BTC', cache_stem='BTC', status='trading',
            tracking_state='tracked', venue='binance', authoritative_source='binance',
            first_bar_date='2017-08-17', last_bar_date='2026-08-11', row_count=3000,
            first_observed='2026-08-12T10:00:00', last_confirmed='2026-08-12T10:00:00',
        )
        im.write_store({'BTC': rec}, data_dir=tmp_path)
        loaded = im.load_records(data_dir=tmp_path)
        assert loaded == {'BTC': rec}

    def test_write_leaves_no_temp_files_behind(self, tmp_path):
        rec = im.InstrumentRecord(id='BTC', ticker='BTC', cache_stem='BTC')
        im.write_store({'BTC': rec}, data_dir=tmp_path)
        leftovers = [p for p in os.listdir(tmp_path) if p.endswith('.tmp')]
        assert leftovers == []
        assert (tmp_path / im.STORE_FILENAME).exists()

    def test_write_overwrites_fully_not_merges(self, tmp_path):
        rec_a = im.InstrumentRecord(id='A', ticker='A', cache_stem='A')
        rec_b = im.InstrumentRecord(id='B', ticker='B', cache_stem='B')
        im.write_store({'A': rec_a}, data_dir=tmp_path)
        im.write_store({'B': rec_b}, data_dir=tmp_path)
        loaded = im.load_records(data_dir=tmp_path)
        assert set(loaded) == {'B'}  # A is gone, not merged in

    def test_load_store_missing_file_returns_empty_skeleton(self, tmp_path):
        payload = im.load_store(data_dir=tmp_path / 'nope')
        assert payload == {'instruments': {}, 'written_at': None}

    def test_load_store_corrupt_json_returns_empty_skeleton(self, tmp_path):
        (tmp_path / im.STORE_FILENAME).write_text('{not valid json')
        payload = im.load_store(data_dir=tmp_path)
        assert payload == {'instruments': {}, 'written_at': None}

    def test_load_records_skips_individually_corrupt_records(self, tmp_path):
        payload = {
            'instruments': {
                'BTC': {'id': 'BTC', 'ticker': 'BTC', 'cache_stem': 'BTC'},
                'BROKEN': {'status': 'trading'},  # missing required id/ticker/cache_stem
            },
            'written_at': '2026-08-12T10:00:00',
        }
        (tmp_path / im.STORE_FILENAME).write_text(json.dumps(payload))
        loaded = im.load_records(data_dir=tmp_path)
        assert set(loaded) == {'BTC'}


# ---------------------------------------------------------------------------
# read_tracked_manifest — own minimal reader
# ---------------------------------------------------------------------------

class TestReadTrackedManifest:
    def test_reads_symbols_uppercased(self, tmp_path):
        (tmp_path / im.TRACKED_MANIFEST_FILENAME).write_text(
            json.dumps({'symbols': ['btc', 'Eth'], 'written_at': '2026-08-12T00:00:00'})
        )
        assert im.read_tracked_manifest(data_dir=tmp_path) == {'BTC', 'ETH'}

    def test_missing_manifest_returns_empty_set(self, tmp_path):
        assert im.read_tracked_manifest(data_dir=tmp_path / 'nope') == set()

    def test_corrupt_manifest_returns_empty_set(self, tmp_path):
        (tmp_path / im.TRACKED_MANIFEST_FILENAME).write_text('not json')
        assert im.read_tracked_manifest(data_dir=tmp_path) == set()


# ---------------------------------------------------------------------------
# The generator is actually CONNECTED
#
# The recurring defect class in this codebase is "built but never connected".
# These tests fail if the precompute wiring of the writer is ever removed,
# which a test of the writer function alone would not notice.
# ---------------------------------------------------------------------------

class TestInstrumentMasterIsWired:
    def test_precompute_calls_the_writer_in_the_full_cycle(self):
        """The call site must exist in the FULL (non-LTF) branch, right after
        the manifest write whose output it reconciles against."""
        src = Path('carrac/precompute.py').read_text()
        assert '_write_instrument_master()' in src, 'generator has no caller'
        manifest_at = src.index('_write_tracked_symbols_manifest(symbols)')
        master_at = src.index('_write_instrument_master()')
        assert manifest_at < master_at, (
            'the store must be written AFTER the manifest it reconciles against'
        )

    def test_writer_beacons_failures_instead_of_warning_only(self):
        """A silent failure would freeze the store while every consumer kept
        reading it as current — the swallowed-error shape. The manifest writer next
        door is deliberately warning-only; this one must not be."""
        src = Path('carrac/precompute.py').read_text()
        start = src.index('def _write_instrument_master(')
        end = src.index('def _alt_season_alarm(')
        body = src[start:end]
        assert 'sentry_sdk.capture_exception' in body
        assert 'logger.error' in body

    def test_writer_produces_a_store_the_stdlib_reader_can_read(self, fixture_cache_dir, monkeypatch):
        """End to end through the real call, not a mock: the app writes, the
        host cron's stdlib-only reader reads. The generator's actual contract."""
        import carrac.precompute as pc

        monkeypatch.setenv('DATA_DIR', str(fixture_cache_dir))
        pc._write_instrument_master()

        assert (fixture_cache_dir / im.STORE_FILENAME).exists()
        records = im.load_records(data_dir=fixture_cache_dir)
        assert set(records) == {'BTC', 'BTC_D', 'ZZORPHAN', 'BUX'}
        assert records['BTC_D'].authoritative_source == 'derived'
        assert records['ZZORPHAN'].tracking_state == 'orphaned'
        assert im.load_store(data_dir=fixture_cache_dir)['written_at']

    def test_venue_status_is_ingested_and_a_failed_fetch_never_delists(
            self, fixture_cache_dir, monkeypatch):
        """The asymmetry that keeps this safe.

        fetch_binance_exchange_info returns {} on ANY failure, and
        binance_status_by_ticker maps a ticker it cannot find to 'unknown'. If
        absence meant 'delisted', one failed fetch would mark every
        Binance-routed symbol delisted at once — and with the delisting gate wired that stops
        collection for nearly the whole universe on a transient blip. So a
        healthy payload records what the venue positively states, and a failed
        one changes nothing.
        """
        import carrac.precompute as pc

        monkeypatch.setenv('DATA_DIR', str(fixture_cache_dir))
        monkeypatch.setattr(im, 'fetch_binance_exchange_info',
                            lambda *a, **k: _healthy_exchange_info(
                                {'symbol': 'BTCUSDT', 'status': 'TRADING'}))
        pc._write_instrument_master()
        assert im.load_records(data_dir=fixture_cache_dir)['BTC'].status == 'trading'

        # Now the fetch fails outright.
        monkeypatch.setattr(im, 'fetch_binance_exchange_info', lambda *a, **k: {})
        pc._write_instrument_master()
        after = im.load_records(data_dir=fixture_cache_dir)
        assert all(r.status != 'delisted' for r in after.values())
        assert after['BTC'].status == 'trading', 'a failed fetch forgot a known status'

        # And the case the {}-check could not see: a payload that is TRUTHY but
        # short. Before the health floor this reached the ingestion, and every
        # ticker absent from it became 'unknown' — erasing the venue evidence
        # with no error anywhere, which is the state the delisting gate would then halt
        # collection from. It must change nothing, exactly like a failed fetch.
        monkeypatch.setattr(im, 'fetch_binance_exchange_info',
                            lambda *a, **k: {'symbols': [
                                {'symbol': 'ETHUSDT', 'status': 'TRADING'}]})
        pc._write_instrument_master()
        short = im.load_records(data_dir=fixture_cache_dir)
        assert short['BTC'].status == 'trading', (
            'a SHORT payload erased a known status — the health floor is not '
            'holding'
        )
        assert all(r.status != 'delisted' for r in short.values())


class TestVenueScopedStatus:
    """Delisting-gate prerequisite — the circularity, and the trap it hides.

    A ticker lands in ``_BINANCE_UNAVAILABLE`` (and so routes to CryptoCompare)
    *because* Binance stopped serving it. A scope that asks Binance only
    about still-Binance-routed tickers never asks about the symbols whose
    listing status matters most — 194 of 380 prod records sat at 'unknown'
    under that scope, including every stale symbol.

    The naive fix — widen the scope and write the answer into ``status`` —
    breaks five live assets, so both halves are pinned here.
    """

    def test_rerouted_symbols_are_in_the_query_scope(self):
        """XMR routes to CryptoCompare and must still be in the query scope."""
        assert im.derive_authoritative_source('XMR') == 'cryptocompare'
        records = {
            'BTC': im.InstrumentRecord(id='BTC', ticker='BTC', cache_stem='BTC'),
            'XMR': im.InstrumentRecord(id='XMR', ticker='XMR', cache_stem='XMR'),
        }
        assert set(im.venue_status_tickers(records)) == {'BTC', 'XMR'}

    def test_non_crypto_and_derived_are_not_asked_about(self):
        """Binance has nothing to say about AAPL or ALTBTC — asking would
        manufacture 'unknown' answers that read like observations."""
        records = {
            t: im.InstrumentRecord(id=t, ticker=t, cache_stem=t)
            for t in ('AAPL', 'ALTBTC', 'BTC')
        }
        assert im.venue_status_tickers(records) == ['BTC']

    def test_binance_break_on_a_rerouted_symbol_never_moves_instrument_status(self):
        """Pins that venue status never leaks into instrument status.

        Binance reports BREAK for XMR, which trades actively elsewhere — real
        varied OHLC and 95k daily volume when this was measured. Collapsing the
        venue verdict into ``status`` would mark a live asset dead.
        """
        rec = im.InstrumentRecord(id='XMR', ticker='XMR', cache_stem='XMR',
                                  status='unknown')
        updated = im.apply_binance_venue_status(
            {'XMR': rec}, {'XMR': 'suspended'},
            now=datetime(2026, 8, 22, 12, 0, 0))
        xmr = updated['XMR']
        assert xmr.binance_status == 'suspended', 'the venue verdict was dropped'
        assert xmr.status == 'unknown', 'a venue verdict leaked into instrument status'
        assert xmr.binance_status_at == '2026-08-22T12:00:00'

    def test_venue_status_at_advances_on_every_confirmation(self):
        """A reader must be able to tell how fresh the verdict is, rather than
        trusting the field's existence."""
        t1 = datetime(2026, 8, 22, 12, 0, 0)
        t2 = datetime(2026, 8, 23, 12, 0, 0)
        rec = im.InstrumentRecord(id='XMR', ticker='XMR', cache_stem='XMR')
        first = im.apply_binance_venue_status({'XMR': rec}, {'XMR': 'suspended'}, now=t1)
        second = im.apply_binance_venue_status(first, {'XMR': 'suspended'}, now=t2)
        assert second['XMR'].binance_status_at == t2.isoformat(timespec='seconds')

    def test_absence_records_unknown_never_delisted(self):
        """The asymmetry stays load-bearing: one failed fetch must not be able
        to condemn the universe."""
        rec = im.InstrumentRecord(id='XMR', ticker='XMR', cache_stem='XMR')
        out = im.apply_binance_venue_status({'XMR': rec}, {'XMR': 'unknown'})
        assert out['XMR'].binance_status == 'unknown'

    def test_unmatched_tickers_leave_the_record_untouched(self):
        rec = im.InstrumentRecord(id='BTC', ticker='BTC', cache_stem='BTC',
                                  binance_status='trading')
        out = im.apply_binance_venue_status({'BTC': rec}, {'ETH': 'suspended'})
        assert out['BTC'].binance_status == 'trading'

    def test_new_fields_survive_a_round_trip_and_old_files_still_load(self):
        """from_dict filters to known fields, so a store written before this
        change loads with defaults rather than raising."""
        rec = im.InstrumentRecord(id='XMR', ticker='XMR', cache_stem='XMR',
                                  binance_status='suspended',
                                  binance_status_at='2026-08-22T12:00:00')
        assert im.InstrumentRecord.from_dict(rec.to_dict()) == rec
        legacy = {'id': 'BTC', 'ticker': 'BTC', 'cache_stem': 'BTC', 'status': 'trading'}
        assert im.InstrumentRecord.from_dict(legacy).binance_status == 'unknown'
