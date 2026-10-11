"""A stale-history flag nobody reads is not a monitor.

`_is_history_stale` sets `data_stale` per asset; if nothing consumes it, a
batch of symbols can stop collecting (e.g. one exchange delist/migration) and
sit frozen in the universe for weeks. The signal is there on day one; the
monitor is what looks at it.

These tests pin the two properties that make it a monitor rather than a
decoration: the count is reported every cycle, and a BATCH of newly-stale
assets raises an alarm.

Tests below list the soon-to-be-stale symbols in the first cycle rather than
leaving them out, because leaving them out models "absent" as "healthy". The
property under test is a TRANSITION and a transition needs a prior state.
Holding the universe constant would let a real false positive through.

The alarm fires on the jump, not the standing total — ~25 assets are already
stale awaiting triage, and alarming on the absolute count would leave this
permanently red. A guard left red trains people to ignore guards.
"""

import pytest

from carrac import precompute


@pytest.fixture(autouse=True)
def _reset_watch():
    """Each test starts from a clean baseline."""
    precompute._stale_watch['count'] = None
    precompute._stale_watch['symbols'] = frozenset()
    precompute._stale_watch['seen'] = frozenset()
    yield
    precompute._stale_watch['count'] = None
    precompute._stale_watch['symbols'] = frozenset()
    precompute._stale_watch['seen'] = frozenset()


def _assets(stale=(), fresh=()):
    out = [{'symbol': s, 'data_stale': True} for s in stale]
    out += [{'symbol': s} for s in fresh]
    return out


class TestCountIsReported:
    def test_counts_stale_assets(self):
        s = precompute._report_stale_assets(
            _assets(stale=['MKR', 'EOS'], fresh=['BTC', 'ETH']), is_ltf=False)
        assert s['stale_count'] == 2
        assert s['total'] == 4

    def test_ltf_pass_does_not_report(self):
        """The LTF pass sees only the ~21-symbol hot tier; the HTF pass owns
        the number, or the two would fight over the baseline."""
        s = precompute._report_stale_assets(_assets(stale=['MKR']), is_ltf=True)
        assert s == {}

    def test_tracks_newly_stale_between_cycles(self):
        # Cycle 1 must list EOS as healthy. "EOS went stale"
        # presupposes EOS was evaluated and fine a cycle earlier; without that
        # it is merely absent, which is a different fact.
        precompute._report_stale_assets(
            _assets(stale=['MKR'], fresh=['BTC', 'EOS']), is_ltf=False)
        s = precompute._report_stale_assets(
            _assets(stale=['MKR', 'EOS'], fresh=['BTC']), is_ltf=False)
        assert s['newly_stale'] == ['EOS']

    def test_tracks_recovery(self):
        # Recovery means still evaluated and no longer stale, so
        # cycle 2 carries EOS as fresh. Dropping it from the result set is
        # disappearance, not recovery.
        precompute._report_stale_assets(_assets(stale=['MKR', 'EOS']), is_ltf=False)
        s = precompute._report_stale_assets(
            _assets(stale=['MKR'], fresh=['EOS']), is_ltf=False)
        assert s['recovered'] == ['EOS']


class TestAlarmFires:
    def test_batch_going_stale_alarms(self):
        """A clean cycle, then eleven symbols going stale at once."""
        batch = ['AGIX', 'BEAM', 'EOS', 'H', 'KAS', 'LRC',
                 'MKR', 'NTRN', 'OCEAN', 'RDNT', 'SIREN']
        # "a clean cycle" means these eleven were in the universe
        # and healthy, which is what a delisting batch looks like the day
        # before. Cycle 1 now says so instead of leaving them out.
        precompute._report_stale_assets(
            _assets(fresh=['BTC', 'ETH'] + batch), is_ltf=False)
        s = precompute._report_stale_assets(
            _assets(stale=batch, fresh=['BTC', 'ETH']), is_ltf=False)
        assert s['alarmed'] is True
        assert len(s['newly_stale']) == 11

    def test_first_cycle_after_restart_does_not_alarm(self):
        """No baseline means every stale asset looks new — that must not fire
        a false batch alarm on every worker restart."""
        s = precompute._report_stale_assets(
            _assets(stale=['A', 'B', 'C', 'D', 'E']), is_ltf=False)
        assert s['alarmed'] is False

    def test_single_asset_going_stale_does_not_alarm(self):
        """One asset drifting is normal churn, not a batch event."""
        precompute._report_stale_assets(_assets(fresh=['BTC']), is_ltf=False)
        s = precompute._report_stale_assets(_assets(stale=['MKR'], fresh=['BTC']), is_ltf=False)
        assert s['alarmed'] is False

    def test_standing_stale_count_does_not_re_alarm(self):
        """THE NOISE-SUPPRESSION PROPERTY. Once a batch has alarmed, the same assets
        staying stale must go quiet — otherwise it is red every 30 minutes
        forever and becomes noise people filter out."""
        batch = ['A', 'B', 'C', 'D']
        precompute._report_stale_assets(
            _assets(fresh=['BTC'] + batch), is_ltf=False)
        first = precompute._report_stale_assets(_assets(stale=batch, fresh=['BTC']), is_ltf=False)
        assert first['alarmed'] is True
        for _ in range(5):
            again = precompute._report_stale_assets(
                _assets(stale=batch, fresh=['BTC']), is_ltf=False)
            assert again['alarmed'] is False, 'standing staleness must not re-alarm'
            assert again['stale_count'] == 4, 'but the count must still be reported'

    def test_alarm_survives_a_broken_sentry(self, monkeypatch):
        """The alarm path must never take down the precompute cycle."""
        import sentry_sdk

        def _boom(*a, **kw):
            raise RuntimeError('sentry down')

        monkeypatch.setattr(sentry_sdk, 'capture_message', _boom)
        precompute._report_stale_assets(
            _assets(fresh=['BTC', 'A', 'B', 'C']), is_ltf=False)
        s = precompute._report_stale_assets(
            _assets(stale=['A', 'B', 'C'], fresh=['BTC']), is_ltf=False)
        assert s['alarmed'] is True   # still reported, despite Sentry failing


