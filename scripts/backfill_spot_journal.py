#!/usr/bin/env python3
"""Backfill: spot-buy drawings placed before auto-promotion -> open journal trades.

Portfolio tracking made new spot buys promote themselves into
``trade_journal`` at the moment they're placed. This script is the one-time
sweep for the spot drawings that already existed on the chart before that
landed — without this, they stay invisible to ``/portfolio``.

Every drawing lands in exactly one of three states, checked in this order:

1. SKIPPED, fully promoted — ``extendData._journalDrawingId`` is already
   set. This is the PRIMARY gate, checked before anything else. The
   live path or a fully-successful prior run of this script stamped it;
   re-promoting it would double-count the holding.
2. ``action: 'stamp_only'`` — an ORPHAN. The drawing reached this point
   NOT stamped (the primary gate would have caught a fully-done one), but
   its own id already appears as a ``drawing_id`` on an existing journal
   trade. That combination only happens when a prior run's trade creation
   succeeded but its write-back did not (e.g. a version conflict — see
   ``_write_back_journal_id``). The trade already exists and is correct;
   only the drawing-side link is missing. Repair replays ONLY the
   write-back — it must never create a second trade, or the book
   double-counts the exact same holding.
3. ``action: 'create_then_stamp'`` — genuinely new. No existing trade, not
   yet promoted. The normal case for a legacy drawing.

These two actions are modeled explicitly (an ``action`` key on every plan
entry) rather than left implicit, precisely so "already fully done",
"needs its trade created", and "needs only its link repaired" can never be
silently conflated into one "skip" bucket — that conflation was a real
bug: an earlier version of this script treated the orphan case as
"already handled" and skipped it forever, permanently un-repairable,
because the ONLY thing the sell/delete paths look up is
``extendData._journalDrawingId`` — a trade with no stamped drawing cannot
be found by them and stays open forever.

Correlation-id scheme: the minted ``drawing_id`` sent to ``log_trade_open``
is the drawing's own database row id — NOT ``Date.now()``. It is already
globally unique (an auto-increment primary key), so it is collision-free
without needing to serialize the loop, and it is deterministic: re-running
the backfill over the same drawing recomputes the same id, which is what
lets state 2 above actually recognize its own prior (partial) output. It
also cannot collide with a Task-10-minted id — those are ``Date.now()``
millisecond timestamps (13 digits, currently >1.7e12), far outside the
range of any real drawing row id.

After creating the trade (or, for a repair, immediately), the script
writes the correlation id back into the drawing's own
``extendData._journalDrawingId`` (a full-column overwrite —
``drawing_update``'s ``metadata`` field replaces the JSON blob rather than
merging it, so the existing ``kline_name``/``points``/``local_id`` are
preserved by copying them forward, not just the ``extend_data`` sub-key).
The write-back is optimistic-concurrency guarded (``expected_version``);
on a lost race it raises loudly rather than swallowing the failure — and
because state 2 above exists, that failure is genuinely recoverable: a
later re-run of this script re-reads the drawing, sees the trade already
exists but the stamp doesn't, and plans a ``stamp_only`` repair instead of
silently skipping it.

Usage:
    python -m scripts.backfill_spot_journal --user-id 1 --dry-run
    python -m scripts.backfill_spot_journal --user-id 1
"""

from __future__ import annotations

import argparse
import logging

logger = logging.getLogger('carrac.backfill.spot')

SPOT_ACCOUNT_TAG = 'spot'
SPOT_TIMEFRAME = '1D'
SPOT_DIRECTION = 'long'

ACTION_CREATE_THEN_STAMP = 'create_then_stamp'
ACTION_STAMP_ONLY = 'stamp_only'


