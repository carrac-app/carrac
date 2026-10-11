"""Run-log rows in ``quant.collection_log`` for the engine batch jobs.

Each engine module (conditional, correlations, factors, leadlag, regime)
brackets its run with ``log_start`` / ``log_finish`` so the health checks can
see when it last ran and how it ended. A failure to write the row is logged at
ERROR and never aborts the engine run itself.
"""

import logging

from carrac.db_pool import get_quant_conn as get_conn

logger = logging.getLogger('quant.engine.run_log')


def log_start(name, dry_run):
    if dry_run:
        return None
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO quant.collection_log (collector, status)
                VALUES (%s, 'running') RETURNING id
            """, (name,))
            row = cur.fetchone()
            return row['id'] if row else None
    except Exception as exc:
        logger.error("collection_log: start row for %s not written: %s", name, exc)
        return None


def log_finish(log_id, status, rows, dry_run, error_msg=None):
    if dry_run or not log_id:
        return
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE quant.collection_log
                SET finished_at = NOW(), status = %s,
                    rows_upserted = %s, error_msg = %s
                WHERE id = %s
            """, (status, rows, error_msg, log_id))
    except Exception as exc:
        logger.error("collection_log: row %s not finished (%s): %s", log_id, status, exc)
