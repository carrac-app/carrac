"""Alembic environment configuration.

Reads DATABASE_URL from environment. Skips if DB_TYPE=sqlite.
No SQLAlchemy models — all migrations are hand-written SQL.
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No SQLAlchemy models — manual migrations only
target_metadata = None


def get_url():
    """Get DATABASE_URL from environment or .env file."""
    url = os.getenv('DATABASE_URL', '')
    if not url:
        # Try loading from .env file
        env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'
        )
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('DATABASE_URL='):
                        url = line.split('=', 1)[1].strip().strip('"').strip("'")
                        break
    if not url:
        raise RuntimeError(
            "DATABASE_URL not set. Export it or add to .env file.\n"
            "Example: DATABASE_URL=postgresql://carrac:pass@localhost:5432/carrac"
        )
    return url


def run_migrations_offline():
    """Run migrations in 'offline' mode — generates SQL without connecting."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations against a live database connection."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
