"""
Plays monitor — background condition checker.

Runs in-process on each cache refresh cycle.
Evaluates conditions for all active plays, auto-transitions states
(invalidation, expiry), and dispatches notifications.

Reuses the shared condition engine (evaluate_conditions_dict) — no
duplicate evaluation logic.
"""

import logging
import time
from datetime import datetime, timezone

from carrac.conditions import evaluate_conditions_dict
from carrac.plays.models import (
    get_active_plays_for_monitoring, transition_status,
    persist_condition_state,
)

logger = logging.getLogger('carrac.plays.monitor')


class PlaysMonitor:
    """Evaluates active plays against cached market data."""

    def __init__(self):
        self._last_run = 0
        self._min_interval = 120  # seconds

    def check_all_active(self, cached_data=None):
        """Check conditions for all active plays.

        Args:
            cached_data: HTF response_cache data dict. If None, fetches from cache.
        """
        now = time.time()
        if now - self._last_run < self._min_interval:
            return
        self._last_run = now

        if cached_data is None:
            cached_data = self._get_cached_data()
        if not cached_data or 'assets' not in cached_data:
            return

        # Build asset lookup
        all_assets = {}
        for asset in cached_data.get('assets', []):
            sym = asset.get('symbol', '').upper()
            if sym:
                all_assets[sym] = asset

        plays = get_active_plays_for_monitoring()
        if not plays:
            return

        paid_user_ids = self._get_paid_user_ids()

        checked = 0
        transitioned = 0
        for play in plays:
            user_id = play.get('user_id')
            if user_id not in paid_user_ids:
                continue

            primary = (play.get('primary_asset') or '').upper()
            # Use trigger_asset for market data if set (cross-asset plays)
            data_asset = (play.get('trigger_asset') or primary).upper()
            asset_data = all_assets.get(data_asset, {})
            rt = asset_data.get('realtime', {})
            price = rt.get('price', asset_data.get('latest_price', 0))

            if not price and not play.get('expiry'):
                continue  # No price data and no expiry to check

            market_data = {
                'price': price or 0,
                'asset_data': asset_data,
                'all_assets': all_assets,
            }

            try:
                result = self.play_evaluate(play, market_data)
                checked += 1

                # Persist updated condition state
                self._persist_state(play, result)

                # Compute auto-transition
                new_status = self._compute_transition(play, result)
                if new_status and new_status != play.get('status'):
                    resolution = self._build_resolution(new_status, result)
                    updated = transition_status(
                        play['id'], user_id, new_status,
                        resolution=resolution,
                    )
                    if updated:
                        transitioned += 1
                        logger.info(
                            f"Play {play['id']} ({play.get('title')}) "
                            f"transitioned: {play['status']} -> {new_status}"
                        )
                        self._notify_transition(user_id, play, new_status, result, price=price)

            except Exception as e:
                logger.warning(f"Error evaluating play {play.get('id')}: {e}")

        if checked > 0:
            logger.info(f"Plays monitor: checked {checked}, transitioned {transitioned}")

    def play_evaluate(self, play, market_data):
        """Evaluate a single play's conditions and invalidation.

        Returns dict with conditions_result, invalidation_triggered,
        invalidation (updated), and expired flag.
        """
        context = {
            'direction': play.get('direction'),
            # The licensing gate on derivatives_metric conditions
            # asks whose play this is. This monitor runs in a background thread
            # with no request context, so the caller-based gate cannot answer —
            # it must be the stored owner.
            'owner_user_id': play.get('user_id'),
        }

        # Evaluate main conditions
        conditions = play.get('conditions') or {'rules': [], 'logic': 'all'}
        result = evaluate_conditions_dict(conditions, market_data, context=context)

        # Evaluate single invalidation condition
        invalidation = play.get('invalidation')
        invalidation_triggered = False
        if invalidation and isinstance(invalidation, dict) and invalidation.get('type'):
            inv_conds = {'rules': [invalidation], 'logic': 'all'}
            inv_result = evaluate_conditions_dict(inv_conds, market_data, context=context)
            invalidation_triggered = inv_result.get('all_met', False)
            if inv_result.get('rules'):
                ev = inv_result['rules'][0]
                invalidation['met'] = ev.get('met', False)
                invalidation['met_at'] = ev.get('met_at')
                invalidation['evidence'] = ev.get('evidence', '')

        # Check expiry
        expired = False
        expiry = play.get('expiry')
        if expiry:
            try:
                if isinstance(expiry, str):
                    expiry_dt = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
                else:
                    expiry_dt = expiry
                if datetime.now(timezone.utc) >= expiry_dt:
                    expired = True
            except (ValueError, TypeError):
                pass

        return {
            'conditions_result': result,
            'invalidation_triggered': invalidation_triggered,
            'invalidation': invalidation,
            'expired': expired,
        }

    def _compute_transition(self, play, eval_result):
        """Determine auto-transition based on evaluation result."""
        current = play['status']

        # Priority 1: Invalidation (watching or acting)
        if eval_result['invalidation_triggered'] and current in ('watching', 'acting'):
            return 'invalidated'

        # Priority 2: Expiry (watching only — acting plays don't auto-expire)
        if eval_result['expired'] and current == 'watching':
            return 'expired'

        return None

    def _persist_state(self, play, eval_result):
        """Update condition met/met_at/evidence in DB."""
        conditions = play.get('conditions') or {}
        rules = conditions.get('rules', [])
        evaluated = eval_result['conditions_result'].get('rules', [])

        if rules and evaluated:
            eval_lookup = {r.get('id'): r for r in evaluated if r.get('id')}
            for rule in rules:
                rid = rule.get('id')
                if rid and rid in eval_lookup:
                    ev = eval_lookup[rid]
                    rule['met'] = ev.get('met', False)
                    rule['met_at'] = ev.get('met_at')
                    rule['evidence'] = ev.get('evidence', '')

        try:
            persist_condition_state(
                play['id'], play['user_id'],
                conditions,
                invalidation=eval_result.get('invalidation'),
            )
        except Exception as e:
            logger.error("Failed to persist condition state for play %s: %s", play['id'], e)

    def _build_resolution(self, new_status, eval_result):
        """Build resolution text for auto-transitions."""
        if new_status == 'invalidated':
            inv = eval_result.get('invalidation', {})
            evidence = inv.get('evidence', '') if isinstance(inv, dict) else ''
            return f"Auto-invalidated: {evidence}" if evidence else "Auto-invalidated by condition monitor"
        elif new_status == 'expired':
            return "Expiry date reached"
        return None

    def _notify_transition(self, user_id, play, new_status, eval_result, price=0):
        """Dispatch notifications for a play status transition."""
        title = play.get('title', 'Untitled')
        asset = play.get('primary_asset') or '?'
        conds = eval_result['conditions_result']
        met = conds.get('met_count', 0)
        total = conds.get('total_count', 0)

        if new_status == 'invalidated':
            inv = eval_result.get('invalidation', {})
            evidence = inv.get('evidence', 'Invalidation condition met') if isinstance(inv, dict) else ''
            push_body = f"{title} — {met}/{total} conditions met"
            channel_body = f"{asset} — Play INVALIDATED: {title}\n{evidence}"
        elif new_status == 'expired':
            push_body = f"{title} — expiry date reached"
            channel_body = f"{asset} — Play EXPIRED: {title}\nExpiry date reached."
        else:
            push_body = f"{title} — {new_status}"
            channel_body = f"{asset} — Play {new_status}: {title}"

        self._send_push(user_id, asset, new_status, push_body)
        self._send_to_user_channels(
            user_id, asset, channel_body,
            alert_id=play.get('id'), price=price,
        )

    def _send_push(self, user_id, asset, event, body):
        try:
            from push_notifications import send_push_notification
            push_title = f"{asset} Play: {event.upper()}"
            send_push_notification(
                user_id=str(user_id),
                title=push_title,
                body=body,
                symbol=asset,
                tag=f'play-{event}',
            )
        except Exception as e:
            logger.debug(f"Push notification failed: {e}")

    def _send_to_user_channels(self, user_id, asset, body, *, alert_id=None, price=0):
        try:
            from push_notifications import _send_user_notifications
            # this is the second of the two evaluators
            # (carrac/conditions.py via play_evaluate
            # above) — alert_id/alert_type/condition ensure a play
            # transition reaches the webhook dispatch seam exactly like
            # indicator/formula/price/quant alerts do. target_price/
            # current_price stay 0 (unchanged) so the existing ntfy/
            # Telegram/Discord text-body format for plays is untouched —
            # `price` is threaded through ONLY as the webhook payload
            # price via webhook_price, a separate kwarg.
            _send_user_notifications(
                str(user_id), asset, 'play', 0, 0, body,
                alert_id=alert_id, alert_type='play', condition=body,
                webhook_price=price,
            )
        except Exception as e:
            logger.debug(f"Play user notification failed: {e}")

    def _get_paid_user_ids(self):
        try:
            from carrac.db_pool import get_conn, adapt_query
            with get_conn() as conn:
                cur = conn.execute(adapt_query(
                    "SELECT id FROM users WHERE tier IN ('member', 'tester', 'premium', 'admin')"
                ))
                return {row['id'] if isinstance(row, dict) else row[0] for row in cur.fetchall()}
        except Exception as e:
            logger.warning(f"Failed to get paid user IDs: {e}")
            return set()

    def _get_cached_data(self):
        try:
            # response_cache lives in carrac.precompute; importing it from
            # carrac.redis_cache never resolves and would silently return None
            # whenever check_all_active is called without cached_data.
            from carrac.precompute import response_cache
            return response_cache.get('htf')
        except Exception as e:
            logger.warning(f"Failed to get cached data: {e}")
            return None


# Module-level singleton
plays_monitor = PlaysMonitor()
