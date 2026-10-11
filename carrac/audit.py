"""Audit log helper for admin and system actions.

All write operations that modify user entitlements or financial data should
call `log()` so there is a persistent DB trail. The audit_log table is
created by an Alembic migration.

Usage:
    from carrac.audit import audit_log
    audit_log.log(
        action='tier_override',
        actor=session['user_id'],
        subject=target_user_id,
        before={'tier': old_tier},
        after={'tier': new_tier},
        source='admin_ui',
        note='Manual correction',
    )
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from carrac.db_pool import get_conn, get_db_type

logger = logging.getLogger('carrac.audit')


class AuditLog:
    """Thin wrapper around the audit_log table."""

    # Well-known action constants — keeps callers from typo-ing strings.
    TIER_OVERRIDE = 'tier_override'
    ADD_CREDITS = 'add_credits'
    DEDUCT_CREDITS = 'deduct_credits'
    MANUAL_REFUND = 'manual_refund'

    def log(
        self,
        action: str,
        actor: Optional[int],
        subject: Optional[int],
        before: Optional[Any] = None,
        after: Optional[Any] = None,
        source: str = 'unknown',
        note: Optional[str] = None,
        conn=None,
    ) -> None:
        """Write an audit row.

        Args:
            action: Short action name, e.g. 'tier_override'.
            actor:  User-ID of the admin or operator performing the action.
                    None if triggered by an automated system process.
            subject: User-ID of the user being affected. None for global ops.
            before:  Dict/value representing state before the action.
            after:   Dict/value representing state after the action.
            source:  Origin: 'admin_ui', 'mcp', 'script', 'webhook', …
            note:    Optional free-text explanation.
            conn:    If provided, use this connection (for callers already
                     inside a transaction). If None, a new connection is opened.
        """
        before_json = json.dumps(before) if before is not None else None
        after_json = json.dumps(after) if after is not None else None

        try:
            if conn is not None:
                # Caller supplied a connection (already in a transaction).
                self._write(conn, action, actor, subject, before_json, after_json, source, note)
            elif get_db_type() == 'postgresql':
                # PostgreSQL: use RAII get_conn() so the pool slot is returned
                # immediately after the write (avoids pool exhaustion).
                with get_conn() as _conn:
                    self._write(_conn, action, actor, subject, before_json, after_json, source, note)
            else:
                # SQLite: audit_log lives in users.db, not carrac.db.
                # Must use user_db's thread-local conn to write to the right file.
                from user_db import user_db as _udb
                _conn = _udb._get_conn()
                self._write(_conn, action, actor, subject, before_json, after_json, source, note)
                _conn.commit()
            logger.debug(
                f'audit_log: action={action} actor={actor} subject={subject} source={source}'
            )
        except Exception as exc:
            # Audit log failure must NEVER break the business operation.
            # Log loudly but swallow so callers are not disrupted.
            logger.error(
                f'audit_log write failed (action={action} actor={actor} subject={subject}): {exc}',
                exc_info=True,
            )

    def _write(self, conn, action, actor, subject, before_json, after_json, source, note):
        """Internal: execute the INSERT on an already-open connection."""
        if get_db_type() == 'postgresql':
            conn.execute(
                '''INSERT INTO audit_log
                   (actor_user_id, subject_user_id, action, before_json, after_json, source, note)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)''',
                (actor, subject, action, before_json, after_json, source, note),
            )
        else:
            conn.execute(
                '''INSERT INTO audit_log
                   (actor_user_id, subject_user_id, action, before_json, after_json, source, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (actor, subject, action, before_json, after_json, source, note),
            )

    def get_for_subject(self, subject_user_id: int, limit: int = 50) -> list[dict]:
        """Fetch recent audit rows for a subject user (admin UI helper)."""
        try:
            if get_db_type() == 'postgresql':
                # PostgreSQL: RAII — pool slot returned after read.
                with get_conn() as conn:
                    rows = conn.execute(
                        '''SELECT id, ts, actor_user_id, action, before_json, after_json, source, note
                           FROM audit_log WHERE subject_user_id = %s
                           ORDER BY ts DESC LIMIT %s''',
                        (subject_user_id, limit),
                    ).fetchall()
                return [dict(r) for r in rows]
            # SQLite: audit_log lives in users.db — must use user_db's conn.
            from user_db import user_db as _udb
            conn = _udb._get_conn()
            try:
                rows = conn.execute(
                    '''SELECT id, ts, actor_user_id, action, before_json, after_json, source, note
                       FROM audit_log WHERE subject_user_id = ?
                       ORDER BY ts DESC LIMIT ?''',
                    (subject_user_id, limit),
                ).fetchall()
                conn.rollback()
                return [dict(r) for r in rows]
            except Exception:
                try: conn.rollback()
                except Exception: pass
                raise
        except Exception as exc:
            logger.error(f'audit_log.get_for_subject failed: {exc}')
            return []


# Module-level singleton — import as `from carrac.audit import audit_log`
audit_log = AuditLog()
