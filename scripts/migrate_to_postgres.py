#!/usr/bin/env python3
"""
Migrate SQLite data to PostgreSQL.

Reads from users.db and drawings.db, inserts into PostgreSQL in dependency order.
Resets sequences to max(id)+1 after migration.

Usage:
    # From project root with .env loaded:
    python scripts/migrate_to_postgres.py

    # Or specify paths:
    DATABASE_URL=postgresql://... python scripts/migrate_to_postgres.py --users-db data/users.db --drawings-db data/drawings.db
"""

import argparse
import os
import sqlite3
import sys

import psycopg2
import psycopg2.extras


def get_sqlite_tables(conn):
    """List all tables in SQLite database."""
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [row[0] for row in cur.fetchall()]


def get_table_columns(conn, table):
    """Get column names for a table."""
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def migrate_table(sqlite_conn, pg_conn, table, columns=None):
    """Migrate a single table from SQLite to PostgreSQL."""
    if columns is None:
        columns = get_table_columns(sqlite_conn, table)

    # Check if table exists in PostgreSQL
    pg_cur = pg_conn.cursor()
    pg_cur.execute(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name=%s)",
        (table,)
    )
    if not pg_cur.fetchone()[0]:
        print(f"  SKIP {table} — not in PostgreSQL schema")
        return 0

    # Get PostgreSQL columns to only migrate matching ones
    pg_cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=%s",
        (table,)
    )
    pg_columns = {row[0] for row in pg_cur.fetchall()}
    common_columns = [c for c in columns if c in pg_columns]

    if not common_columns:
        print(f"  SKIP {table} — no matching columns")
        return 0

    # Read from SQLite
    col_list = ', '.join(common_columns)
    sqlite_cur = sqlite_conn.execute(f"SELECT {col_list} FROM {table}")
    rows = sqlite_cur.fetchall()

    if not rows:
        print(f"  SKIP {table} — empty")
        return 0

    # Insert into PostgreSQL
    placeholders = ', '.join(['%s'] * len(common_columns))
    insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"

    count = 0
    for row in rows:
        try:
            pg_cur.execute(insert_sql, tuple(row))
            count += 1
        except Exception as e:
            print(f"  WARN: {table} row error: {e}")
            pg_conn.rollback()
            continue

    pg_conn.commit()
    print(f"  OK   {table}: {count}/{len(rows)} rows migrated")
    return count


def reset_sequences(pg_conn):
    """Reset all SERIAL sequences to max(id)+1."""
    tables_with_serial = [
        'users', 'invites', 'user_settings', 'ai_usage', 'feedback',
        'onboarding', 'scenarios', 'email_verifications', 'password_reset_tokens',
        'widget_tokens', 'checklist_templates',
        'drawings', 'reports', 'sentiment_history', 'regime_history',
        'heatmap_history', 'pattern_log', 'trade_journal',
        'journal_sessions', 'journal_screenshots', 'setup_checklists',
        'journal_views', 'journal_playbooks', 'monthly_goals',
    ]

    pg_cur = pg_conn.cursor()
    for table in tables_with_serial:
        try:
            pg_cur.execute(f"SELECT MAX(id) FROM {table}")
            max_id = pg_cur.fetchone()[0]
            if max_id is not None:
                seq_name = f"{table}_id_seq"
                pg_cur.execute(f"SELECT setval('{seq_name}', {max_id + 1}, false)")
                print(f"  SEQ  {table}: reset to {max_id + 1}")
        except Exception as e:
            print(f"  WARN: {table} sequence reset failed: {e}")
            pg_conn.rollback()
            continue
    pg_conn.commit()


def main():
    parser = argparse.ArgumentParser(description='Migrate SQLite to PostgreSQL')
    parser.add_argument('--users-db', default='data/users.db', help='Path to users.db')
    parser.add_argument('--drawings-db', default='data/drawings.db', help='Path to drawings.db')
    parser.add_argument('--database-url', default=os.getenv('DATABASE_URL', ''),
                        help='PostgreSQL connection URL')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be migrated')
    args = parser.parse_args()

    if not args.database_url:
        print("ERROR: DATABASE_URL not set. Use --database-url or set env var.")
        sys.exit(1)

    # Connect to PostgreSQL
    print("Connecting to PostgreSQL...")
    pg_conn = psycopg2.connect(args.database_url)
    pg_conn.autocommit = False

    # Migration order (users first, then FKs)
    print(f"\n=== Migrating users.db ({args.users_db}) ===")
    if os.path.exists(args.users_db):
        users_conn = sqlite3.connect(args.users_db)
        users_conn.row_factory = sqlite3.Row

        user_tables = [
            'users',           # First — other tables reference this
            'invites',
            'user_settings',
            'ai_usage',
            'feedback',
            'onboarding',
            'scenarios',
            'email_verifications',
            'password_reset_tokens',
            'widget_tokens',
            'checklist_templates',
        ]

        for table in user_tables:
            try:
                migrate_table(users_conn, pg_conn, table)
            except Exception as e:
                print(f"  ERR  {table}: {e}")

        users_conn.close()
    else:
        print(f"  SKIP — {args.users_db} not found")

    print(f"\n=== Migrating drawings.db ({args.drawings_db}) ===")
    if os.path.exists(args.drawings_db):
        drawings_conn = sqlite3.connect(args.drawings_db)
        drawings_conn.row_factory = sqlite3.Row

        drawing_tables = [
            'drawings',
            'reports',
            'sentiment_history',
            'regime_history',
            'options_history',
            'heatmap_history',
            'pattern_log',
            'trade_journal',
            'journal_sessions',
            'journal_screenshots',
            'setup_checklists',
            'journal_views',
            'journal_playbooks',
            'monthly_goals',
        ]

        for table in drawing_tables:
            try:
                migrate_table(drawings_conn, pg_conn, table)
            except Exception as e:
                print(f"  ERR  {table}: {e}")

        drawings_conn.close()
    else:
        print(f"  SKIP — {args.drawings_db} not found")

    # Reset sequences
    print("\n=== Resetting sequences ===")
    reset_sequences(pg_conn)

    # Verification
    print("\n=== Verification ===")
    pg_cur = pg_conn.cursor()
    for table in ['users', 'drawings', 'trade_journal', 'sentiment_history']:
        try:
            pg_cur.execute(f"SELECT COUNT(*) FROM {table}")
            count = pg_cur.fetchone()[0]
            print(f"  {table}: {count} rows")
        except Exception:
            pass

    pg_conn.close()
    print("\nMigration complete!")


if __name__ == '__main__':
    main()
