"""Notification channel resilience: per-channel circuit-breaker + DLQ.

Each channel (web_push, ntfy, discord, telegram) gets its own breaker keyed
by user. Consecutive failures trip the breaker open; while open, sends are
enqueued to the DLQ and skipped. A half-open probe is attempted after the
cooldown expires — one successful delivery closes the breaker.

Design notes:
- Storage is in-memory with a thread-safe dict. A future Redis backend can
  be swapped in by replacing `_state` with a Redis-backed store.
- DLQ is a JSONL file at data/alert_dlq.jsonl (append-only, admin replay
  script tails it). Redis-backed variant kept for later.
- The module never raises: all callers get a bool back indicating delivery.
- the error tracker is pinged once when a breaker trips open (escalation, not per-fail).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

from carrac.notifications.redact import describe_exception, redact_secrets

logger = logging.getLogger('carrac.notifications.resilience')

DATA_DIR = Path(os.environ.get('DATA_DIR', 'data'))
DLQ_FILE = DATA_DIR / 'alert_dlq.jsonl'

# The DLQ records only failures, and `_record_success` writes to
# `_state`, which is per-process. So until now NOTHING durably recorded that a
# delivery SUCCEEDED, and "which of this user's channels is still delivering"
# was unanswerable outside the one process that happened to make the send.
#
# That question is the whole escape hatch for a delivery-outage alarm: telling
# a user their notifications are broken over the notifications that are broken
# is circular. When ntfy and telegram are dead while
# discord still delivers, a working channel is available and nothing could
# see it.
#
# One small JSON file, last-writer-wins, rewritten atomically. It holds a
# timestamp per (channel, user), not a history — the DLQ is the history.
HEALTH_FILE = DATA_DIR / 'delivery_health.json'
_health_lock = threading.Lock()

# Circuit-breaker thresholds. Tuned so a flaky channel (1-2 blips) doesn't
# trip, but a genuine outage (Telegram/Discord down for a minute) does.
CONSECUTIVE_FAILURES_TO_OPEN = 5
COOLDOWN_SECONDS = 120
HALF_OPEN_PROBES = 1


@dataclass
class _BreakerState:
    consecutive_failures: int = 0
    open_until: float = 0.0  # epoch; if now < open_until, breaker is open
    half_open_probes_in_flight: int = 0
    tripped_at: Optional[float] = None
    last_error: str = ''

    def is_open(self) -> bool:
        return time.time() < self.open_until

    def is_half_open(self) -> bool:
        return (
            self.open_until > 0
            and time.time() >= self.open_until
            and self.consecutive_failures >= CONSECUTIVE_FAILURES_TO_OPEN
        )


_state: Dict[str, _BreakerState] = {}
_state_lock = threading.RLock()


def _key(channel: str, user_id: str) -> str:
    return f'{channel}:{user_id}'


def _get_state(channel: str, user_id: str) -> _BreakerState:
    k = _key(channel, user_id)
    with _state_lock:
        if k not in _state:
            _state[k] = _BreakerState()
        return _state[k]


def _record_success(channel: str, user_id: str):
    st = _get_state(channel, user_id)
    with _state_lock:
        was_open = st.consecutive_failures >= CONSECUTIVE_FAILURES_TO_OPEN
        st.consecutive_failures = 0
        st.open_until = 0.0
        st.half_open_probes_in_flight = 0
        st.tripped_at = None
        st.last_error = ''
    if was_open:
        logger.info(f'circuit-breaker CLOSED: channel={channel} user={user_id}')


def _record_failure(channel: str, user_id: str, error: str):
    st = _get_state(channel, user_id)
    tripped_now = False
    with _state_lock:
        st.consecutive_failures += 1
        st.last_error = error[:200]
        if st.consecutive_failures >= CONSECUTIVE_FAILURES_TO_OPEN and st.open_until < time.time():
            st.open_until = time.time() + COOLDOWN_SECONDS
            st.tripped_at = time.time()
            tripped_now = True
    if tripped_now:
        logger.warning(
            f'circuit-breaker OPEN: channel={channel} user={user_id} '
            f'fails={st.consecutive_failures} error={error[:80]}'
        )
        _report_tripped_to_bugsink(channel, user_id, error)


def _report_tripped_to_bugsink(channel: str, user_id: str, error: str):
    """Escalate a breaker trip to the error tracker. Silent on misconfigured DSN.

    The scope MUST be set before capture_message, not after. Capturing first
    sends the 'Notification channel breaker tripped' event with no channel tag,
    no user_id and no last_error — the three things you need to act on it —
    and forces diagnosis through worker logs, which is exactly the fallback
    the error tracker exists to remove.

    A FORKED scope, not configure_scope(), so the tags attach to THIS event
    only and do not bleed into whatever the worker reports next. sentry-sdk
    2.x renamed push_scope() to new_scope() and warns on the old name, so
    the call is resolved at runtime rather than pinned to either spelling.
    """
    try:
        import sentry_sdk  # sentry-sdk ships via sentry-sdk[flask]
        fork_scope = getattr(sentry_sdk, 'new_scope', None) or sentry_sdk.push_scope
        with fork_scope() as scope:
            scope.set_tag('channel', channel)
            scope.set_tag('user_id', str(user_id))
            scope.set_context('breaker', {'last_error': error[:200]})
            sentry_sdk.capture_message(
                f'Notification channel breaker tripped: {channel}',
                level='warning',
            )
    except Exception:
        pass


def deliver(
    channel: str,
    user_id: str,
    send_fn: Callable[[], bool],
    payload: dict,
) -> bool:
    """Invoke send_fn through the circuit-breaker.

    send_fn is a zero-arg callable returning True on success. payload is the
    serializable message shape used only for DLQ enqueue on failure.

    Returns True iff delivery succeeded. Open breaker → DLQ + False.
    """
    st = _get_state(channel, user_id)

    if st.is_open():
        _dlq_enqueue(channel, user_id, payload, reason='breaker_open')
        return False

    if st.is_half_open():
        with _state_lock:
            if st.half_open_probes_in_flight >= HALF_OPEN_PROBES:
                _dlq_enqueue(channel, user_id, payload, reason='half_open_probe_in_flight')
                return False
            st.half_open_probes_in_flight += 1

    try:
        ok = bool(send_fn())
    except Exception as e:
        ok = False
        err = describe_exception(e)
        _record_failure(channel, user_id, err)
        record_delivery_outcome(channel, user_id, False)
        _dlq_enqueue(channel, user_id, payload, reason='exception', error=err)
        return False

    if ok:
        _record_success(channel, user_id)
        record_delivery_outcome(channel, user_id, True)
        return True

    _record_failure(channel, user_id, 'send_fn returned False')
    record_delivery_outcome(channel, user_id, False)
    _dlq_enqueue(channel, user_id, payload, reason='send_failed',
                 error='send_fn returned False')
    return False


# Payload keys that name where a message was going. Each is a credential
# (a Discord webhook URL, an ntfy topic URL, a bot token), and each is the
# user's saved setting for that channel, so (channel, user_id) finds it again.
_DESTINATION_KEYS = frozenset({'url', 'webhook_url', 'bot_token', 'token'})


def _dlq_enqueue(channel: str, user_id: str, payload: dict, reason: str,
                 error: str = ''):
    """Append a failed delivery to the DLQ file (JSONL, append-only).

    `error` is the whole point of the record: it says WHY a delivery failed.
    `_ok_or_raise` carries the HTTP status and body into the exception; that
    string lands here.

    The record never holds a credential: the error text is redacted and the
    destination is dropped from the payload (see ``_DESTINATION_KEYS``).
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in (payload or {}).items() if k not in _DESTINATION_KEYS}
        rec = {
            'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'channel': channel,
            'user_id': str(user_id),
            'reason': reason,
            'error': redact_secrets(error or '')[:300].replace('\n', ' ').replace('\r', ' '),
            'payload': payload,
        }
        with open(DLQ_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception as e:
        # DLQ itself failing is non-fatal, but the notification is now lost.
        logger.error('DLQ enqueue failed for %s notification to user %s: %s', channel, user_id, e)


def undelivered_summary(
    user_id: str,
    since_hours: int = 48,
    max_bytes: int = 262144,
) -> Dict[str, dict]:
    """Per-channel count of alerts this user was never sent, from the DLQ.

Returns {channel: {'count', 'last_ts', 'last_reason'}} for
    one user, restricted to the last `since_hours`. Empty dict means every
    delivery in the window succeeded — which is also what a missing or
    unreadable DLQ returns, deliberately: this feeds a UI warning, and a
    warning invented from a read error is worse than no warning.

    WHY THE DLQ AND NOT THE BREAKER. `breaker_status()` reads `_state`,
    which is an in-process dict (see the module docstring). Alert delivery
    runs in the WORKER process; the settings page is served by the WEB
    process. The web process therefore has a permanently empty breaker map
    and would report every channel healthy no matter how badly it is
    failing. The DLQ file is the one durable, shared record of a delivery
    that did not happen — prod's had 511 undelivered ntfy alerts sitting in
    it, for a user whose settings page said the channel was enabled.

    Only the tail is read (`max_bytes`, default 256 KiB): the file is
    append-only and never rotated, so an unbounded read would get slower
    every day this stays broken. The first line of the tail is dropped
    because a byte offset lands mid-record. `dlq_tail()` deliberately is
    not reused here — it does `f.readlines()` on the whole file, which is
    acceptable for an occasional admin call and not for a page load.
    """
    out: Dict[str, dict] = {}
    uid = str(user_id)
    try:
        if not DLQ_FILE.exists():
            return out
        size = DLQ_FILE.stat().st_size
        with open(DLQ_FILE, 'rb') as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # discard the partial record at the seek point
            raw = f.read().decode('utf-8', errors='replace')
    except Exception as e:
        logger.warning(f'DLQ read failed: {e}')
        return out

    cutoff = time.time() - since_hours * 3600
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if str(rec.get('user_id')) != uid:
            continue
        ts = rec.get('ts') or ''
        try:
            when = datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp()
        except Exception:
            # An unparseable stamp must not silently drop a real failure.
            when = cutoff
        if when < cutoff:
            continue
        ch = rec.get('channel') or 'unknown'
        slot = out.setdefault(ch, {'count': 0, 'last_ts': None,
                                   'last_reason': None, 'last_error': None})
        slot['count'] += 1
        if slot['last_ts'] is None or ts >= slot['last_ts']:
            slot['last_ts'] = ts
            slot['last_reason'] = rec.get('reason')
            # Older records predate the `error` field, so this
            # stays None for them rather than inventing a cause. Records
            # written before errors were redacted are redacted here.
            err = rec.get('error')
            slot['last_error'] = redact_secrets(err) if err else None
    return out


def _health_load() -> Dict[str, dict]:
    """Read the health ledger. A missing or corrupt file reads as empty.

    Empty means "nothing known", never "everything is fine" — every caller
    below has to treat an absent last_success as unproven, not as healthy.
    """
    try:
        if not HEALTH_FILE.exists():
            return {}
        with open(HEALTH_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f'delivery health read failed: {e}')
        return {}


def _health_save(data: Dict[str, dict]) -> None:
    """Atomic rewrite — a torn read here would be indistinguishable from a
    channel that never succeeded, which is exactly the wrong way to be wrong."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = HEALTH_FILE.with_suffix('.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, HEALTH_FILE)
    except Exception as e:
        logger.warning(f'delivery health write failed: {e}')


def record_delivery_outcome(channel: str, user_id: str, ok: bool) -> None:
    """Stamp the durable last_success / last_failure for one (channel, user).

    Called from `deliver()` on real attempt outcomes only. A breaker-open skip
    is deliberately NOT recorded: nothing was attempted, so it is evidence
    about neither health nor failure, and stamping it would keep last_failure
    fresh forever and make a recovered channel look permanently broken.

    A success also clears `alarmed_at`, which is what makes the outage
    alarm self-clearing: the next outage on the same channel can raise again.
    """
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    key = f'{channel}:{user_id}'
    with _health_lock:
        data = _health_load()
        slot = data.get(key) or {}
        if ok:
            slot['last_success'] = now
            slot.pop('alarmed_at', None)
        else:
            slot['last_failure'] = now
        data[key] = slot
        _health_save(data)


def clear_outage_latch(user_id: str, channel: str) -> None:
    """Drop one alarm latch. Used when the condition it recorded no longer
    holds but no delivery outcome will ever clear it — a user who had no
    channels and has now configured one."""
    key = f'{channel}:{user_id}'
    with _health_lock:
        data = _health_load()
        slot = data.get(key)
        if not slot or 'alarmed_at' not in slot:
            return
        slot.pop('alarmed_at', None)
        data[key] = slot
        _health_save(data)


def channel_health(user_id: str) -> Dict[str, dict]:
    """Per-channel durable delivery health for one user.

    Returns {channel: {'last_success', 'last_failure', 'alarmed_at', 'broken'}}.

    `broken` is "the most recent ATTEMPT on this channel failed" — a failure
    with no success after it. A channel with no record at all is absent from
    the result rather than reported healthy; callers must not read silence as
    health, which is the mistake `breaker_status()` makes structurally.
    """
    uid = str(user_id)
    out: Dict[str, dict] = {}
    with _health_lock:
        data = _health_load()
    for key, slot in data.items():
        ch, _, who = key.partition(':')
        if who != uid or not ch:
            continue
        succ = slot.get('last_success')
        fail = slot.get('last_failure')
        out[ch] = {
            'last_success': succ,
            'last_failure': fail,
            'alarmed_at': slot.get('alarmed_at'),
            'broken': bool(fail) and (not succ or fail > succ),
        }
    return out


def mark_outage_alarmed(user_id: str, channels) -> None:
    """Record that the outage alarm has been raised for these channels.

    Once per outage, not per fire — an alarm that repeats every
    tick becomes furniture, and then it trains you to ignore it. Cleared by
    `record_delivery_outcome` on the next success.
    """
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    uid = str(user_id)
    with _health_lock:
        data = _health_load()
        for ch in channels:
            key = f'{ch}:{uid}'
            slot = data.get(key) or {}
            slot['alarmed_at'] = now
            data[key] = slot
        _health_save(data)


def breaker_status() -> Dict[str, dict]:
    """Snapshot of all breakers — for admin UI / health endpoint.

    ⚠ In-process only. A caller in the web process sees nothing about
    deliveries attempted by the worker — use undelivered_summary() for
    anything user-facing.
    """
    with _state_lock:
        out = {}
        now = time.time()
        for k, st in _state.items():
            out[k] = {
                'consecutive_failures': st.consecutive_failures,
                'is_open': st.is_open(),
                'open_seconds_remaining': max(0, st.open_until - now) if st.is_open() else 0,
                'tripped_at': (
                    datetime.fromtimestamp(st.tripped_at, timezone.utc).isoformat().replace('+00:00', 'Z')
                    if st.tripped_at else None
                ),
                'last_error': st.last_error,
            }
        return out


def reset_all_breakers():
    """Wipe all in-memory state. Test-only."""
    with _state_lock:
        _state.clear()


def dlq_tail(n: int = 100) -> list:
    """Read the last n DLQ records. Admin use."""
    if not DLQ_FILE.exists():
        return []
    try:
        with open(DLQ_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
    except Exception as e:
        logger.warning(f'DLQ tail failed: {e}')
        return []
