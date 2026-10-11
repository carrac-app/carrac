"""The delisting gate.

Collection: a delisted symbol stops being collected and stops being published.
Ingest: a bar offered for a non-tradeable instrument is rejected at INGEST.

The decision these tests pin (a deliberate decision): the curated
list ``instrument_master.LIFECYCLE_OVERRIDES`` is the ONLY writer of
``status='delisted'``. The venue-suspended-AND-stale conjunction never writes
a status — it proposes candidates to a human and stops there.

Why that separation is the thing under test rather than an implementation
detail: the delisting gate halts collection, so a wrongly-condemned symbol can never produce
the fresh bar that would clear it. A 14-day threshold would condemn OCEAN,
NTRN, RNDR and FTM together — four instruments whose bars all stopped within
a few days of each other, i.e. one source-side event rather than four delistings.

Nothing here monkeypatches the unit under test. ``send_ntfy`` is stubbed at the
outbound boundary only, to capture what would have been sent.
"""
import datetime
import logging
from pathlib import Path

import pandas as pd

import instrument_master as im


# Four symbols Binance reports BREAK for that are ALIVE —
# current bars, real volume, trading on venues Carrac does read. They are the
# control group for every rule here: any change that condemns one of them is
# a defect.
ALIVE_UNDER_BREAK = ('BEAM', 'LIT', 'RDNT', 'XMR')


def _rec(ticker, *, binance_status='unknown', last_bar=None, status='unknown',
         source='cryptocompare', candidate_since=None):
    stem = im._cache_stem(ticker)
    return im.InstrumentRecord(
        id=stem, ticker=ticker, cache_stem=stem, status=status,
        tracking_state='tracked', venue=source, authoritative_source=source,
        last_bar_date=last_bar, binance_status=binance_status,
        delist_candidate_since=candidate_since,
    )


def _store(*records):
    return {r.id: r for r in records}


# ---------------------------------------------------------------------------
# The curated list itself
# ---------------------------------------------------------------------------

class TestCuratedList:
    def test_every_entry_is_well_formed(self):
        for ticker, entry in im.LIFECYCLE_OVERRIDES.items():
            assert entry['status'] in im.STATUSES, ticker
            assert entry['reason'] in im.LIFECYCLE_REASONS, ticker
            assert entry['note'], ticker
            # An effective date that does not parse would silently become the
            # `last_bar_date` of a published staleness block.
            datetime.date.fromisoformat(entry['effective_date'])

    def test_the_live_four_are_not_on_it(self):
        """The pin that matters most.

        BEAM, LIT, RDNT and XMR are all BREAK on Binance. Collapsing the
        venue's verdict into the instrument's status would mark four live
        assets dead — they all trade with real daily volume.
        """
        for ticker in ALIVE_UNDER_BREAK:
            assert not im.is_delisted(ticker), (
                f'{ticker} trades actively elsewhere — a venue BREAK is not a '
                f'delisting'
            )

    def test_lookup_accepts_either_form_of_the_name(self):
        assert im.is_delisted('AGIX')
        assert im.is_delisted('agix')
        assert im.is_delisted(im._cache_stem('AGIX'))
        assert not im.is_delisted('BTC')
        assert not im.is_delisted(None)

    def test_delisted_tickers_matches_the_overrides(self):
        assert im.delisted_tickers() == {
            t for t, e in im.LIFECYCLE_OVERRIDES.items()
            if e['status'] == 'delisted'
        }


