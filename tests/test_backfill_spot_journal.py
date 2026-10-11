import pytest

from scripts.backfill_spot_journal import (
    ACTION_CREATE_THEN_STAMP,
    ACTION_STAMP_ONLY,
    _load_user_drawings,
    apply_backfill,
    plan_backfill,
)


def _drawing(did, symbol='BTC', qty=0.5, price=50000.0, sold=False,
             journal_id=None):
    """A legacy spot drawing. `journal_id` set => already promoted."""
    ext = {'quantity': qty, 'usdAmount': (qty * price) if qty else None,
           'sold': sold}
    if journal_id is not None:
        ext['_journalDrawingId'] = journal_id
    return {'id': did, 'symbol': symbol, 'name': 'spot',
            'points': [{'value': price}], 'extendData': ext}


def test_a_drawing_already_promoted_by_the_live_path_is_skipped():
    # The live path stamps extendData._journalDrawingId on promotion. Re-promoting
    # such a drawing would double-count the holding on /portfolio.
    assert plan_backfill([_drawing(1, journal_id=1738000000001)], []) == []


def test_plans_one_trade_per_unpromoted_spot_drawing():
    plan = plan_backfill([_drawing(1), _drawing(2, 'ETH', 2.0, 3000.0)], [])
    assert len(plan) == 2
    assert plan[0] == {'action': ACTION_CREATE_THEN_STAMP, 'drawing_id': 1,
                       'asset': 'BTC', 'entry_price': 50000.0, 'size': 0.5}
    assert plan[1] == {'action': ACTION_CREATE_THEN_STAMP, 'drawing_id': 2,
                       'asset': 'ETH', 'entry_price': 3000.0, 'size': 2.0}


def test_orphaned_trade_is_planned_for_stamp_only_repair_not_a_second_create():
    # drawing 1's trade already exists (its own id is a drawing_id on an
    # existing trade), but drawing 1 itself carries no _journalDrawingId.
    # The primary gate did NOT catch it, so this is NOT "already fully
    # done" — it is an orphan left by a prior run whose write-back failed
    # after the trade was created (e.g. a version conflict). It must be
    # repaired (stamp-only), never re-created as a second trade. Drawing 2
    # is genuinely new and still gets a normal create_then_stamp entry.
    existing = [{'id': 99, 'drawing_id': 1}]
    plan = plan_backfill([_drawing(1), _drawing(2)], existing)

    assert plan == [
        {'action': ACTION_STAMP_ONLY, 'drawing_id': 1, 'asset': 'BTC'},
        {'action': ACTION_CREATE_THEN_STAMP, 'drawing_id': 2, 'asset': 'BTC',
         'entry_price': 50000.0, 'size': 0.5},
    ]


def test_orphan_repair_matches_when_existing_drawing_id_is_a_string():
    # tests/schema/carrac_pg_schema.sql declares trade_journal.drawing_id
    # as `text`. psycopg2 returns that column as a Python str on real
    # Postgres, so `existing_trades` here carries drawing_id='1' (str), not
    # 1 (int) — while `drawing.get('id')` (the drawing table's own PK) is
    # always a genuine int. A plain `in` membership test against a set of
    # ints would never match a str, so on real Postgres this orphan would
    # be silently re-classified as create_then_stamp and get a SECOND trade
    # for the same holding — the exact double-count this repair path exists
    # to prevent. SQLite's INTEGER affinity round-trips a Python int and
    # hides this completely, which is why this test builds the string form
    # by hand rather than trusting a real DB round-trip.
    existing = [{'id': 99, 'drawing_id': '1'}]
    plan = plan_backfill([_drawing(1), _drawing(2)], existing)

    assert plan == [
        {'action': ACTION_STAMP_ONLY, 'drawing_id': 1, 'asset': 'BTC'},
        {'action': ACTION_CREATE_THEN_STAMP, 'drawing_id': 2, 'asset': 'BTC',
         'entry_price': 50000.0, 'size': 0.5},
    ]


def test_a_second_run_over_a_fully_successful_first_run_plans_nothing():
    # A genuinely completed run both creates the trade AND stamps the
    # drawing (that's what apply_backfill's write-back does on success —
    # see test_apply_backfill_creates_the_trade_and_writes_the_id_back...
    # below). Only THAT combined state is a true no-op on a second run;
    # see test_orphaned_trade_is_planned_for_stamp_only_repair_not_a_second_create
    # for what happens when only the trade half landed.
    drawings = [_drawing(1), _drawing(2)]
    first = plan_backfill(drawings, [])
    produced = [{'id': 100 + i, 'drawing_id': p['drawing_id']}
                for i, p in enumerate(first)]
    stamped_drawings = [_drawing(p['drawing_id'], journal_id=p['drawing_id'])
                        for p in first]

    assert plan_backfill(stamped_drawings, produced) == []


