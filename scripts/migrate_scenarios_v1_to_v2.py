#!/usr/bin/env python3
"""
Migrate drawing-based scenarios (v1) to structured scenarios (v2).

Reads scenario drawings from the drawings table (drawing_type='scenario'),
creates matching rows in the expanded scenarios table, and links child drawings.

Safe to run multiple times — skips scenarios that already have v2 entries.

Usage:
    python scripts/migrate_scenarios_v1_to_v2.py [--dry-run]
"""

import json
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from carrac.db_pool import get_conn, adapt_query, returning_id, get_last_id, get_db_type


def migrate(dry_run=False):
    """Migrate v1 drawing-based scenarios to v2 structured scenarios."""
    with get_conn() as conn:
        # Find all top-level scenario drawings (drawing_type='scenario', no scenario_id parent)
        cur = conn.execute(adapt_query('''
            SELECT id, user_id, asset, timeframe, label, metadata, created_at, updated_at
            FROM drawings
            WHERE drawing_type = 'scenario' AND (scenario_id IS NULL OR scenario_id = 0)
        '''))
        scenario_drawings = [dict(r) for r in cur.fetchall()]

        if not scenario_drawings:
            print("No v1 scenario drawings found. Nothing to migrate.")
            return

        print(f"Found {len(scenario_drawings)} v1 scenario drawings to migrate.")

        migrated = 0
        skipped = 0

        for sd in scenario_drawings:
            drawing_id = sd['id']
            user_id = sd['user_id']
            asset = sd['asset']
            timeframe = sd['timeframe']
            label = sd.get('label', '')

            # Parse metadata
            meta = {}
            if sd.get('metadata'):
                try:
                    meta = json.loads(sd['metadata']) if isinstance(sd['metadata'], str) else sd['metadata']
                except (json.JSONDecodeError, TypeError):
                    meta = {}

            name = meta.get('name', label or f'{asset} scenario')
            thesis = meta.get('thesis', '')
            is_archived = meta.get('archived', False)

            # Determine direction from metadata or scenario_type
            direction_map = {'bullish': 'long', 'bearish': 'short'}
            direction = direction_map.get(meta.get('direction'), None)

            # Check if already migrated (scenario with this drawing_id in linked_drawing_ids)
            if get_db_type() == 'postgresql':
                check_cur = conn.execute(adapt_query(
                    "SELECT id FROM scenarios WHERE user_id = %s AND label = %s AND asset = %s AND timeframe = %s LIMIT 1"
                ), (user_id, name, asset, timeframe))
            else:
                check_cur = conn.execute(adapt_query(
                    "SELECT id FROM scenarios WHERE user_id = ? AND label = ? AND asset = ? AND timeframe = ? LIMIT 1"
                ), (user_id, name, asset, timeframe))

            if check_cur.fetchone():
                skipped += 1
                print(f"  SKIP: '{name}' ({asset}/{timeframe}) — already exists")
                continue

            # Get child drawings
            child_cur = conn.execute(adapt_query(
                'SELECT id FROM drawings WHERE scenario_id = ?'
            ), (drawing_id,))
            child_ids = [r['id'] if isinstance(r, dict) else r[0] for r in child_cur.fetchall()]
            linked_ids = [drawing_id] + child_ids

            status = 'resolved' if is_archived else 'watching'
            outcome = 'cancelled' if is_archived else None
            now = sd.get('created_at')

            if dry_run:
                print(f"  DRY-RUN: Would create '{name}' ({asset}/{timeframe}) "
                      f"status={status}, {len(linked_ids)} linked drawings")
                migrated += 1
                continue

            # Insert into scenarios table
            targets_json = '[]'
            conditions_json = '{"rules": [], "logic": "all"}'
            linked_json = json.dumps(linked_ids)
            tags_val = '[]' if get_db_type() == 'sqlite' else []

            sql = adapt_query('''
                INSERT INTO scenarios (
                    user_id, asset, timeframe, label, thesis, notes,
                    scenario_type, direction, status, status_changed_at,
                    is_active, outcome, targets, conditions, linked_drawing_ids,
                    tags, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    'trade_setup', ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?
                )
            ''') + returning_id()

            ins_cur = conn.execute(sql, (
                user_id, asset, timeframe, name, thesis, thesis,
                direction, status, now,
                not is_archived, outcome, targets_json, conditions_json, linked_json,
                tags_val, now, now,
            ))
            new_id = get_last_id(ins_cur, conn)
            migrated += 1
            print(f"  OK: '{name}' ({asset}/{timeframe}) -> scenario #{new_id}, "
                  f"{len(linked_ids)} linked drawings, status={status}")

        print(f"\nMigration complete: {migrated} migrated, {skipped} skipped.")


if __name__ == '__main__':
    import argparse
    _parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _parser.add_argument('--dry-run', action='store_true',
                         help='report what would be migrated without writing')
    dry_run = _parser.parse_args().dry_run
    if dry_run:
        print("=== DRY RUN MODE ===\n")
    migrate(dry_run=dry_run)
