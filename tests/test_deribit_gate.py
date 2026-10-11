"""The surface walk deribit_gate.py has claimed since it was introduced.

That module's docstring has said, for months:

    Everything that could surface a Deribit-derived value imports from here,
    so the answer lives in exactly one place and a new surface has to opt in
    deliberately. ``tests/test_deribit_gate.py`` walks the public surfaces and
    fails if any of these names reappear.

This file did not exist. Not in the repo, not in any worktree. So the promise
that a new surface "has to opt in deliberately" was never enforced, and three
surfaces were added or left ungated behind it — the report pipeline
(a generation-gate sweep), the notification body and play-condition evidence,
and the AI shared context, which handed a paying PREMIUM customer "futures APR
+8.2%, skew -4.5%, GEX negative" because a comment read "Premium / admin only"
and the code believed the first half.

It is a REGISTRY walk rather than a grep for leaks: every
module that names a barred column must be listed here with the reason it is
safe. A new file that mentions one fails this test until somebody classifies
it, which is the "opt in deliberately" the docstring promised.
"""
import pathlib
import re

import pytest

from carrac.compliance.deribit_gate import DERIBIT_COLUMNS

#: Directories that may reference barred columns freely.
#: Collection is explicitly permitted — the rule is collect freely, expose to
#: admin only — and tests must be able to name what they are testing.
_EXEMPT_DIRS = ('tests/', '.venv/', 'node_modules/',
                'quant/collectors/', '__pycache__')

#: Every module that names a barred column, and WHY it is not a leak.
#: The value is documentation that a human has to write, which is the point:
#: adding a file here is the deliberate opt-in.
_SURFACE_REGISTRY = {
    'carrac/compliance/deribit_gate.py':
        'the gate itself — it must name what it bars',

    # ── definitions, not publication ──
    'quant/schema.py': 'DDL for the shared table',
    'quant/normalize.py': 'DOMAIN_QUERIES registry; the anomaly gate parses it',
    'quant/engine/config.py': 'CANONICAL_METRICS registry; deribit_leadlag_metrics() derives from it',
    'quant/engine/derivatives_regime.py': 'classifier internals; emits a label, and callers gate the label',
    'data_sources/binance.py': 'Binance perp basis (markPrice vs indexPrice) — PERMITTED, see the name-collision note below',

    # ── gated surfaces ──
    'quant/bridge.py': 'allowed_series_keys / is_gated_series',
    'quant/api/routes.py': '_gate_snapshot + _strip_gated_* on the way out',
    'carrac/formula_eval.py': 'allowed_formula_vars (formula variable gate)',
    'carrac/facts/bands.py': 'documented exclusion from published report facts',
    'carrac/mcp/tools/quant.py': 'deribit_visible / leadlag_metric_allowed',
    'carrac/mcp/tools/regime.py': 'strip_deribit_columns on regime output',
    'carrac/mcp/tools/reports.py': 'strip_deribit_columns + _caller_is_admin',
    'morning_engine/data_assembly.py':
        'fetch_options_data is gated whole; fetch_permitted_derivatives reads only licence-permitted columns',
    'carrac/conditions.py':
        'condition_metric_visible at validate AND evaluate, owner-scoped',
    'push_notifications.py':
        'condition_metric_visible on the alert evaluator; build_context_chip is per-recipient',
    'carrac/ai/context_builder.py':
        'shared-context derivatives block narrowed from premium to admin, cache key narrowed with it',

    # ── name collision, NOT Deribit data ──
    'carrac/indicator_eval.py':
        'perp_basis_state reads asset_data["perp_basis_pct"] from Binance markPrice/indexPrice — PERMITTED. '
        'The local name basis_pct collides with the Deribit column; different number, different source',
    'carrac/indicator_state.py':
        'delegates to indicator_eval.perp_basis_state — same Binance-sourced value, same collision',
}

_PATTERN = re.compile(r'\b(' + '|'.join(sorted(DERIBIT_COLUMNS)) + r')\b')


