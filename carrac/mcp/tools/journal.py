"""MCP tools: Positions & Journal (8 tools)."""

import json

from carrac.journal.tags import with_tag_lists
from carrac.site_url import site_base_url
from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools import get_mcp_user_id


@mcp_tool(
    'trade_get_positions',
    'Returns open trade journal entries enriched with live data. Response has a '
    '`positions` array; each row includes: id, asset, direction, entry (entry price), '
    'price (live price), pnl_pct (unrealised P&L %), rr (planned reward:risk), risk, '
    'scanner (0–100 composite signal score for the asset right now), heat, and '
    'mfe_r/mae_r (max favorable/adverse excursion in R, with mfe_computed_at). '
    'Call this to see current exposure before deciding to log or close a trade. '
    'Live data — re-call to refresh. Optional asset filter scopes to one symbol. '
    'Workflow: trade_get_positions → trade_close / trade_update.',
    {
        'type': 'object',
        'properties': {
            'asset': {
                'type': 'string',
                'description': 'Filter by asset symbol (optional)',
            },
        },
    },
    min_tier='free',
)
def trade_get_positions(arguments):
    from carrac.composite.assemblers import assemble_live_positions
    from carrac.composite.formatters import compact_live_positions

    asset_filter = arguments.get('asset', '').upper().strip() or None
    result = assemble_live_positions(user_id=get_mcp_user_id(), asset_filter=asset_filter)
    return compact_live_positions(result)


_MAX_TAGS = 16
_MAX_TAG_LEN = 50

# Every journal row carries exactly one ``src:`` origin tag so trades stay
# attributable to the decision-maker that produced them.  The read side
# (head-to-head performance by origin) is not built yet.  ``src:agent`` marks an
# external automated journal source, which emits that tag itself; this is the
# Carrac-side enforcement so nothing lands untagged.  ``src:copy`` marks a
# trade mirrored from an external pick list.
_SRC_PREFIX = 'src:'
_SRC_VOCABULARY = frozenset({'agent', 'manual', 'scanner', 'copy'})
# Origin names used by earlier releases.  Rows already stored with them keep
# working: they are mapped onto the current names whenever a tag list is
# validated, so a client that echoes an old value back is not rejected.
_LEGACY_SRC = {'keel': 'agent', 'coinvest': 'copy'}
_DEFAULT_SRC = 'src:manual'


def _src_tags(tags) -> list:
    """Return the ``src:`` entries present in a sanitised tag list."""
    return [t for t in tags if t.startswith(_SRC_PREFIX)]


def _canonical_src(tag):
    """Map a legacy ``src:`` tag onto its current name (others unchanged)."""
    if not isinstance(tag, str) or not tag.startswith(_SRC_PREFIX):
        return tag
    value = tag[len(_SRC_PREFIX):]
    return _SRC_PREFIX + _LEGACY_SRC.get(value, value)


def _validate_tags(raw, *, inherit_src=None) -> str:
    """Validate and serialise the ``tags`` argument.

    Accepts a list of strings.  Rejects non-list values, lists with more than
    ``_MAX_TAGS`` items, and individual tags longer than ``_MAX_TAG_LEN`` chars.
    Returns a JSON-serialised string suitable for the ``tags`` TEXT column.

    Exactly one ``src:`` origin tag is guaranteed on the way out:
    an unknown ``src:`` value or more than one is rejected, and a list carrying
    none inherits ``inherit_src`` if given, else defaults to ``src:manual``.
    """
    if not isinstance(raw, list):
        raise ToolError('tags must be a list of strings')
    if len(raw) > _MAX_TAGS:
        raise ToolError(f'tags may contain at most {_MAX_TAGS} items (got {len(raw)})')
    sanitised = []
    for item in raw:
        if not isinstance(item, str):
            raise ToolError('each tag must be a string')
        item = item.strip()
        if len(item) > _MAX_TAG_LEN:
            raise ToolError(
                f'each tag must be at most {_MAX_TAG_LEN} characters '
                f'(got {len(item)} chars: {item!r})'
            )
        sanitised.append(_canonical_src(item))

    inherit_src = _canonical_src(inherit_src)
    srcs = _src_tags(sanitised)
    if len(srcs) > 1:
        raise ToolError(
            f'at most one {_SRC_PREFIX} origin tag is allowed (got {srcs})'
        )
    for tag in srcs:
        if tag[len(_SRC_PREFIX):] not in _SRC_VOCABULARY:
            allowed = ', '.join(sorted(_SRC_PREFIX + v for v in _SRC_VOCABULARY))
            raise ToolError(
                f'unknown origin tag {tag!r} — {_SRC_PREFIX} must be one of: {allowed}'
            )
    if not srcs:
        # The _MAX_TAGS budget belongs to the caller; the origin tag is
        # reserved system metadata appended on top, so supplying a full 16
        # tags stays valid rather than overflowing.
        sanitised.append(inherit_src or _DEFAULT_SRC)
    return json.dumps(sanitised)


