"""Telegram bot token at-rest encryption (no-op schema migration).

Tokens are stored inside the user_preferences JSONB column, not in a
dedicated column, so no schema change is required. This migration serves as
an audit-trail marker and a deployment gate:

  * New writes will produce "fernet:<ciphertext>" values once
    TELEGRAM_TOKEN_ENCRYPTION_KEY is set in the environment.
  * Existing plaintext tokens continue to work via the legacy-fallback path
    in carrac/secret_box.py (decrypt_token returns the raw
    string if it lacks the "fernet:" prefix).
  * Tokens are re-encrypted on the user's next settings save.

OPERATOR ACTION REQUIRED:
  Set TELEGRAM_TOKEN_ENCRYPTION_KEY in the container environment:
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  Store the 44-character key in .env / secrets manager before deploying this
  release. Without it the module falls back to plaintext (existing behaviour)
  and logs a WARNING at startup.

Revision ID: p6q7r8s9t0u1
Revises: o5p6q7r8s9t0
Create Date: 2026-04-20
"""

from alembic import op  # noqa: F401 — imported for consistency with migration style


revision = 'p6q7r8s9t0u1'
down_revision = 'o5p6q7r8s9t0'
branch_labels = None
depends_on = None


def upgrade():
    # No DDL change needed — encryption is handled at the application layer.
    # This revision exists as a deployment marker so operators know the
    # TELEGRAM_TOKEN_ENCRYPTION_KEY env var must be set before this runs.
    pass


def downgrade():
    # Reversing encryption at the DB level is not possible here; rolling back
    # means deploying the previous application code (which ignores the prefix).
    pass