def _scan():
    """{path: [barred column names]} for every non-exempt Python file."""
    found = {}
    for path in pathlib.Path('.').rglob('*.py'):
        rel = str(path).replace('\\', '/')
        if rel.startswith('./'):
            rel = rel[2:]
        if any(part in rel for part in _EXEMPT_DIRS):
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        names = sorted(set(_PATTERN.findall(text)))
        if names:
            found[rel] = names
    return found


class TestEverySurfaceIsAccountedFor:
    def test_no_unregistered_module_names_a_barred_column(self):
        """The opt-in the docstring promised.

        A new file that mentions dvol/gex_net/skew_25d/basis_pct/... fails here
        until someone adds it to _SURFACE_REGISTRY with the reason it is safe.
        Writing that reason is the review.
        """
        unregistered = {
            path: cols for path, cols in _scan().items()
            if path not in _SURFACE_REGISTRY
        }
        assert not unregistered, (
            "these modules name a Deribit-barred column and are not in "
            "_SURFACE_REGISTRY — gate them, or register them with the reason "
            f"they are safe: {unregistered}")

    def test_the_registry_has_no_stale_entries(self):
        """A registry that outlives its subjects stops describing the system."""
        scanned = set(_scan())
        stale = sorted(set(_SURFACE_REGISTRY) - scanned)
        assert not stale, (
            f"registered but no longer references a barred column: {stale}")

    def test_the_scan_can_actually_see_a_leak(self):
        """An empty result is not a finding. Prove the pattern matches a known
        barred name before trusting any absence above."""
        assert _PATTERN.search("SELECT dvol, gex_net FROM quant.derivatives")
        assert _PATTERN.search("row['skew_25d']")
        assert not _PATTERN.search("SELECT funding_rate_avg, oi_usd")

    def test_every_registry_entry_carries_a_reason(self):
        for path, reason in _SURFACE_REGISTRY.items():
            assert reason and len(reason) > 20, \
                f"{path} is registered without a real reason"


class TestTheGatedSurfacesActuallyImportTheGate:
    """Registering a file says a human checked it. This says the gate is
    genuinely wired, so a registry entry cannot drift into a claim about code
    that no longer calls anything."""

    @pytest.mark.parametrize('path', [
        'quant/bridge.py',
        'quant/api/routes.py',
        'carrac/formula_eval.py',
        'carrac/mcp/tools/quant.py',
        'carrac/mcp/tools/regime.py',
        'carrac/mcp/tools/reports.py',
        'morning_engine/data_assembly.py',
        'carrac/conditions.py',
        'push_notifications.py',
    ])
    def test_it_references_the_compliance_module(self, path):
        src = pathlib.Path(path).read_text(encoding='utf-8')
        assert 'deribit_gate' in src, \
            f"{path} is registered as gated but never imports the gate"

    @pytest.mark.parametrize('tier,gets_derivatives', [
        (None, False), ('free', False), ('member', False),
        ('premium', False), ('admin', True),
    ])
    def test_the_ai_context_is_admin_only_not_premium(
            self, monkeypatch, tier, gets_derivatives):
        """Only the operator's AI context may carry the derivatives block. A
        paying PREMIUM customer must get the base context, and the two tiers
        must not share a cache entry."""
        from carrac.ai import context_builder

        requested = []

        def fake_build(include_premium=False):
            requested.append(include_premium)
            return 'derivatives' if include_premium else 'base'

        monkeypatch.setattr(context_builder, '_build_shared_context', fake_build)
        monkeypatch.setattr(context_builder, '_shared_context_base',
                            {'text': '', 'updated_at': 0})
        monkeypatch.setattr(context_builder, '_shared_context_premium',
                            {'text': '', 'updated_at': 0})

        # Warm the operator's cache first: a premium request must still not
        # read it.
        context_builder.get_shared_context('admin')
        text = context_builder.get_shared_context(tier)

        assert (text == 'derivatives') is gets_derivatives
        if not gets_derivatives:
            assert text == 'base'
            assert requested == [True, False]