class TestOverridesAreTheFinalWord:
    def test_a_curated_verdict_outranks_the_venue(self):
        """Order is load-bearing. LRC is 'suspended' to Binance and 'delisted'
        to us; whichever call runs last decides, so the collector must apply
        the overrides AFTER the venue ingestion."""
        records = _store(_rec('LRC', source='binance', last_bar='2026-06-09'))
        records = im.apply_status_updates(records, {'LRC': 'suspended'})
        assert records['LRC'].status == 'suspended'

        records = im.apply_lifecycle_overrides(records)
        assert records['LRC'].status == 'delisted'

    def test_the_collector_applies_them_in_that_order(self):
        """rg the call site, not the intent — a correct function called too
        early is the whole bug."""
        src = Path('carrac/precompute.py').read_text()
        start = src.index('def _write_instrument_master(')
        body = src[start:src.index('def _alt_season_alarm(')]
        assert body.index('apply_status_updates') < body.index('apply_lifecycle_overrides')
        assert body.index('apply_lifecycle_overrides') < body.index('write_store')

    def test_reconcile_stamps_them_too(self, tmp_path):
        """A caller that only reconciles — no network — still gets the
        curated verdicts, so the store is never briefly wrong."""
        (tmp_path / 'AGIX_daily.csv').write_text(
            'timestamp,open,high,low,close,volume\n2024-07-08,1,1,1,1,0\n')
        records = im.reconcile(cache_dir=tmp_path, data_dir=tmp_path)
        assert records['AGIX'].status == 'delisted'

    def test_an_unknown_status_in_an_override_is_ignored_not_written(self):
        """Guards the store's vocabulary at the point of application, so a
        typo cannot invent a status every downstream consumer will miss."""
        records = _store(_rec('BTC', status='trading', source='binance'))
        try:
            im._LIFECYCLE_OVERRIDES_BY_STEM['BTC'] = {
                'ticker': 'BTC', 'status': 'retired-ish', 'reason': 'migrated',
            }
            out = im.apply_lifecycle_overrides(records)
            assert out['BTC'].status == 'trading'
        finally:
            im._LIFECYCLE_OVERRIDES_BY_STEM.pop('BTC', None)


# ---------------------------------------------------------------------------
# The proposer — the conjunction, and what it refuses to do
# ---------------------------------------------------------------------------

NOW = datetime.datetime(2026, 8, 23, 12, 0)


class TestDelistCandidates:
    def test_both_halves_are_required(self):
        records = _store(
            # suspended AND long stale -> the only candidate
            _rec('DEADCOIN', binance_status='suspended', last_bar='2026-06-01'),
            # suspended but current -> the BEAM/LIT/RDNT/XMR shape
            _rec('ALIVECOIN', binance_status='suspended', last_bar='2026-08-23'),
            # ancient but the venue never SAID anything -> absence is not evidence
            _rec('QUIETCOIN', binance_status='unknown', last_bar='2024-01-01'),
            # trading and current
            _rec('BTC', binance_status='trading', last_bar='2026-08-23'),
        )
        got = [c['ticker'] for c in im.delist_candidates(records, now=NOW)]
        assert got == ['DEADCOIN']

    def test_absence_alone_never_proposes_anything(self):
        """One failed exchangeInfo fetch maps every Binance-routed ticker to
        'unknown'. If that could propose, a network blip would nominate the
        whole universe for condemnation."""
        records = _store(*[
            _rec(f'SYM{i}', binance_status='unknown', last_bar='2020-01-01')
            for i in range(50)
        ])
        assert im.delist_candidates(records, now=NOW) == []

    def test_the_threshold_clears_the_cryptocompare_cluster(self):
        """The measured near-miss. OCEAN, NTRN, RNDR and FTM all stopped
        within a few days of each other — one source-side event. At the shipped
        floor none of them is proposed; at 14 days all four would be."""
        cluster = _store(
            _rec('OCEAN2', binance_status='suspended', last_bar='2026-08-08'),
            _rec('NTRN2', binance_status='suspended', last_bar='2026-08-07'),
            _rec('RNDR2', binance_status='suspended', last_bar='2026-08-07'),
            _rec('FTM2', binance_status='suspended', last_bar='2026-08-04'),
        )
        assert im.delist_candidates(cluster, now=NOW) == []
        assert len(im.delist_candidates(cluster, now=NOW, min_stale_days=14)) == 4

    def test_already_curated_symbols_are_not_re_proposed(self):
        records = _store(_rec('AGIX', binance_status='suspended',
                              last_bar='2024-07-08'))
        assert im.delist_candidates(records, now=NOW) == []

    def test_an_unparseable_bar_date_is_not_a_proposal(self):
        records = _store(
            _rec('BAD', binance_status='suspended', last_bar='not-a-date'),
            _rec('NONE', binance_status='suspended', last_bar=None),
        )
        assert im.delist_candidates(records, now=NOW) == []

    def test_the_findings_carry_their_evidence(self):
        records = _store(_rec('DEADCOIN', binance_status='suspended',
                              last_bar='2026-06-01', source='cryptocompare'))
        c = im.delist_candidates(records, now=NOW)[0]
        assert c['age_days'] == 83
        assert c['last_bar_date'] == '2026-06-01'
        assert c['authoritative_source'] == 'cryptocompare'