def test_sold_drawings_are_not_resurrected_as_open_positions():
    assert plan_backfill([_drawing(1, sold=True)], []) == []


def test_drawings_without_a_usable_quantity_are_skipped():
    assert plan_backfill([_drawing(1, qty=0)], []) == []
    assert plan_backfill([_drawing(1, qty=None)], []) == []


def test_non_spot_drawings_are_ignored():
    position = {'id': 5, 'symbol': 'BTC', 'name': 'longPosition',
                'points': [{'value': 1}, {'value': 2}, {'value': 3}]}
    assert plan_backfill([position], []) == []


class _FakeDb:
    """Stand-in for ContentDatabase — enough surface for apply_backfill."""
    UPDATE_OK = 'ok'
    UPDATE_CONFLICT = 'conflict'

    def __init__(self, fail_drawing_update=False, fail_trade_update=False):
        self.trades = {}
        self.drawing_updates = []
        self._next_trade_id = 1
        self._fail_drawing_update = fail_drawing_update
        self._fail_trade_update = fail_trade_update

    def log_trade_open(self, **kwargs):
        journal_id = self._next_trade_id
        self._next_trade_id += 1
        self.trades[journal_id] = dict(kwargs)
        return journal_id

    def trade_update(self, journal_id, **kwargs):
        if self._fail_trade_update:
            return False
        self.trades[journal_id].update(kwargs)
        return True

    def drawing_update(self, drawing_id, user_id=None, metadata=None,
                       expected_version=None, **kwargs):
        self.drawing_updates.append({
            'drawing_id': drawing_id, 'user_id': user_id, 'metadata': metadata,
            'expected_version': expected_version,
        })
        if self._fail_drawing_update:
            return (self.UPDATE_CONFLICT, (expected_version or 0) + 1)
        return (self.UPDATE_OK, (expected_version or 1) + 1)


def test_apply_backfill_creates_the_trade_and_writes_the_id_back_onto_the_drawing():
    # The write-back is the whole point of this task: without it, a later
    # sell/delete can't find the trade (those paths key off this exact
    # field), it stays open forever, and /portfolio overstates the book.
    db = _FakeDb()
    raw = {1: {'id': 1, 'metadata': {
        'kline_name': 'spot', 'local_id': 'overlay_x',
        'points': [{'value': 50000.0}],
        'extend_data': {'quantity': 0.5, 'usdAmount': 25000.0, 'sold': False},
    }}}
    plan = [{'action': ACTION_CREATE_THEN_STAMP, 'drawing_id': 1,
             'asset': 'BTC', 'entry_price': 50000.0, 'size': 0.5}]

    written = apply_backfill(db, user_id=7, plan=plan, raw_drawings_by_id=raw)

    assert written == 1
    [(journal_id, trade)] = db.trades.items()
    assert trade['drawing_id'] == 1
    assert trade['asset'] == 'BTC'
    assert trade['size'] == 0.5
    assert trade['account_tag'] == 'spot'

    assert len(db.drawing_updates) == 1
    written_meta = db.drawing_updates[0]['metadata']
    assert written_meta['extend_data']['_journalDrawingId'] == 1
    # sibling metadata keys must survive the write-back, not just extend_data
    assert written_meta['extend_data']['quantity'] == 0.5
    assert written_meta['local_id'] == 'overlay_x'
    assert written_meta['kline_name'] == 'spot'


def test_apply_backfill_raises_loudly_when_the_write_back_fails():
    # A swallowed failure here is exactly the "half-promoted, invisible to
    # future sells" defect the brief calls out — it must not be silent.
    db = _FakeDb(fail_drawing_update=True)
    raw = {1: {'id': 1, 'metadata': {'extend_data': {'quantity': 0.5}}}}
    plan = [{'action': ACTION_CREATE_THEN_STAMP, 'drawing_id': 1,
             'asset': 'BTC', 'entry_price': 50000.0, 'size': 0.5}]

    with pytest.raises(RuntimeError):
        apply_backfill(db, user_id=7, plan=plan, raw_drawings_by_id=raw)

    # The trade WAS created even though the write-back failed — the raise
    # surfaces exactly this partial state instead of hiding it.
    assert len(db.trades) == 1


def test_dry_run_writes_nothing():
    db = _FakeDb()
    plan = [{'action': ACTION_CREATE_THEN_STAMP, 'drawing_id': 1,
             'asset': 'BTC', 'entry_price': 50000.0, 'size': 0.5}]

    written = apply_backfill(db, user_id=7, plan=plan, raw_drawings_by_id={}, dry_run=True)

    assert written == 0
    assert db.trades == {}
    assert db.drawing_updates == []