def plan_backfill(spot_drawings: list, existing_trades: list) -> list:
    """Which drawings still need journal work, and what kind. Pure — no I/O.

    ``spot_drawings`` items carry ``id``, ``symbol``, ``name``, ``points``
    and ``extendData`` (see ``_load_user_drawings`` for how DB rows are
    normalized into this shape). ``existing_trades`` items carry
    ``drawing_id`` (open + historical ``trade_journal`` rows).

    Returns a list of plan entries, each tagged with an explicit
    ``action`` — ``'create_then_stamp'`` or ``'stamp_only'`` — see the
    module docstring for what distinguishes the two. A drawing that is
    already fully promoted (primary gate) or not eligible at all
    (wrong ``name``, sold, no usable quantity/price) contributes nothing
    to the plan.
    """
    # Compared as strings — prod Postgres declares trade_journal.drawing_id
    # as `text` (tests/schema/carrac_pg_schema.sql), so psycopg2 hands
    # back a str here, while `drawing.get('id')` below (a drawing-table PK)
    # is always a genuine int. A plain int-keyed set would never match the
    # str form on real Postgres, silently reclassifying every orphan as
    # create_then_stamp and double-creating its trade. SQLite's INTEGER
    # affinity hides this — see the string-drawing-id test in
    # tests/test_backfill_spot_journal.py.
    already_linked = {
        str(t.get('drawing_id')) for t in existing_trades
        if t.get('drawing_id') is not None
    }

    plan = []
    for drawing in spot_drawings:
        if drawing.get('name') != 'spot':
            continue

        extend = drawing.get('extendData') or {}

        # Primary gate — checked before anything else. Already stamped =>
        # fully promoted; re-promoting it would double-count the holding.
        if extend.get('_journalDrawingId') is not None:
            continue

        drawing_id = drawing.get('id')

        # Reaching here means NOT stamped. If a trade for this exact id
        # already exists anyway, a prior run's trade-creation succeeded
        # but its write-back didn't — an orphan needing repair, not a
        # signal that nothing needs to happen. Checked BEFORE the
        # sold/quantity/price gates below: those gates decide whether
        # it's safe to OPEN a new position, which doesn't apply here —
        # the position already exists; only its journal link is missing.
        if str(drawing_id) in already_linked:
            plan.append({
                'action': ACTION_STAMP_ONLY,
                'drawing_id': drawing_id,
                'asset': drawing.get('symbol'),
            })
            continue

        if extend.get('sold'):
            continue

        quantity = extend.get('quantity')
        if not quantity or quantity <= 0:
            continue

        points = drawing.get('points') or []
        if not points:
            continue
        entry_price = points[0].get('value')
        if not entry_price or entry_price <= 0:
            continue

        plan.append({
            'action': ACTION_CREATE_THEN_STAMP,
            'drawing_id': drawing_id,
            'asset': drawing.get('symbol'),
            'entry_price': entry_price,
            'size': quantity,
        })
    return plan


def _load_user_drawings(db, user_id: int):
    """Fetch the user's drawings, normalized for ``plan_backfill``.

    ``ContentDatabase`` has no ``get_drawings`` — the real accessor is
    ``drawing_list(user_id=...)``, which returns raw rows: top-level
    ``asset``/``id``/``metadata`` columns, with ``name``/``points`` and the
    klinecharts ``extendData`` nested inside the parsed ``metadata`` JSON
    blob (stored there as ``kline_name``/``points``/``extend_data`` by
    ``DrawingTools.syncDrawingToServer`` — see ``static/js/drawing-tools.js``).

    Returns ``(normalized, raw_by_id)``: ``normalized`` is the
    ``{id, symbol, name, points, extendData}`` shape ``plan_backfill``
    consumes; ``raw_by_id`` keeps the untouched DB row (and its full
    ``metadata`` dict) so the write-back can replace that column without
    dropping sibling keys (``local_id``, ``z_order``, ...) it doesn't own.
    """
    rows = db.drawing_list(user_id=user_id)
    normalized = []
    raw_by_id = {}
    for row in rows:
        # drawing_list's own docstring: passing user_id returns that user's
        # rows PLUS system drawings at user_id=0 (legacy pre-multi-user
        # rows — content_db.py::_init_database). Promoting one of those into THIS
        # user's journal would misattribute a holding that was never
        # theirs — worse than a double-count, since the number belongs to
        # someone else. Filter it back out to an exact user match.
        if row.get('user_id') != user_id:
            continue
        meta = row.get('metadata') or {}
        normalized.append({
            'id': row.get('id'),
            'symbol': row.get('asset'),
            'name': meta.get('kline_name'),
            'points': meta.get('points') or [],
            'extendData': meta.get('extend_data') or {},
        })
        raw_by_id[row.get('id')] = row
    return normalized, raw_by_id