@mcp_tool(
    'trade_log',
    'Create a new open trade journal entry. '
    'SIDE EFFECTS: writes exactly one row to the journal DB; no push notification, '
    'no SSE event is fired. Returns {trade_id, status, asset}. '
    'PREREQUISITES: asset and entry_price are required; direction defaults to "long", '
    'timeframe defaults to "1D". '
    'TAGS: use the tags field as the primary traceability hook for automation — '
    'e.g. tags=["src:agent","strat:bb_breakdown"] lets you later filter by origin '
    'strategy or system in trade_list / trade_get_analytics. '
    'ORIGIN: every entry carries exactly one src: tag naming the decision-maker. '
    'Allowed values are src:agent, src:manual, src:scanner, src:copy — any '
    'other src: value is rejected. Omit it and the entry defaults to src:manual, '
    'so pass the right one explicitly when logging on behalf of a system. '
    'Max 16 tags, each at most 50 chars. '
    'Use trade_close to record the exit and compute P&L; '
    'use trade_update to amend stop/target/notes on an open position.',
    {
        'type': 'object',
        'properties': {
            'asset': {
                'type': 'string',
                'description': 'Asset symbol (e.g. BTC, ETH)',
            },
            'entry_price': {
                'type': 'number',
                'description': 'Entry price',
            },
            'direction': {
                'type': 'string',
                'enum': ['long', 'short'],
                'description': 'Trade direction (default: long)',
            },
            'timeframe': {
                'type': 'string',
                'description': 'Timeframe (default: 1D)',
            },
            'target_price': {
                'type': 'number',
                'description': 'Target / take-profit price (optional)',
            },
            'stop_price': {
                'type': 'number',
                'description': 'Stop-loss price (optional)',
            },
            'setup_type': {
                'type': 'string',
                'description': 'Setup classification (e.g. "breakout", "pullback")',
            },
            'size': {
                'type': 'number',
                'description': 'Position size (optional)',
            },
            'account_tag': {
                'type': 'string',
                'description': 'Account / sub-account label (optional, max 30 chars)',
            },
            'confidence': {
                'type': 'integer',
                'description': 'Confidence level 1-5 (optional)',
            },
            'tags': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': (
                    'Origin / classification tags (optional). '
                    'Max 16 items, each at most 50 chars. '
                    'Use namespaced tags for traceability, '
                    'e.g. ["src:agent", "strat:bb_breakdown"]. '
                    'Exactly one src: origin tag is stored per entry — one of '
                    'src:agent, src:manual, src:scanner, src:copy. '
                    'Defaults to src:manual when omitted.'
                ),
            },
        },
        'required': ['asset', 'entry_price'],
    },
    min_tier='free',
)
def trade_log(arguments):
    from carrac.content_db import get_content_db
    from carrac.auth.decorators import get_tier_limits_for_user
    from carrac.mcp.runtime import get_mcp_caller_tier

    asset = arguments.get('asset', '').upper().strip()
    entry_price = arguments.get('entry_price')
    if not asset:
        raise ToolError('asset is required')
    if not entry_price or entry_price <= 0:
        raise ToolError('entry_price must be a positive number')

    # Always run through _validate_tags, even when the caller
    # supplied nothing, so every new row lands with a src: origin tag.
    tags_json = _validate_tags(arguments.get('tags') or [])

    # Extended fields not covered by log_trade_open's core signature — same
    # pattern as the Flask/FastAPI journal write paths (carrac/journal/routes.py,
    # carrac/fastapi_app/routers/journal.py): validated BEFORE the commit below.
    # A bad value here must never leave an orphaned row — raising post-commit
    # would let the caller see a -32603/no-trade_id failure while the row was
    # already written, and a retry would then create a duplicate.
    extended = {}
    if arguments.get('size') is not None:
        from carrac.validation import parse_float
        size_val = parse_float(arguments, 'size')
        if size_val is None:
            raise ToolError('size must be a valid number')
        if size_val > 0:
            extended['size'] = size_val
    account_tag = arguments.get('account_tag')
    if account_tag:
        extended['account_tag'] = str(account_tag).strip()[:30]

    db = get_content_db()
    if not db:
        raise ToolError('Journal database not available')

    user_id = get_mcp_user_id()

    # Tier gate — journal_trades_monthly cap.
    tier = get_mcp_caller_tier()
    limits = get_tier_limits_for_user(user_id, tier)
    monthly_cap = limits.get('journal_trades_monthly', 0)
    trades_this_month = db.count_trades_this_month(user_id)
    if trades_this_month >= monthly_cap:
        raise ToolError(
            f'Monthly journal limit reached ({trades_this_month}/{monthly_cap} '
            'trades logged this month). Upgrade your plan for unlimited journal trades.'
        )

    trade_id = db.log_trade_open(
        user_id=user_id,
        asset=asset,
        timeframe=arguments.get('timeframe', '1D'),
        direction=arguments.get('direction', 'long'),
        entry_price=float(entry_price),
        target_price=float(arguments['target_price']) if arguments.get('target_price') else None,
        stop_price=float(arguments['stop_price']) if arguments.get('stop_price') else None,
        setup_type=arguments.get('setup_type'),
        tags=tags_json,
    )

    if extended:
        db.trade_update(trade_id, **extended)

    return {'trade_id': trade_id, 'status': 'created', 'asset': asset}