class TestCandidacyIsEdgeTriggered:
    def test_reported_once_then_silent(self):
        records = _store(_rec('DEADCOIN', binance_status='suspended',
                              last_bar='2026-06-01'))
        cands = im.delist_candidates(records, now=NOW)

        records, new = im.mark_delist_candidates(records, cands, now=NOW)
        assert [c['ticker'] for c in new] == ['DEADCOIN']
        assert records['DEADCOIN'].delist_candidate_since is not None

        records, new = im.mark_delist_candidates(
            records, im.delist_candidates(records, now=NOW), now=NOW)
        assert new == [], 'a standing condition re-announced every cycle is noise'

    def test_recovery_clears_it_so_a_relapse_is_reported_again(self):
        records = _store(_rec('DEADCOIN', binance_status='suspended',
                              last_bar='2026-06-01'))
        records, _ = im.mark_delist_candidates(
            records, im.delist_candidates(records, now=NOW), now=NOW)

        # Bars start arriving again.
        records['DEADCOIN'] = im.replace(records['DEADCOIN'],
                                         last_bar_date='2026-08-23')
        records, new = im.mark_delist_candidates(
            records, im.delist_candidates(records, now=NOW), now=NOW)
        assert new == []
        assert records['DEADCOIN'].delist_candidate_since is None

        # ...and stop again.
        records['DEADCOIN'] = im.replace(records['DEADCOIN'],
                                         last_bar_date='2026-06-01')
        records, new = im.mark_delist_candidates(
            records, im.delist_candidates(records, now=NOW), now=NOW)
        assert [c['ticker'] for c in new] == ['DEADCOIN']


class TestTheProposerAlarmReachesAHuman:
    def test_it_pages_and_beacons(self, monkeypatch, caplog):
        import carrac.notifications.ntfy as ntfy_mod
        import carrac.precompute as pc

        sent = []
        monkeypatch.setattr(ntfy_mod, 'send_ntfy',
                            lambda **kw: sent.append(kw) or True)
        with caplog.at_level(logging.ERROR):
            pc._alarm_on_delist_candidates([{
                'ticker': 'DEADCOIN', 'last_bar_date': '2026-06-01',
                'age_days': 83, 'authoritative_source': 'cryptocompare',
            }])

        assert len(sent) == 1, 'log-only is the alarm nobody reads'
        assert 'DEADCOIN' in sent[0]['body']
        assert any(r.levelno >= logging.ERROR for r in caplog.records), (
            'the Bugsink half is missing'
        )
        # It must say it changed nothing, or the reader assumes it did.
        assert 'Nothing has been changed' in sent[0]['body']

    def test_nothing_is_sent_on_a_normal_day(self, monkeypatch):
        """The question asked of this alarm: what does it look like
        when everything is fine? Empty. No page, no error-tracker issue."""
        import carrac.notifications.ntfy as ntfy_mod
        import carrac.precompute as pc

        sent = []
        monkeypatch.setattr(ntfy_mod, 'send_ntfy',
                            lambda **kw: sent.append(kw) or True)
        pc._alarm_on_delist_candidates([])
        assert sent == []

    def test_the_live_store_proposes_nothing_today(self):
        """With the nine curated, the four remaining BREAK symbols are all
        current — so the shipped configuration is silent, not dormant-because-
        broken."""
        records = _store(
            *[_rec(t, binance_status='suspended', last_bar='2026-08-23')
              for t in ALIVE_UNDER_BREAK],
            *[_rec(t, binance_status='suspended',
                   last_bar=im.LIFECYCLE_OVERRIDES[t]['effective_date'])
              for t in im.LIFECYCLE_OVERRIDES],
        )
        assert im.delist_candidates(records, now=NOW) == []


# ---------------------------------------------------------------------------
# Collection stops
# ---------------------------------------------------------------------------