def _write_back_journal_id(db, user_id: int, raw_drawing: dict, drawing_id: int) -> None:
    """Stamp ``extendData._journalDrawingId`` on the drawing's stored metadata.

    ``drawing_update(metadata=...)`` overwrites the whole JSON column, so we
    copy the existing metadata forward and only touch the nested
    ``extend_data`` key — losing ``points``/``kline_name``/``local_id`` here
    would corrupt the drawing on the chart, not just the journal link.

    Passes ``expected_version`` (the version this script's own snapshot of
    the row was read at) so the update is optimistic-concurrency guarded:
    if another writer (a different device syncing, a live chart session)
    touched this drawing between our read and this write, a blind
    whole-column overwrite would silently discard THEIR change (a moved
    point, a renamed label, a colour) even though our own field always
    wins the merge. ``drawing_update`` refuses instead — see
    ``content_db.py::get_drawing_by_id`` and ``drawing_update``.

    Raises on failure rather than swallowing it. This is genuinely
    recoverable, not just loud: if the trade for this drawing was already
    created (the ``ACTION_CREATE_THEN_STAMP`` case), a re-run of the whole
    script re-reads the drawing, finds the trade already exists but the
    stamp doesn't, and ``plan_backfill`` classifies it as
    ``ACTION_STAMP_ONLY`` — repairing just the link, without creating a
    second trade. That is what makes the error message below true.
    """
    raw_drawing = raw_drawing or {}
    metadata = dict(raw_drawing.get('metadata') or {})
    extend = dict(metadata.get('extend_data') or {})
    extend['_journalDrawingId'] = drawing_id
    metadata['extend_data'] = extend
    expected_version = raw_drawing.get('version')

    status, current_version = db.drawing_update(
        drawing_id, user_id=user_id, metadata=metadata,
        expected_version=expected_version)
    if status == db.UPDATE_CONFLICT:
        raise RuntimeError(
            f'write-back of _journalDrawingId for drawing {drawing_id} lost a '
            f'concurrency race: expected version {expected_version}, found '
            f'{current_version} — the drawing changed elsewhere since this '
            f'script read it. Its trade (if this was a create) already '
            f'exists and is correct — re-run the backfill; it will be found '
            f'as an orphan and repaired with a stamp-only write-back, not '
            f'recreated.'
        )
    if status != db.UPDATE_OK:
        raise RuntimeError(
            f'write-back of _journalDrawingId failed for drawing {drawing_id}: '
            f'drawing_update status={status!r}'
        )


def apply_backfill(db, user_id: int, plan: list, raw_drawings_by_id: dict,
                    dry_run: bool = False) -> int:
    """Execute the plan: create trades where needed, repair orphaned links,
    write the correlation id back in both cases. Returns count written.

    Branches on each item's ``action``:
    - ``ACTION_STAMP_ONLY``: the trade already exists (see
      ``plan_backfill``) — replays ONLY the write-back. Never calls
      ``log_trade_open`` for these; doing so would create a second trade
      for a holding that's already recorded, double-counting the book.
    - ``ACTION_CREATE_THEN_STAMP``: the normal new-drawing path — create
      the trade, set size/account_tag, then write back.

    On any failure mid-loop, logs how many items were successfully
    completed *before* re-raising — the operator most needs that tally
    exactly when the run breaks, not only on a clean finish.
    """
    written = 0
    try:
        for item in plan:
            drawing_id = item['drawing_id']
            action = item['action']

            if dry_run:
                logger.info('DRY RUN would %s: %s', action, item)
                continue

            if action == ACTION_STAMP_ONLY:
                # The trade already exists — repair the link only. Must
                # NOT call log_trade_open here, or the holding double-counts.
                _write_back_journal_id(
                    db, user_id, raw_drawings_by_id.get(drawing_id), drawing_id)
                written += 1
                logger.info(
                    'repaired orphaned promotion for drawing %s '
                    '(its trade already existed)', drawing_id)
                continue

            trade_id = db.log_trade_open(
                user_id=user_id, asset=item['asset'], timeframe=SPOT_TIMEFRAME,
                direction=SPOT_DIRECTION, entry_price=item['entry_price'],
                drawing_id=drawing_id)
            # log_trade_open accepts neither size nor account_tag.
            updated = db.trade_update(trade_id, size=item['size'],
                                       account_tag=SPOT_ACCOUNT_TAG)
            if not updated:
                # Loud on purpose: silently proceeding to the write-back
                # would stamp this drawing as fully promoted while the
                # trade it points at has no size — permanently excluded
                # from /portfolio valuation, and (without the orphan-repair
                # path above) permanently un-repairable by a re-run.
                raise RuntimeError(
                    f'trade_update(size, account_tag) returned False for '
                    f'trade {trade_id} (drawing {drawing_id}) — trade was '
                    f'created but is missing size/account_tag; refusing to '
                    f'mark the drawing as promoted'
                )

            _write_back_journal_id(db, user_id, raw_drawings_by_id.get(drawing_id), drawing_id)

            written += 1
            logger.info('promoted drawing %s -> trade %s (%s %s @ %s)',
                        drawing_id, trade_id, item['size'], item['asset'], item['entry_price'])
    except Exception:
        logger.error('backfill aborted after writing %d item(s)', written)
        raise
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id', type=int, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

    from carrac.content_db import get_content_db
    db = get_content_db()

    drawings, raw_by_id = _load_user_drawings(db, args.user_id)
    existing = (db.get_open_trades(args.user_id)
                + db.get_trade_history(args.user_id, days=3650, limit=10000))

    plan = plan_backfill(drawings, existing)
    logger.info('%d item(s) to promote/repair', len(plan))
    written = apply_backfill(db, args.user_id, plan, raw_by_id, dry_run=args.dry_run)
    if not args.dry_run:
        logger.info('%d item(s) written', written)


if __name__ == '__main__':
    main()