@mcp_tool(
    'trade_get_analytics',
    'Returns aggregate performance metrics for closed journal trades over a '
    'configurable lookback window (default 90 days). All R-based (reward:risk), '
    'not currency. Key fields: trades, win_rate, avg_rr, avg_win_rr, avg_loss_rr, '
    'expectancy (R per trade), profit_factor, max_drawdown (in R), avg_hold_hours, '
    'longest_win_streak / longest_loss_streak / current_streak, best_rr / worst_rr, '
    'plus by_setup and by_asset breakdowns (each with count, win_rate, avg_rr, expectancy). '
    'Only closed trades with a recorded R outcome are counted; open positions do not affect metrics. '
    'Requires member tier. '
    'ADDITIVE: a `playbook` field carries a deeper per-group breakdown — '
    'by_setup, by_tag (archetype tags), by_symbol, by_side, by_hour_of_day, by_day_of_week — '
    'each group has n, win_rate, expectancy, profit_factor, avg_win/avg_loss, avg_mfe/avg_mae, '
    'an r_distribution (7 buckets: <=-2R, -2..-1, -1..0, 0..1, 1..2, 2..3, >3R), a `basis` '
    '("r" or "pct" — a group falls back to percent-return and is labelled pct-based the moment '
    'ANY trade in it has no recorded stop, so R and percent numbers are never mixed), and '
    'small_sample (n<20). `playbook` is itself paid-tier gated; when locked it is '
    '{"locked": true, "reason": ...} instead — check `playbook.locked` before reading the rest. '
    'Purely descriptive numbers — not trading advice. '
    'Call trade_list first to inspect individual entries, then this tool for the '
    'aggregate picture. Use the days parameter to widen/narrow the window.',
    {
        'type': 'object',
        'properties': {
            'days': {
                'type': 'integer',
                'description': 'Lookback period in days (default: 90)',
            },
        },
    },
    min_tier='member',
)
def trade_get_analytics(arguments):
    from carrac.content_db import get_content_db
    from carrac.mcp.runtime import get_mcp_caller_tier
    from carrac.journal.playbook_analytics import get_playbook_analytics

    db = get_content_db()
    if not db:
        raise ToolError('Journal database not available')

    user_id = get_mcp_user_id()
    days = arguments.get('days', 90)
    analytics = db.get_journal_analytics(user_id, days=days)

    # Additive per-setup/tag/symbol/side/time-of-day breakdown.
    # New `playbook` key only — every existing field above is unchanged, so
    # this stays backward-compatible for existing callers.
    from carrac.auth.decorators import get_tier_limits_for_user
    limits = get_tier_limits_for_user(user_id, get_mcp_caller_tier())
    if limits.get('journal_analytics', False):
        playbook = get_playbook_analytics(db, user_id, days=days)
        playbook['locked'] = False
        analytics['playbook'] = playbook
    else:
        analytics['playbook'] = {
            'locked': True,
            'reason': ('Playbook analytics (per-setup expectancy, R-multiple '
                       'distribution, and average MAE/MFE) requires a paid plan.'),
        }
    return analytics


@mcp_tool(
    'trade_delete',
    'Delete one or more trade journal entries (and their screenshots). '
    'Pass `trade_id` for a single delete or `trade_ids` for bulk. '
    'Use `dry_run` to preview without deleting. '
    'Hard delete — no undo (relies on the daily DB backup for recovery).',
    {
        'type': 'object',
        'properties': {
            'trade_id': {
                'type': 'integer',
                'description': 'Single trade ID to delete',
            },
            'trade_ids': {
                'type': 'array',
                'items': {'type': 'integer'},
                'description': 'List of trade IDs to delete (max 50)',
            },
            'dry_run': {
                'type': 'boolean',
                'description': 'If true, return previews without deleting (default false)',
            },
        },
    },
    min_tier='free',  # owns own data — same as in-app delete button
)
def trade_delete(arguments):
    from carrac.content_db import get_content_db

    BULK_CAP = 50

    raw_id = arguments.get('trade_id')
    raw_ids = arguments.get('trade_ids') or []
    dry_run = bool(arguments.get('dry_run', False))

    ids = []
    if raw_id is not None:
        ids.append(int(raw_id))
    if raw_ids:
        if not isinstance(raw_ids, list):
            raise ToolError('trade_ids must be an array of integers')
        try:
            ids.extend(int(i) for i in raw_ids)
        except (TypeError, ValueError):
            raise ToolError('trade_ids must contain integers')
    # De-dupe while preserving order
    seen = set()
    ids = [i for i in ids if not (i in seen or seen.add(i))]

    if not ids:
        raise ToolError('Provide trade_id or trade_ids')
    if len(ids) > BULK_CAP:
        raise ToolError(f'Too many ids ({len(ids)}); max {BULK_CAP} per call')

    db = get_content_db()
    if not db:
        raise ToolError('Journal database not available')

    user_id = get_mcp_user_id()

    # Build previews from the user's own rows (skips foreign IDs silently)
    previews = []
    skipped = []
    for tid in ids:
        entry = db.get_journal_entry(tid)
        if not entry or entry.get('user_id') != user_id:
            skipped.append(tid)
            continue
        previews.append({
            'trade_id': tid,
            'asset': entry.get('asset'),
            'direction': entry.get('direction'),
            'entry_price': entry.get('entry_price'),
            'opened_at': entry.get('opened_at'),
            'closed_at': entry.get('closed_at'),
        })

    if dry_run:
        return {
            'dry_run': True,
            'would_delete': previews,
            'skipped': skipped,
            'count': len(previews),
        }

    deleted = db.delete_trades_bulk([p['trade_id'] for p in previews], user_id)
    return {
        'dry_run': False,
        'deleted_count': deleted,
        'deleted': previews,
        'skipped': skipped,
    }