class TestCollectionGate:
    def test_delisted_symbols_leave_the_work_list(self):
        from carrac.precompute import _drop_delisted
        assert _drop_delisted(['BTC', 'AGIX', 'ETH', 'LRC']) == ['BTC', 'ETH']

    def test_the_live_four_survive_it(self):
        from carrac.precompute import _drop_delisted
        assert _drop_delisted(list(ALIVE_UNDER_BREAK)) == list(ALIVE_UNDER_BREAK)

    def test_a_broken_gate_fails_open(self, monkeypatch):
        """Collecting a delisted symbol wastes one fetch. Skipping a live one
        blanks the board — so the failure has to land on the cheap side."""
        from carrac import precompute as pc

        monkeypatch.setattr(im, 'is_delisted',
                            lambda s: (_ for _ in ()).throw(RuntimeError('boom')))
        assert pc._drop_delisted(['BTC', 'AGIX']) == ['BTC', 'AGIX']

    def test_the_gate_runs_after_the_manifest_and_the_store(self):
        """Filtering earlier would drop delisted symbols out of the manifest,
        which marks their cache files orphaned and loses the record that
        explains why they stopped."""
        src = Path('carrac/precompute.py').read_text()
        assert (src.index('_write_tracked_symbols_manifest(symbols)')
                < src.index('_write_instrument_master()')
                < src.index('symbols = _drop_delisted(symbols)'))

    def test_retention_is_never_touched(self):
        """The survivorship rule, pinned in code rather than in a comment:
        nothing on the gate path deletes anything."""
        src = Path('carrac/precompute.py').read_text()
        start = src.index('def _drop_delisted(')
        body = src[start:src.index('# LTF hot tier')]
        for forbidden in ('unlink', 'remove(', 'rmtree', 'DELETE'):
            assert forbidden not in body


# ---------------------------------------------------------------------------
# Publishing stops
# ---------------------------------------------------------------------------

def _fresh_daily(n=40, end='2026-08-23'):
    idx = pd.date_range(end=end, periods=n, freq='D')
    return pd.DataFrame(
        {'open': 1.0, 'high': 1.1, 'low': 0.9, 'close': 1.05, 'volume': 100.0},
        index=idx,
    )


class TestPublishingGate:
    def test_a_delisted_symbol_is_answered_from_reference_data(self):
        """Even with perfectly fresh bars. The age of the bars is not the
        question for a delisted instrument — and a delisted symbol whose last
        bar happened to be four days old would otherwise publish as healthy."""
        from carrac.precompute import describe_staleness

        block = describe_staleness(_fresh_daily(), 'AGIX', now=NOW)
        assert block is not None
        assert block['reason'] == 'delisted'
        assert block['successor'] == 'FET'

    def test_a_live_symbol_with_fresh_bars_is_still_fresh(self):
        from carrac.precompute import describe_staleness
        assert describe_staleness(_fresh_daily(), 'BTC', now=NOW) is None
        for ticker in ALIVE_UNDER_BREAK:
            assert describe_staleness(_fresh_daily(), ticker, now=NOW) is None

    def test_the_block_says_history_is_retained(self):
        from carrac.staleness import delisted_block

        block = delisted_block('LRC')
        assert 'retained' in block['note']
        assert block['delisting_reason'] == 'venue_delisted'
        assert block['last_bar_date'] == '2026-06-09'

    def test_it_suppresses_derived_readings(self):
        from carrac.staleness import delisted_block, history_is_dead
        assert history_is_dead(delisted_block('AGIX'))

    def test_a_live_symbol_gets_no_block(self):
        from carrac.staleness import delisted_block
        assert delisted_block('BTC') is None
        assert delisted_block(None) is None

    def test_a_broken_lookup_falls_through_to_ordinary_staleness(self, monkeypatch):
        """Fail OPEN here too — a lookup fault must not suppress the
        no_recent_bars block that would otherwise fire."""
        from carrac.staleness import delisted_block

        monkeypatch.setattr(im, 'lifecycle_override_for',
                            lambda s: (_ for _ in ()).throw(RuntimeError('boom')))
        assert delisted_block('AGIX') is None

    def test_the_composite_surface_stamps_it_without_the_snapshot(self):
        """A delisted symbol is no longer IN the HTF snapshot, so there is no
        flag left to carry — while the live-price feed can still answer. That
        is the shape of a series that has to be rebuilt from reference data."""
        from carrac.composite.assemblers import _extract_market

        result = _extract_market('AGIX', {'AGIX': {'price': 0.31}}, {})
        assert result['data_stale']['reason'] == 'delisted'

    def test_the_mcp_surface_asks_before_the_no_history_fallback(self):
        """`no_history_block` would say "no price history is available", which
        is both wrong (222 rows are retained) and silent about the reason."""
        src = Path('carrac/mcp/tools/market.py').read_text()
        assert src.index('_delisted = delisted_block(symbol)') < src.index(
            "result['data_stale'] = no_history_block(symbol)")