def test_apply_backfill_raises_when_trade_update_returns_false():
    # A False return (allowlist drift, a lookup miss) must not be silently
    # ignored: the trade would be created with no size — permanently
    # invisible to /portfolio valuation — while the write-back would still
    # mark the drawing as promoted, making it un-repairable by a re-run.
    db = _FakeDb(fail_trade_update=True)
    raw = {1: {'id': 1, 'metadata': {'extend_data': {'quantity': 0.5}}}}
    plan = [{'action': ACTION_CREATE_THEN_STAMP, 'drawing_id': 1,
             'asset': 'BTC', 'entry_price': 50000.0, 'size': 0.5}]

    with pytest.raises(RuntimeError, match='trade_update'):
        apply_backfill(db, user_id=7, plan=plan, raw_drawings_by_id=raw)

    # The trade WAS created (log_trade_open succeeded) but the write-back
    # must never have been attempted — the drawing must not be stamped as
    # promoted while its trade is missing size/account_tag.
    assert len(db.trades) == 1
    assert db.drawing_updates == []


def test_apply_backfill_passes_the_snapshotted_version_as_the_concurrency_guard():
    db = _FakeDb()
    raw = {1: {'id': 1, 'version': 3,
               'metadata': {'extend_data': {'quantity': 0.5}}}}
    plan = [{'action': ACTION_CREATE_THEN_STAMP, 'drawing_id': 1,
             'asset': 'BTC', 'entry_price': 50000.0, 'size': 0.5}]

    apply_backfill(db, user_id=7, plan=plan, raw_drawings_by_id=raw)

    assert db.drawing_updates[0]['expected_version'] == 3


def test_apply_backfill_raises_loudly_on_a_concurrency_conflict():
    # Something else (another device syncing, a live chart session) wrote
    # to this drawing between our read and our write-back. A blind
    # whole-column overwrite would silently discard that change; the
    # expected_version guard must refuse instead, and we must not eat it.
    db = _FakeDb(fail_drawing_update=True)
    raw = {1: {'id': 1, 'version': 3,
               'metadata': {'extend_data': {'quantity': 0.5}}}}
    plan = [{'action': ACTION_CREATE_THEN_STAMP, 'drawing_id': 1,
             'asset': 'BTC', 'entry_price': 50000.0, 'size': 0.5}]

    with pytest.raises(RuntimeError, match='concurrency race'):
        apply_backfill(db, user_id=7, plan=plan, raw_drawings_by_id=raw)


def test_apply_backfill_logs_the_partial_tally_before_raising(caplog):
    # Two items in the plan; the second's write-back fails. The operator
    # needs to know "1 succeeded" from the log, not just see a traceback.
    class _FailsOnSecondDrawingUpdate(_FakeDb):
        def drawing_update(self, drawing_id, **kwargs):
            if drawing_id == 2:
                self.drawing_updates.append({'drawing_id': drawing_id, **kwargs})
                return (self.UPDATE_CONFLICT, 99)
            return super().drawing_update(drawing_id, **kwargs)

    db = _FailsOnSecondDrawingUpdate()
    raw = {
        1: {'id': 1, 'version': 1, 'metadata': {'extend_data': {}}},
        2: {'id': 2, 'version': 1, 'metadata': {'extend_data': {}}},
    }
    plan = [
        {'action': ACTION_CREATE_THEN_STAMP, 'drawing_id': 1, 'asset': 'BTC',
         'entry_price': 50000.0, 'size': 0.5},
        {'action': ACTION_CREATE_THEN_STAMP, 'drawing_id': 2, 'asset': 'ETH',
         'entry_price': 3000.0, 'size': 2.0},
    ]

    with caplog.at_level('ERROR'):
        with pytest.raises(RuntimeError):
            apply_backfill(db, user_id=7, plan=plan, raw_drawings_by_id=raw)

    assert any('1 item(s)' in r.message for r in caplog.records)


def test_apply_backfill_repair_action_does_not_create_a_second_trade():
    # A stamp_only item means the trade already exists (plan_backfill only
    # emits this when it found the drawing's id already on an existing
    # trade). apply_backfill must NEVER call log_trade_open for it.
    db = _FakeDb()
    raw = {1: {'id': 1, 'version': 2, 'metadata': {
        'kline_name': 'spot', 'local_id': 'overlay_x',
        'points': [{'value': 50000.0}],
        'extend_data': {'quantity': 0.5, 'sold': False},
    }}}
    plan = [{'action': ACTION_STAMP_ONLY, 'drawing_id': 1, 'asset': 'BTC'}]

    written = apply_backfill(db, user_id=7, plan=plan, raw_drawings_by_id=raw)

    assert written == 1
    assert db.trades == {}, 'a repair must never call log_trade_open'
    assert len(db.drawing_updates) == 1
    assert db.drawing_updates[0]['metadata']['extend_data']['_journalDrawingId'] == 1
    assert db.drawing_updates[0]['expected_version'] == 2