@mcp_tool(
    'trade_close',
    'Close one or more open trade journal entries by recording an exit price. '
    'Pass `trade_id` (single) or `trade_ids` (bulk, max 50) with a uniform `exit_price`, '
    'or pass `exits` for per-trade exit prices/reasons. '
    'Use `dry_run` to preview without writing. '
    'Soft close — fills exit_price, exit_reason, closed_at, and recomputes actual R:R. '
    'Already-closed trades are returned in the `already_closed` bucket rather than erroring.',
    {
        'type': 'object',
        'properties': {
            'trade_id': {
                'type': 'integer',
                'description': 'Single trade ID to close',
            },
            'trade_ids': {
                'type': 'array',
                'items': {'type': 'integer'},
                'description': 'List of trade IDs to close (max 50)',
            },
            'exit_price': {
                'type': 'number',
                'description': 'Uniform exit price applied to every ID (must be > 0)',
            },
            'exits': {
                'type': 'array',
                'description': (
                    'Per-trade exit specs. Each item: {trade_id, exit_price, '
                    'exit_reason?, notes?}. Overrides exit_price if both given.'
                ),
                'items': {
                    'type': 'object',
                    'properties': {
                        'trade_id': {'type': 'integer'},
                        'exit_price': {'type': 'number'},
                        'exit_reason': {'type': 'string'},
                        'notes': {'type': 'string'},
                    },
                    'required': ['trade_id', 'exit_price'],
                },
            },
            'exit_reason': {
                'type': 'string',
                'enum': ['manual', 'tp_hit', 'sl_hit', 'trailing_stop', 'time_based', 'breakeven'],
                'description': (
                    'Default exit reason applied to all IDs when not overridden per-trade '
                    '(default: manual)'
                ),
            },
            'notes': {
                'type': 'string',
                'description': 'Optional notes appended to all trades (max 10 000 chars)',
            },
            'dry_run': {
                'type': 'boolean',
                'description': 'If true, return previews without writing (default false)',
            },
        },
    },
    min_tier='free',  # owns own data — same as in-app close button and trade_delete
)
def trade_close(arguments):
    import logging
    from carrac.content_db import get_content_db
    from carrac.journal.routes import _ALLOWED_EXIT_REASONS

    logger = logging.getLogger(__name__)
    BULK_CAP = 50
    MAX_NOTES = 10_000

    # ── Parse the two input forms ──────────────────────────────────────────────
    raw_id = arguments.get('trade_id')
    raw_ids = arguments.get('trade_ids') or []
    raw_exits = arguments.get('exits')  # per-id list overrides uniform form

    uniform_exit_price = arguments.get('exit_price')
    default_exit_reason = (arguments.get('exit_reason') or 'manual').strip()
    if default_exit_reason not in _ALLOWED_EXIT_REASONS:
        raise ToolError(
            f'exit_reason must be one of: {", ".join(sorted(_ALLOWED_EXIT_REASONS))}'
        )
    global_notes = (arguments.get('notes') or '')[:MAX_NOTES] or None
    dry_run = bool(arguments.get('dry_run', False))

    # Build a unified per-id spec list: [{trade_id, exit_price, exit_reason, notes}]
    specs = []

    if raw_exits:
        # Per-id form — validate each item
        if not isinstance(raw_exits, list):
            raise ToolError('exits must be an array')
        for item in raw_exits:
            if not isinstance(item, dict):
                raise ToolError('each exits item must be an object')
            tid = item.get('trade_id')
            eprice = item.get('exit_price')
            if tid is None or eprice is None:
                raise ToolError('each exits item requires trade_id and exit_price')
            try:
                tid = int(tid)
                eprice = float(eprice)
            except (TypeError, ValueError):
                raise ToolError('exits items: trade_id must be integer, exit_price must be number')
            if eprice <= 0:
                raise ToolError(f'exit_price must be > 0 (got {eprice} for trade_id {tid})')
            reason = (item.get('exit_reason') or default_exit_reason).strip()
            if reason not in _ALLOWED_EXIT_REASONS:
                reason = default_exit_reason  # fall back rather than hard-error
            notes = (item.get('notes') or global_notes or '')[:MAX_NOTES] or None
            specs.append({'trade_id': tid, 'exit_price': eprice,
                          'exit_reason': reason, 'notes': notes})
    else:
        # Uniform form — trade_id / trade_ids + exit_price
        ids = []
        if raw_id is not None:
            ids.append(int(raw_id))
        if raw_ids:
            if not isinstance(raw_ids, list):
                raise ToolError('trade_ids must be an array of integers')
            try:
                ids.extend(int(i) for i in raw_ids)
            except (TypeError, ValueError):
                raise ToolError('trade_ids must contain integers')
        if not ids:
            raise ToolError(
                'Provide trade_id / trade_ids (with exit_price), or an exits array'
            )
        if uniform_exit_price is None:
            raise ToolError('exit_price is required when using trade_id / trade_ids')
        try:
            uniform_exit_price = float(uniform_exit_price)
        except (TypeError, ValueError):
            raise ToolError('exit_price must be a number')
        if uniform_exit_price <= 0:
            raise ToolError('exit_price must be > 0')
        for tid in ids:
            specs.append({'trade_id': tid, 'exit_price': uniform_exit_price,
                          'exit_reason': default_exit_reason, 'notes': global_notes})

    # De-dupe by trade_id preserving first occurrence
    seen = set()
    unique_specs = []
    for s in specs:
        if s['trade_id'] not in seen:
            seen.add(s['trade_id'])
            unique_specs.append(s)
    specs = unique_specs

    if len(specs) > BULK_CAP:
        raise ToolError(f'Too many ids ({len(specs)}); max {BULK_CAP} per call')

    db = get_content_db()
    if not db:
        raise ToolError('Journal database not available')

    user_id = get_mcp_user_id()

    # ── Ownership + already-closed check ──────────────────────────────────────
    to_close = []   # [{spec, entry}]
    skipped = []
    already_closed = []

    for spec in specs:
        tid = spec['trade_id']
        entry = db.get_journal_entry(tid)
        if not entry or entry.get('user_id') != user_id:
            skipped.append(tid)
            continue
        if entry.get('closed_at'):
            already_closed.append(tid)
            continue
        to_close.append({'spec': spec, 'entry': entry})

    # ── Dry run ────────────────────────────────────────────────────────────────
    if dry_run:
        previews = []
        for item in to_close:
            e = item['entry']
            s = item['spec']
            previews.append({
                'trade_id': e['id'],
                'asset': e.get('asset'),
                'direction': e.get('direction'),
                'entry_price': e.get('entry_price'),
                'exit_price': s['exit_price'],
                'exit_reason': s['exit_reason'],
            })
        return {
            'dry_run': True,
            'would_close': previews,
            'count': len(previews),
            'skipped': skipped,
            'already_closed': already_closed,
        }

    # ── Real operation ─────────────────────────────────────────────────────────
    closed_rows = []
    for item in to_close:
        spec = item['spec']
        tid = spec['trade_id']

        success = db.log_trade_close(
            journal_id=tid,
            exit_price=spec['exit_price'],
            exit_reason=spec['exit_reason'],
            notes=spec['notes'],
        )
        if not success:
            # log and continue — partial success is fine for bulk ops
            logger.warning(f'trade_close MCP: log_trade_close returned False for trade {tid}')
            skipped.append(tid)
            continue

        # Re-fetch to get computed fields (rr_actual, closed_at)
        updated = db.get_journal_entry(tid)

        # Best-effort side effects — never fail the tool on these
        try:
            from carrac.journal.mfe_mae import calculate_and_store_mfe_mae
            calculate_and_store_mfe_mae(db, tid)
        except Exception as exc:
            # No retry exists, so this is a permanent hole in the
            # trade's analytics — ERROR (the error tracker), same as the two HTTP close paths.
            logger.error(
                f'MFE/MAE calculation failed for trade {tid} — never recomputed '
                f'after close, analytics for this trade stay blank: {exc}',
                exc_info=True,
            )

        try:
            from carrac.scanner.snapshot import capture_score_snapshot
            capture_score_snapshot(
                db, user_id, tid,
                item['entry'].get('asset'),
                item['entry'].get('direction', 'long'),
                snapshot_type='exit',
            )
        except Exception as exc:
            logger.warning(f'Exit snapshot failed for trade {tid}: {exc}')

        # Integration (PostHog / EspoCRM) — requires the user's email, which is
        # not available in MCP context (no Flask session). We skip it rather than
        # synthesising a fake address. The in-app close route does fire this;
        # deviation is intentional and documented here.
        try:
            from carrac.integrations import on_trade_closed
            # Attempt to resolve email via the DB if a helper exists; otherwise skip.
            user_email = ''
            try:
                user_row = db.get_user_by_id(user_id) if hasattr(db, 'get_user_by_id') else None
                user_email = (user_row or {}).get('email', '')
            except Exception:
                pass
            if user_email:
                stats = db.get_journal_stats(user_id, days=365)
                on_trade_closed(user_id, user_email, updated, stats)
            else:
                logger.info(
                    f'trade_close MCP: skipping on_trade_closed integration for trade {tid} '
                    '— email not resolvable from MCP context'
                )
        except Exception as exc:
            logger.warning(f'Trade close integration failed for trade {tid}: {exc}')

        closed_rows.append({
            'trade_id': tid,
            'asset': updated.get('asset') if updated else item['entry'].get('asset'),
            'direction': updated.get('direction') if updated else item['entry'].get('direction'),
            'entry_price': updated.get('entry_price') if updated else item['entry'].get('entry_price'),
            'exit_price': spec['exit_price'],
            'exit_reason': spec['exit_reason'],
            'closed_at': (updated or {}).get('closed_at'),
            'rr_actual': (updated or {}).get('rr_actual'),
        })

    return {
        'dry_run': False,
        'closed_count': len(closed_rows),
        'closed': closed_rows,
        'skipped': skipped,
        'already_closed': already_closed,
    }


