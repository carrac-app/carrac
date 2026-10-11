"""legal/marketing pages must not name data sources Carrac
does not run. indicators.html is exempt (describes a user-screenshot
feature). Cadence claims must match the real dispatch (06:00 UTC cron)."""
from pathlib import Path
import pytest

PAGES = ["terms.html", "privacy.html", "disclaimer.html",
         "landing_v5.html", "intelligence.html"]

@pytest.mark.parametrize("page", PAGES)
def test_no_coinglass_claim(page):
    assert "coinglass" not in Path(f"templates/{page}").read_text().lower()

def test_no_template_hardcodes_an_mcp_tool_count():
    """Tool counts must be derived, never typed in.

    They had drifted to three different wrong numbers simultaneously — /workflow
    and the 404 nav said "33 tools", /llms.txt said 76, /integrations and the
    landing pages said "100+" — while the registry held something else again.
    Every count now comes from ``mcp_tool_count`` (a context processor reading
    the live registry), so adding or consolidating a tool cannot strand a stale
    figure on a marketing page.
    """
    import re
    stale = []
    # "NN tools" / "NN+ tools" / "NN MCP tools" anywhere in a template.
    # Threshold 20: guide.html legitimately says things like "6 tools" about a
    # single tool family, and those are not claims about the registry size.
    pattern = re.compile(r'\b(\d+)\+?\s+(?:MCP\s+)?(?:Carrac\s+)?tools\b', re.I)
    for f in Path("templates").rglob("*.html"):
        for hit in pattern.finditer(f.read_text(encoding="utf-8")):
            if int(hit.group(1)) >= 20:
                stale.append(f"{f}: {hit.group(0)!r}")
    assert stale == [], (
        "Hardcoded MCP tool counts found — use {{ mcp_tool_count }} instead:\n"
        + "\n".join(stale)
    )


def test_llms_txt_tool_counts_match_the_registry():
    """/llms.txt is the client-facing entry point — its counts must be real."""
    import carrac.mcp.tools  # noqa: F401 — triggers registration
    from carrac.mcp.registry import TOOL_REGISTRY
    from carrac.fastapi_app.routers.migrated import llms_txt

    body = llms_txt().body.decode()
    total = len(TOOL_REGISTRY)
    free = sum(1 for m in TOOL_REGISTRY.values() if m.get('min_tier') == 'free')
    assert f'{total} tools; {free}' in body
    assert '{_n_tools}' not in body  # placeholder actually substituted


def test_integrations_mcp_snippet_uses_x_api_key():
    """/integrations is the first MCP setup code a prospective
    integrator copies — before login, before Settings. Its config block must
    show X-API-Key, matching settings.html's 11 client accordions and
    guide.html. Bearer is accepted server-side and may be *mentioned* as an
    alternative elsewhere, but must not be the header in this snippet.
    """
    src = Path("templates/integrations.html").read_text()
    assert "X-API-Key" in src
    assert "Bearer" not in src


def test_no_stale_445_cadence_anywhere():
    # The dispatch cron fires 06:00 UTC; no template may promise 4:45.
    stale = [str(f) for f in Path("templates").rglob("*.html")
             if "4:45" in f.read_text()]
    assert stale == []


# ── Deribit must not reach any public surface ──────────────────
#
# Deribit's ToS bars publishing or processing its market/derived data without
# written approval. The desk was switched off and facts/bands.py stopped
# publishing the columns, but MCP, the formula engine and eleven marketing
# templates were still redistributing or advertising it. These tests are the
# thing that keeps it gone.

_DERIBIT_TEMPLATE_EXEMPT = {
    # None today. Add here ONLY with written approval on file, and say where.
}


def test_no_template_names_deribit():
    """Marketing, legal and app pages must not advertise Deribit as a source."""
    offenders = []
    for f in Path("templates").rglob("*.html"):
        if f.name in _DERIBIT_TEMPLATE_EXEMPT:
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "deribit" in line.lower():
                offenders.append(f"{f}:{i}: {line.strip()[:90]}")
    assert offenders == [], (
        "Deribit named on a public page:\n" + "\n".join(offenders)
    )


def test_mcp_tool_descriptions_do_not_advertise_deribit_fields():
    """The tool catalogue is public — descriptions are marketing copy.

    quant_get_regime must not list dvol/skew_25d/gex_net/gex_flip_price in its
    description: the field names would be discoverable by any MCP client even
    when the values are gated.
    """
    import carrac.mcp.tools  # noqa: F401 — triggers registration
    from carrac.mcp.registry import TOOL_REGISTRY
    from carrac.compliance.deribit_gate import DERIBIT_COLUMNS

    offenders = []
    for name, meta in TOOL_REGISTRY.items():
        blob = f"{meta.get('description', '')} {meta.get('inputSchema', '')}".lower()
        if "deribit" in blob:
            offenders.append(f"{name}: names Deribit")
        for col in DERIBIT_COLUMNS:
            if col in blob:
                offenders.append(f"{name}: advertises column {col!r}")
    assert offenders == [], (
        "MCP tool catalogue advertises Deribit-derived data:\n"
        + "\n".join(sorted(set(offenders)))
    )


def test_the_gate_fails_closed_with_no_caller():
    """No request context, no MCP caller → not the operator."""
    from carrac.compliance.deribit_gate import deribit_visible
    assert deribit_visible() is False


def test_gate_strips_deribit_but_keeps_permitted_columns():
    from carrac.compliance.deribit_gate import (
        PERMITTED_DERIVATIVES_COLUMNS, strip_deribit_columns)

    row = {"asset": "BTC", "ts": "2026-08-05", "dvol": 55.2, "gex_net": 1e9,
           "skew_25d": -3.1, "funding_rate_avg": 0.01, "oi_usd": 123.0,
           "long_short_ratio": 1.2}

    out = strip_deribit_columns(row)

    assert "dvol" not in out and "gex_net" not in out and "skew_25d" not in out
    for permitted in PERMITTED_DERIVATIVES_COLUMNS & set(row):
        assert permitted in out, f"{permitted} is licence-permitted, must survive"
    assert row["dvol"] == 55.2, "input must not be mutated"


def test_formula_vars_and_series_are_filtered_for_non_admin():
    from carrac.compliance.deribit_gate import (
        allowed_formula_vars, allowed_series_keys, is_gated_series)
    from carrac.formula_eval import DERIV_VARS, QUANT_VARS

    visible = allowed_formula_vars({**QUANT_VARS, **DERIV_VARS})
    assert "dvol" not in visible and "gex_net" not in visible
    assert "funding" in visible, "Binance funding is licence-permitted"
    # funding_oi_ratio was removed outright — it named a column
    # nothing ever wrote, so "permitted" was a ruling on a phantom.
    assert "funding_oi_ratio" not in visible

    assert "DVOL" not in allowed_series_keys(["FUNDING", "DVOL", "BTC"])
    assert is_gated_series("DVOL") and not is_gated_series("FUNDING")