def test_full_sequence_conflict_orphans_then_repairs_then_true_noop():
    """Traces exactly the recovery path the write-back's error message
    promises, end to end across three simulated runs:

    Run 1: a version conflict during the write-back. The trade IS created
           (log_trade_open + trade_update succeed); the drawing is left
           unstamped — an orphan.
    Run 2: a fresh plan sees the trade already exists but the drawing is
           unstamped -> plans a stamp_only repair (NOT a skip, NOT a
           second create). The conflicting writer is gone now, so the
           repair succeeds.
    Run 3: the drawing is now genuinely stamped -> the primary gate skips
           it -> a true no-op. The orphan does not resurrect.
    """
    raw_drawing_row = {
        'id': 1, 'version': 1,
        'metadata': {
            'kline_name': 'spot', 'points': [{'value': 50000.0}],
            'extend_data': {'quantity': 0.5, 'usdAmount': 25000.0, 'sold': False},
        },
    }
    drawing = _drawing(1)  # unstamped, as read at the start of run 1

    # --- Run 1: version conflict during the write-back -----------------
    db = _FakeDb(fail_drawing_update=True)
    plan_1 = plan_backfill([drawing], existing_trades=[])
    assert [p['action'] for p in plan_1] == [ACTION_CREATE_THEN_STAMP]

    with pytest.raises(RuntimeError, match='concurrency race'):
        apply_backfill(db, user_id=7, plan=plan_1, raw_drawings_by_id={1: raw_drawing_row})

    assert len(db.trades) == 1, 'the trade WAS created — this is the orphan'
    assert drawing['extendData'].get('_journalDrawingId') is None, \
        'the write-back failed, so the drawing was never stamped'

    # --- Run 2: a fresh run derives existing_trades from the DB as it now
    # stands; the conflicting writer is done, so the repair can land. ----
    existing_after_run_1 = [{'drawing_id': t['drawing_id']} for t in db.trades.values()]
    plan_2 = plan_backfill([drawing], existing_after_run_1)
    assert [p['action'] for p in plan_2] == [ACTION_STAMP_ONLY], \
        'not a skip — the trade exists but the drawing is unstamped'

    db._fail_drawing_update = False
    written_run_2 = apply_backfill(db, user_id=7, plan=plan_2,
                                   raw_drawings_by_id={1: raw_drawing_row})

    assert written_run_2 == 1
    assert len(db.trades) == 1, 'run 2 must NOT have created a second trade'
    stamped_metadata = db.drawing_updates[-1]['metadata']
    assert stamped_metadata['extend_data']['_journalDrawingId'] == 1

    # --- Run 3: reload the drawing as _load_user_drawings would after run
    # 2's write-back actually landed — this must be a genuine no-op. -----
    reloaded_drawing = dict(drawing)
    reloaded_drawing['extendData'] = stamped_metadata['extend_data']
    plan_3 = plan_backfill([reloaded_drawing], existing_after_run_1)

    assert plan_3 == []


class _FakeDrawingListDb:
    """Mirrors drawing_list's documented behavior: passing user_id returns
    that user's rows PLUS system rows at user_id=0."""

    def __init__(self, rows):
        self._rows = rows

    def drawing_list(self, user_id=None):
        return [r for r in self._rows if r['user_id'] in (user_id, 0)]


def _row(did, user_id, symbol='BTC', qty=0.5, price=50000.0):
    return {
        'id': did, 'user_id': user_id, 'asset': symbol, 'version': 1,
        'metadata': {
            'kline_name': 'spot',
            'points': [{'value': price}],
            'extend_data': {'quantity': qty, 'usdAmount': qty * price, 'sold': False},
        },
    }


def test_load_user_drawings_excludes_a_system_row_from_a_specific_users_backfill():
    db = _FakeDrawingListDb([
        _row(1, user_id=0),   # legacy system drawing, not this user's
        _row(2, user_id=7),   # actually belongs to the requested user
    ])

    normalized, raw_by_id = _load_user_drawings(db, user_id=7)

    assert [d['id'] for d in normalized] == [2]
    assert set(raw_by_id.keys()) == {2}


def test_a_system_drawing_at_user_id_zero_is_never_planned_for_a_real_user():
    # drawing_list(user_id=7) legitimately also returns user_id=0 system
    # rows per its own docstring. Without filtering, plan_backfill would
    # happily plan a trade for someone else's (nobody's) holding, and
    # /portfolio would attribute it to user 7. End-to-end through the real
    # loader, not just the pure planner, since the filtering happens in the loader.
    db = _FakeDrawingListDb([_row(1, user_id=0)])

    normalized, _raw_by_id = _load_user_drawings(db, user_id=7)

    assert plan_backfill(normalized, []) == []