@mcp_tool(
    'trade_get_ai_review',
    'Generate an AI-written review of a single closed trade. '
    'Returns: entry/exit quality assessment, letter grade (A–F), '
    'R:R commentary, rule-adherence score, and concrete improvement suggestions. '
    'PREREQUISITES: trade must already be closed (use trade_close first); '
    'passing an open trade_id raises a ToolError. '
    'Requires member tier — token cost is incurred per call. '
    'Use trade_list to find the trade_id before calling this tool.',
    {
        'type': 'object',
        'properties': {
            'trade_id': {
                'type': 'integer',
                'description': 'Trade ID to review (must be closed)',
            },
        },
        'required': ['trade_id'],
    },
    min_tier='member',  # AI reviews cost tokens — gate to paid tier
)
def trade_get_ai_review(arguments):
    trade_id = arguments.get('trade_id')
    if not trade_id:
        raise ToolError('trade_id is required')

    try:
        from carrac.journal.ai_review import review_trade
        from carrac.mcp.runtime import get_mcp_caller_is_internal
        from carrac.content_db import get_content_db

        db = get_content_db()
        if not db:
            raise ToolError('Journal database not available')

        result = review_trade(
            db, trade_id, get_mcp_user_id(),
            is_internal=get_mcp_caller_is_internal(),
        )
        if 'error' in result:
            raise ToolError(result['error'])
        return result
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f'AI trade review failed: {e}')


