#!/usr/bin/env python3
"""Prune AI prompt/response logs older than 30 days.

The retention window mirrors the GDPR-friendly disclosure in Privacy §8.
Run daily from cron (see deploy/crontab.example):

    0 4 * * *  cd /path/to/carrac && \
               docker compose exec -T carrac python scripts/ai_log_retention_prune.py
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timedelta, timezone


if __name__ == "__main__":
    # Answer --help before anything below is imported or touched.
    import argparse
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()


RETENTION_DAYS = int(os.environ.get('AI_LOG_RETENTION_DAYS', '30'))


def main() -> int:
    root = (os.environ.get('AI_LOG_DIR')
            or os.path.join(os.environ.get('DATA_DIR', 'data'), 'ai-logs'))
    if not os.path.isdir(root):
        print(f"AI log dir absent: {root} — nothing to prune.")
        return 0

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=RETENTION_DAYS)
    pruned = 0
    for entry in sorted(os.listdir(root)):
        try:
            d = datetime.strptime(entry, '%Y-%m-%d').date()
        except ValueError:
            continue
        if d < cutoff:
            path = os.path.join(root, entry)
            try:
                shutil.rmtree(path)
                pruned += 1
                print(f"  pruned {path}")
            except Exception as e:
                print(f"  ERROR pruning {path}: {e}", file=sys.stderr)

    print(f"Done. Removed {pruned} day-folders older than {RETENTION_DAYS} days.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