# ---------------------------------------------------------------------------
# Rejected at ingest
# ---------------------------------------------------------------------------

class TestIngestGate:
    def test_a_delisted_write_is_refused_and_the_file_is_untouched(
            self, tmp_path, caplog):
        from csv_cache import CSVCache

        cache = CSVCache(cache_dir=str(tmp_path))
        path = cache.get_path('AGIX', 'daily')
        path.write_text('timestamp,open,high,low,close,volume\n'
                        '2024-07-08,1,1,1,1,5\n')
        before = path.read_text()

        with caplog.at_level(logging.ERROR):
            cache.save('AGIX', _fresh_daily(), 'daily')

        assert path.read_text() == before, 'a refused write still changed the file'
        assert any('DELISTED' in r.getMessage() for r in caplog.records)

    def test_a_live_symbol_still_writes(self, tmp_path):
        from csv_cache import CSVCache

        cache = CSVCache(cache_dir=str(tmp_path))
        cache.save('BTC', _fresh_daily(), 'daily')
        assert cache.get_path('BTC', 'daily').exists()

    def test_a_suspended_pair_is_accepted(self, tmp_path):
        """The decision that had to be written down. Binance's BREAK is a
        halted session, not an ended instrument: a halted pair resumes, and can
        receive corrections to bars traded before the halt. Refusing those
        leaves the cache permanently wrong with no path back — for a state the
        venue itself calls temporary."""
        from csv_cache import CSVCache

        assert not im.is_delisted('XMR')  # BREAK on Binance, trading elsewhere
        cache = CSVCache(cache_dir=str(tmp_path))
        cache.save('XMR', _fresh_daily(), 'daily')
        assert cache.get_path('XMR', 'daily').exists()

    def test_the_guard_sits_at_the_chokepoint_every_writer_passes(self):
        """The delisting filter only covers the precompute cycle. The WS writers,
        cache_manager backfills and hand-run scripts all reach the cache
        through save(), which is why the ingest guard is not redundant."""
        src = Path('csv_cache.py').read_text()
        body = src[src.index('    def save(self'):src.index('    def _reject_persisted_synthetic_bars(')]
        assert 'is_delisted(symbol)' in body
        assert body.index('if df.empty') < body.index('is_delisted(symbol)')


# ---------------------------------------------------------------------------
# The morning cron
# ---------------------------------------------------------------------------

class TestCronStopsPagingAboutCuratedDelistings:
    def test_a_delisted_series_produces_no_stale_finding(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            'cache_integrity_check', 'scripts/cache_integrity_check.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        today = datetime.date(2026, 8, 23)
        dates = [datetime.date(2024, 7, 8) - datetime.timedelta(days=i)
                 for i in range(60)]
        symbol_dates = {'AGIX': dates}
        tracked = {'AGIX'}

        loud = mod.frozen_series(symbol_dates, today=today, tracked=tracked,
                                 real_tail={'AGIX': datetime.date(2024, 7, 8)},
                                 derived=set(), delisted=set())
        assert loud, 'the check no longer detects a frozen series at all'

        quiet = mod.frozen_series(symbol_dates, today=today, tracked=tracked,
                                  real_tail={'AGIX': datetime.date(2024, 7, 8)},
                                  derived=set(), delisted={'AGIX'})
        assert quiet == []

    def test_a_missing_store_fails_loud_not_silent(self):
        """No store means no suppression set, so the findings come back —
        the check degrades toward noise rather than toward silence."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            'cache_integrity_check', 'scripts/cache_integrity_check.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        today = datetime.date(2026, 8, 23)
        dates = [datetime.date(2024, 7, 8) - datetime.timedelta(days=i)
                 for i in range(60)]
        assert mod.frozen_series({'AGIX': dates}, today=today, tracked={'AGIX'},
                                 real_tail={'AGIX': datetime.date(2024, 7, 8)},
                                 derived=set(), delisted=None)