def _as_naive_dt(value):
    """Coerce a DB timestamp (datetime on Postgres, str on SQLite) to a naive
    datetime; ``None`` when absent or unparseable."""
    from datetime import datetime
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(
            str(value).replace('Z', '+00:00').split('+')[0])
    except ValueError:
        return None


def _opened_within(opened_at, from_dt, to_dt):
    dt = _as_naive_dt(opened_at)
    if dt is None:
        return False
    if from_dt and dt < from_dt:
        return False
    if to_dt and dt > to_dt:
        return False
    return True


@mcp_tool(
    'trade_list',
    'List trade journal entries with optional filtering. '
    'Returns a trades array where each item includes: trade_id, asset, direction, '
    'timeframe, entry_price, exit_price (null if open), rr_actual, status, '
    'setup_type, tags, opened_at, closed_at, and notes. '
    'Filter by status (open / closed / all), asset symbol, direction, setup_type, '
    'and ISO date range (from_date / to_date). '
    'Default: all statuses, last 365 days, limit 50 (max 200). '
    'Data comes directly from the journal DB — freshness matches last log/close. '
    'Use this as the first step before trade_close, trade_update, trade_delete, '
    'or trade_get_ai_review to look up trade_id values.',
    {
        'type': 'object',
        'properties': {
            'asset': {
                'type': 'string',
                'description': 'Filter by asset symbol (e.g. BTC)',
            },
            'status': {
                'type': 'string',
                'enum': ['open', 'closed', 'all'],
                'description': 'Trade status filter (default: all)',
            },
            'direction': {
                'type': 'string',
                'enum': ['long', 'short'],
                'description': 'Filter by trade direction',
            },
            'setup_type': {
                'type': 'string',
                'description': 'Filter by setup classification (e.g. "breakout")',
            },
            'from_date': {
                'type': 'string',
                'description': 'Start date ISO 8601 (e.g. 2026-01-01)',
            },
            'to_date': {
                'type': 'string',
                'description': 'End date ISO 8601 (e.g. 2026-12-31)',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results to return (default 50, max 200)',
            },
        },
    },
    min_tier='free',
)
def trade_list(arguments):
    from carrac.content_db import get_content_db
    from datetime import datetime, timedelta

    user_id = get_mcp_user_id()
    db = get_content_db()
    if not db:
        raise ToolError('Journal database not available')

    status = (arguments.get('status') or 'all').lower()
    if status not in ('open', 'closed', 'all'):
        raise ToolError('status must be one of: open, closed, all')

    asset = (arguments.get('asset') or '').strip().upper() or None
    direction = (arguments.get('direction') or '').strip().lower() or None
    if direction and direction not in ('long', 'short'):
        raise ToolError('direction must be "long" or "short"')
    setup_type = (arguments.get('setup_type') or '').strip() or None

    limit = arguments.get('limit', 50)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ToolError('limit must be an integer')
    limit = max(1, min(limit, 200))

    # from_date / to_date determine the lookback window
    from_date_str = (arguments.get('from_date') or '').strip()
    to_date_str = (arguments.get('to_date') or '').strip()

    # Convert from_date to a days lookback for get_trade_history
    from_dt = None
    if from_date_str:
        try:
            from_dt = datetime.fromisoformat(from_date_str)
            days = (datetime.now() - from_dt).days + 1
            days = max(1, min(days, 3650))  # cap at 10 years
        except ValueError:
            raise ToolError('from_date must be ISO 8601 (e.g. 2026-01-01)')
    else:
        days = 365  # default: 1-year lookback

    to_dt = None
    if to_date_str:
        try:
            to_dt = datetime.fromisoformat(to_date_str)
        except ValueError:
            raise ToolError('to_date must be ISO 8601 (e.g. 2026-12-31)')
        if len(to_date_str) == 10:
            # Date-only bound means "through the end of that day".
            to_dt = to_dt + timedelta(days=1) - timedelta(microseconds=1)

    trades = []

    if status in ('closed', 'all'):
        history = db.get_trade_history(user_id, days=days, limit=limit, asset=asset)
        trades.extend(history)

    if status in ('open', 'all'):
        open_trades = db.get_open_trades(user_id, asset=asset)
        trades.extend(open_trades)

    # Apply direction / setup_type filters (DB layer doesn't have these)
    if direction:
        trades = [t for t in trades if (t.get('direction') or '').lower() == direction]
    if setup_type:
        trades = [t for t in trades if t.get('setup_type') == setup_type]

    # Apply the opened_at window in memory. The DB layer only bounds
    # history by a day count and open trades not at all, and its rows carry
    # ``opened_at`` as a datetime on Postgres but a string on SQLite
    # (comparing to an ISO string raised TypeError in production).
    if from_dt or to_dt:
        trades = [
            t for t in trades
            if _opened_within(t.get('opened_at'), from_dt, to_dt)
        ]

    # Cap to limit
    trades = [with_tag_lists(t) for t in trades[:limit]]

    return {
        'trades': trades,
        'count': len(trades),
        'filters': {
            'asset': asset,
            'status': status,
            'direction': direction,
            'setup_type': setup_type,
            'from_date': from_date_str or None,
            'to_date': to_date_str or None,
            'limit': limit,
        },
    }


