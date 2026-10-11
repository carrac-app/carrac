"""
Canonical tier constants — single source of truth for all tier-set definitions.

All frozensets here must be imported by the consumer modules rather than
re-defined locally.  When a tier is added or renamed, this file is the
only place that needs to change.

Tier vocabulary:
  free      — unauthenticated / no subscription
  member    — $34.99/mo paid tier
  tester    — admin-granted, mirrors member feature set with tester extras
  premium   — $69.00/mo paid tier (power-user headroom)
  admin     — full access, internal staff
"""

# ---------------------------------------------------------------------------
# Primary tier sets
# ---------------------------------------------------------------------------

# All tiers that require a paid subscription (member, tester, premium, admin).
# Used by @premium_required, require_premium, and auth gate checks.
PAID_TIERS: frozenset[str] = frozenset({"member", "tester", "premium", "admin"})

# Tiers at the tester level or above (tester, premium, admin).
# Used by @tester_or_key_required and the equivalent FastAPI dep.
TESTER_OR_HIGHER: frozenset[str] = frozenset({"tester", "premium", "admin"})

# Tiers that receive premium AI content (derivatives regime, etc.).
# Consumers reference this as _PREMIUM_TIERS; import it under that alias.
PREMIUM_TIERS: frozenset[str] = frozenset({"premium", "admin"})

# Tiers permitted to mint and use API keys.
# Open to every real tier for the free invite-only beta; unknown/disabled
# tiers are refused at the gate.
API_ALLOWED_TIERS: frozenset[str] = frozenset({"free", "member", "premium", "tester", "admin"})

# ---------------------------------------------------------------------------
# Admin-UI helper sets
# ---------------------------------------------------------------------------

# Complete set of all valid tier names.
ALL_TIERS: frozenset[str] = frozenset({"free", "member", "tester", "premium", "admin"})

# Tiers whose limits can be edited via the admin tier-config UI.
# Intentionally excludes "admin" — the admin tier is not configurable from
# the UI to prevent accidental self-lockout.
CONFIGURABLE_TIERS: frozenset[str] = frozenset({"free", "member", "tester", "premium"})

# Tiers that a non-admin user can be assigned to via the admin set-tier UI.
# Intentionally excludes "admin" — admin status is managed separately (is_admin flag).
ASSIGNABLE_TIERS: frozenset[str] = frozenset({"free", "member", "tester", "premium"})