class TestSentryIsActuallyCalled:
    def test_capture_message_receives_the_batch(self, monkeypatch):
        """Guard the guard: assert the alarm reaches the reporting channel,
        not just that a dict field flipped."""
        import sentry_sdk
        seen = {}

        def _capture(msg, level=None):
            seen['msg'] = msg
            seen['level'] = level

        monkeypatch.setattr(sentry_sdk, 'capture_message', _capture)
        precompute._report_stale_assets(
            _assets(fresh=['BTC', 'MKR', 'EOS', 'KAS']), is_ltf=False)
        precompute._report_stale_assets(
            _assets(stale=['MKR', 'EOS', 'KAS'], fresh=['BTC']), is_ltf=False)

        assert seen.get('level') == 'error'
        assert 'MKR' in seen['msg'] and 'EOS' in seen['msg'] and 'KAS' in seen['msg']
        assert '3 assets went stale' in seen['msg']


class TestUniverseSizeChanges:
    """The universe is not a constant, and every test above holds
    it constant, which is why this got through.

    Example: after a worker recreate, cycle 1 evaluates 339 assets and finds
    4 stale; cycle 2 evaluates 355 and finds 9. The 5 extra were not newly
    stale (their newest bars are years old); they were simply absent from
    cycle 1's result set, and must not be reported as having gone stale in a
    single 30-minute cycle.
    """

    def test_assets_absent_last_cycle_are_not_newly_stale(self):
        """A smaller cycle, then a larger one whose newcomers were already
        stale."""
        precompute._report_stale_assets(
            _assets(stale=['BUTTHOLE', 'CHILLGUY', 'COLLAT', 'KAS'],
                    fresh=['BTC', 'ETH']), is_ltf=False)
        s = precompute._report_stale_assets(
            _assets(stale=['BUTTHOLE', 'CHILLGUY', 'COLLAT', 'KAS',
                           'BGB', 'GT', 'LBTC', 'LEO', 'PI'],
                    fresh=['BTC', 'ETH']), is_ltf=False)
        assert s['stale_count'] == 9, 'the standing count must still be honest'
        assert s['newly_stale'] == [], \
            'assets the previous cycle never evaluated did not go stale in it'
        assert s['alarmed'] is False, 'a growing universe is not a delisting'

    def test_a_real_batch_still_alarms_when_the_universe_grows(self):
        """The fix must not buy quiet by going blind: symbols that WERE
        evaluated and healthy last cycle still raise the alarm, even in the
        same cycle that the universe expands."""
        precompute._report_stale_assets(
            _assets(fresh=['BTC', 'ETH', 'A', 'B', 'C']), is_ltf=False)
        s = precompute._report_stale_assets(
            _assets(stale=['A', 'B', 'C', 'NEWCOMER'],
                    fresh=['BTC', 'ETH', 'ALSO_NEW']), is_ltf=False)
        assert s['alarmed'] is True, 'a real batch delisting must still fire'
        assert s['newly_stale'] == ['A', 'B', 'C'], \
            'NEWCOMER was never evaluated before, so it did not go stale'

    def test_assets_leaving_the_universe_are_not_recovered(self):
        """The mirror bug: a stale symbol dropping out of the result set must
        not be reported as having recovered."""
        precompute._report_stale_assets(
            _assets(stale=['GONE', 'STAYS'], fresh=['BTC']), is_ltf=False)
        s = precompute._report_stale_assets(
            _assets(stale=['STAYS'], fresh=['BTC']), is_ltf=False)
        assert s['recovered'] == [], \
            'a symbol that vanished from the universe did not recover'