@mcp_tool(
    'trade_update',
    'Amend mutable fields on an existing journal entry. Ownership is verified — '
    'foreign trade_ids are rejected. '
    'Updatable on open AND closed trades: stop_price, target_price, size, notes, '
    'confidence (1–5), setup_type, tags. '
    'entry_price is updatable on OPEN trades only; raises ToolError on closed ones. '
    'SIDE EFFECTS: writes the updated row and returns the full refreshed entry. '
    'No push notification is fired. '
    'Pass tags=[] to clear all tags. '
    'DESTRUCTIVE? No — existing fields not supplied are left unchanged. '
    'EXAMPLE: update stop after a breakeven move: '
    '{trade_id: 42, stop_price: 65000, notes: "moved to breakeven"}',
    {
        'type': 'object',
        'properties': {
            'trade_id': {
                'type': 'integer',
                'description': 'Trade ID to update (required)',
            },
            'stop_price': {
                'type': 'number',
                'description': 'New stop-loss price (must be > 0)',
            },
            'target_price': {
                'type': 'number',
                'description': 'New target / take-profit price (must be > 0)',
            },
            'entry_price': {
                'type': 'number',
                'description': 'Entry price (must be > 0; only updatable on open trades)',
            },
            'size': {
                'type': 'number',
                'description': 'Position size (must be > 0)',
            },
            'notes': {
                'type': 'string',
                'description': 'Trade notes (max 10 000 chars)',
            },
            'confidence': {
                'type': 'integer',
                'description': 'Confidence level 1–5',
            },
            'setup_type': {
                'type': 'string',
                'description': 'Setup classification (e.g. "breakout", "pullback")',
            },
            'tags': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': (
                    'Replace the trade\'s tags with this list (optional). '
                    'Max 16 items, each at most 50 chars. '
                    'Pass an empty list [] to clear all tags. '
                    'e.g. ["src:agent", "strat:bb_breakdown"].'
                ),
            },
        },
        'required': ['trade_id'],
    },
    min_tier='free',
)
def update_trade_mcp(arguments):
    from carrac.content_db import get_content_db

    user_id = get_mcp_user_id()
    trade_id = arguments.get('trade_id')
    if trade_id is None:
        raise ToolError('trade_id is required')
    try:
        trade_id = int(trade_id)
    except (TypeError, ValueError):
        raise ToolError('trade_id must be an integer')

    db = get_content_db()
    if not db:
        raise ToolError('Journal database not available')

    # Verify ownership
    entry = db.get_journal_entry(trade_id)
    if not entry or entry.get('user_id') != user_id:
        raise ToolError(f'Trade {trade_id} not found or not owned by caller')

    updates = {}

    # Price fields
    for field in ('stop_price', 'target_price', 'size'):
        val = arguments.get(field)
        if val is not None:
            try:
                val = float(val)
            except (TypeError, ValueError):
                raise ToolError(f'{field} must be a number')
            if val <= 0:
                raise ToolError(f'{field} must be positive')
            updates[field] = val

    # entry_price only allowed on open trades
    if 'entry_price' in arguments and arguments['entry_price'] is not None:
        if entry.get('closed_at'):
            raise ToolError('entry_price cannot be changed on a closed trade')
        val = arguments['entry_price']
        try:
            val = float(val)
        except (TypeError, ValueError):
            raise ToolError('entry_price must be a number')
        if val <= 0:
            raise ToolError('entry_price must be positive')
        updates['entry_price'] = val

    if 'notes' in arguments:
        notes = (arguments['notes'] or '')[:10_000]
        updates['notes'] = notes or None

    if 'confidence' in arguments and arguments['confidence'] is not None:
        try:
            conf = int(arguments['confidence'])
        except (TypeError, ValueError):
            raise ToolError('confidence must be an integer 1–5')
        if conf < 1 or conf > 5:
            raise ToolError('confidence must be between 1 and 5')
        updates['confidence'] = conf

    if 'setup_type' in arguments:
        st = (arguments.get('setup_type') or '').strip()[:50]
        updates['setup_type'] = st or None

    if 'tags' in arguments:
        raw_tags = arguments['tags']
        # The src: origin tag survives a tag rewrite — clearing or
        # replacing tags must not silently orphan a row's attribution.
        try:
            existing_tags = json.loads(entry.get('tags') or '[]')
        except (TypeError, ValueError):
            existing_tags = []
        prior_src = next(iter(_src_tags(existing_tags)), None)
        if raw_tags is None or raw_tags == []:
            updates['tags'] = _validate_tags([], inherit_src=prior_src)
        else:
            updates['tags'] = _validate_tags(raw_tags, inherit_src=prior_src)

    if not updates:
        raise ToolError('No updatable fields provided')

    success = db.trade_update(trade_id, **updates)
    if not success:
        raise ToolError(f'Trade {trade_id} update failed')

    updated = db.get_journal_entry(trade_id)
    return {'ok': True, 'entry': with_tag_lists(updated)}


# No longer a standalone MCP tool — absorbed into share_manage
# (entity_type=trade, action=share). Kept as a plain helper, called from
# carrac.mcp.tools.sharing. The legacy 'share_trade' tool name still
# dispatches via registry.LEGACY_TOOL_ALIASES + LEGACY_ARG_DEFAULTS.
#
# Mints (or fetches) the PUBLIC share link for one of the user's CLOSED
# journal trades — a branded result card (signed %, R-multiple,
# asset/direction, hold time). Anyone can open the returned URL with no
# Carrac account: the trade is FROZEN into an immutable snapshot at mint
# time. PRIVACY: the public artifact shows percentages and R only —
# position size, fees and currency P&L NEVER appear. Toggle defaults:
# include_levels (entry/exit prices) ON, include_note (close note) OFF.
# Calling again returns the same active link; passing toggles re-freezes
# the snapshot with those toggles. SIDE EFFECTS: creates a public web page
# + og:image card at the returned URL; counts against the per-tier
# active-share quota (shared pool with play/chart shares) when minting a
# NEW link.
def share_trade(arguments):

    from carrac.content_db import get_content_db
    from carrac.mcp.runtime import get_mcp_caller_tier
    from carrac.plays.shares import (
        count_active_shares,
        create_trade_share,
        get_active_share_for_trade,
        share_quota,
    )

    user_id = get_mcp_user_id()
    trade_id = arguments.get('trade_id')
    if not trade_id:
        raise ToolError('trade_id is required')

    trade = get_content_db().get_journal_entry(trade_id)
    if not trade or trade.get('user_id') != user_id:
        raise ToolError(f'Trade {trade_id} not found')
    if not trade.get('closed_at'):
        raise ToolError(
            f'Trade {trade_id} is still open — only closed trades can be '
            f'shared (trade_close first)')

    # Quota only charges NEW mints — re-sharing returns the existing link.
    if get_active_share_for_trade(trade_id, user_id) is None:
        tier = get_mcp_caller_tier()
        quota = share_quota(tier)
        current = count_active_shares(user_id)
        if current >= quota:
            raise ToolError(
                f'Share limit reached ({current}/{quota} active links on the '
                f'{tier} tier). Revoke an old share or upgrade for more.')

    toggles = {k: arguments[k] for k in ('include_levels', 'include_note')
               if k in arguments}
    share = create_trade_share(user_id, trade_id, **toggles)
    if not share:
        raise ToolError(f'Trade {trade_id} not found')

    base = site_base_url()
    snap = share.get('snapshot') or {}
    return {
        'slug': share['slug'],
        'url': f"{base}/s/{share['slug']}",
        'image_url': f"{base}/s/{share['slug']}/card.png",
        'include_levels': share.get('include_plan'),
        'include_note': share.get('include_thesis'),
        'pnl_pct': snap.get('pnl_pct'),
        'rr_actual': snap.get('rr_actual'),
        'view_count': share.get('view_count', 0),
    }


# No longer a standalone MCP tool — absorbed into share_manage
# (entity_type=trade, action=unshare). Revokes the public share link(s) for
# one of the user's journal trades. SIDE EFFECTS: the public URL immediately
# returns 410 Gone; the freed slot returns to the per-tier active-share
# quota. The slug is NOT reusable — a later share re-mints a fresh link.
def unshare_trade(arguments):
    from carrac.plays.shares import revoke_trade_share

    trade_id = arguments.get('trade_id')
    if not trade_id:
        raise ToolError('trade_id is required')

    revoked = revoke_trade_share(trade_id, get_mcp_user_id())
    return {'trade_id': trade_id, 'revoked': revoked}
