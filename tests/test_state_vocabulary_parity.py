"""Guard: frontend-offered indicator states vs. what
the backend evaluators can actually produce.

Two independent vocabularies are checked, each sourced from BOTH sides
(frontend JS parsed from disk, backend Python imported directly):

  1. Alerts — static/js/alert-panel.js `_indicatorTargetStates` (what the
     "create indicator alert" dropdown offers) vs.
     carrac/indicator_state.py `INDICATOR_ALERT_CONDITIONS` (what
     check_indicator_alerts() will actually match against).

  2. Plays — the condition-builder's indicator_state picker, which is built
     at runtime by static/js/condition-builder.js `_buildStateOptions()` from
     two sources (the registry's per-indicator `states:` arrays in
     static/js/indicator-registry.js, plus `_CONDITION_ALIAS_STATES`) vs.
     carrac/conditions.py `_get_indicator_state()` (what a saved Play
     condition actually evaluates against).

This bug class (frontend offers a state/indicator the backend evaluator has
no vocabulary for, or vice versa) blocks features outright — hence a
standalone parity suite rather than relying on the two sides happening to be
edited together.

Submit-time encoding matters as well as the offered list: a boolean coercion
in _createIndicatorAlert() that encoded every bb_squeeze selection as False
would make those alerts impossible to create from the UI. See
test_bb_squeeze_submit_encoding_reaches_backend_as_valid_state() below. The
*vocabulary* comparison (family 1) does not catch this class — both sides
agree on ['squeezed', 'normal', 'expanded'] — because the problem would be in
the submit-time value encoding, not the offered state list.
"""

import re
from pathlib import Path

import pytest

from carrac.conditions import _get_indicator_state
from carrac.indicator_state import INDICATOR_ALERT_CONDITIONS
from carrac.mcp.indicator_registry_parser import get_all_indicators
from tests.test_conditions import MOCK_ASSET_DATA

REPO_ROOT = Path(__file__).resolve().parents[1]
ALERT_PANEL_JS = REPO_ROOT / 'static' / 'js' / 'alert-panel.js'
CONDITION_BUILDER_JS = REPO_ROOT / 'static' / 'js' / 'condition-builder.js'
INDICATOR_REGISTRY_JS = REPO_ROOT / 'static' / 'js' / 'indicator-registry.js'


# ---------------------------------------------------------------------------
# Minimal, robust JS object-literal parsing.
#
# A single non-greedy regex over an entire `var X = {...}` block is fragile
# (breaks the moment someone nests a `{` inside a comment or value). Brace
# counting is immune to that, at the cost of assuming `marker` names a spot
# in the text immediately preceding the object's opening '{'.
# ---------------------------------------------------------------------------

def _extract_braced_block(text: str, marker: str) -> str:
    """Return the text strictly between the braces of the first `{...}`
    block found after `marker`. Raises AssertionError (loud, not a silent
    empty result) if `marker` isn't found or the braces never balance."""
    assert marker in text, f"Could not find {marker!r} in source — file format changed?"
    idx = text.index(marker)
    brace_start = text.index('{', idx)
    depth = 0
    i = brace_start
    while i < len(text):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return text[brace_start + 1:i]
        i += 1
    raise AssertionError(f"Unbalanced braces extracting block for marker {marker!r}")


_STR_ITEM_RE = re.compile(r"'([^']*)'")

# Matches one `key: value,` entry per line inside an object-literal body,
# where value is either a bracketed string-array (`['a', 'b']`) or a bare
# identifier reference (`_CPR_ZONES`) to another module-level array. Lines
# that don't match this shape (comments, multi-line values) are silently
# skipped, which is exactly what we want for the comment-heavy bodies here.
_KV_LINE_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(\[[^\]]*\]|[A-Za-z_][A-Za-z0-9_]*)\s*,?\s*$",
    re.MULTILINE,
)


def _parse_key_to_string_list(body: str, resolve: dict | None = None) -> dict:
    """Parse an object-literal body into {key: [str, ...]}.

    `resolve` maps bare-identifier values (e.g. `_CPR_ZONES`) to their
    already-parsed array, for the (rare) case a value is a variable
    reference rather than an inline `[...]` literal.
    """
    resolve = resolve or {}
    out = {}
    for m in _KV_LINE_RE.finditer(body):
        key, value = m.group(1), m.group(2)
        if value.startswith('['):
            out[key] = _STR_ITEM_RE.findall(value)
        elif value in resolve:
            out[key] = list(resolve[value])
        # else: unresolvable bare identifier — skip rather than guess. If this
        # ever fires for a real entry, the downstream minimum-count assertions
        # will catch the shrinkage.
    return out


def _extract_var_array(text: str, var_name: str) -> list:
    m = re.search(rf"var {re.escape(var_name)}\s*=\s*\[(.*?)\];", text, re.DOTALL)
    assert m, f"Could not find `var {var_name} = [...]` — file format changed?"
    return _STR_ITEM_RE.findall(m.group(1))


# ---------------------------------------------------------------------------
# Family 1 — Alerts: parse static/js/alert-panel.js `_indicatorTargetStates`
# ---------------------------------------------------------------------------

_alert_js_text = ALERT_PANEL_JS.read_text(encoding='utf-8')
_cpr_zones = _extract_var_array(_alert_js_text, '_CPR_ZONES')
_alert_body = _extract_braced_block(_alert_js_text, 'var _indicatorTargetStates =')
ALERT_TARGET_STATES = _parse_key_to_string_list(_alert_body, resolve={'_CPR_ZONES': _cpr_zones})

# Fail loudly at collection time if the parse came back empty/tiny — never
# let an empty parse silently "pass" every downstream assertion.
assert len(ALERT_TARGET_STATES) >= 10, (
    f"Only parsed {len(ALERT_TARGET_STATES)} keys from alert-panel.js's "
    f"_indicatorTargetStates — expected >= 10 (actual count at last audit: "
    f"20). The JS parser likely broke silently; inspect ALERT_TARGET_STATES."
)

# The 7 Tier-1 sidebar-only signals the Alerts UI
# deliberately does not expose a dropdown for, even though the backend can
# track & fire on them. A NEW backend-only key not in this set trips
# test_backend_only_keys_are_exactly_the_documented_allow_list below —
# add it here only after confirming the omission from the UI is deliberate.
ALLOWED_BACKEND_ONLY_ALERT_KEYS = {
    'ema_gap_squeeze', 'v_cpr', 'cpr_extreme', 'structure_disagreement',
    'tf_divergence', 'rsi_divergence', 'perp_basis',
}


class TestAlertsExactMatch:
    """Frontend `_indicatorTargetStates` vs. backend `INDICATOR_ALERT_CONDITIONS`.

    Coverage rule: frontend keys must be a SUBSET of backend keys (key
    coverage), but every key present on BOTH sides must have an EXACTLY
    matching state list (order-insensitive).
    """

    def test_frontend_alert_keys_are_subset_of_backend(self):
        missing = set(ALERT_TARGET_STATES) - set(INDICATOR_ALERT_CONDITIONS)
        assert not missing, (
            f"alert-panel.js offers indicator alert(s) with no matching "
            f"backend vocabulary in INDICATOR_ALERT_CONDITIONS: {sorted(missing)}"
        )

    def test_backend_only_keys_are_exactly_the_documented_allow_list(self):
        backend_only = set(INDICATOR_ALERT_CONDITIONS) - set(ALERT_TARGET_STATES)
        assert backend_only == ALLOWED_BACKEND_ONLY_ALERT_KEYS, (
            "The set of backend-only alert indicators changed since "
            "ALLOWED_BACKEND_ONLY_ALERT_KEYS was written. If a NEW backend "
            "key is deliberately not exposed in the Alerts UI, add it to "
            "ALLOWED_BACKEND_ONLY_ALERT_KEYS with a comment explaining why; "
            "otherwise alert-panel.js._indicatorTargetStates is missing a "
            "key it should offer.\n"
            f"  now backend-only:      {sorted(backend_only)}\n"
            f"  documented allow-list: {sorted(ALLOWED_BACKEND_ONLY_ALERT_KEYS)}"
        )

    @pytest.mark.parametrize(
        'indicator',
        sorted(set(ALERT_TARGET_STATES) & set(INDICATOR_ALERT_CONDITIONS)),
    )
    def test_state_lists_match_exactly(self, indicator):
        """Note: bb_squeeze's *vocabulary* matches on both sides (this test
        passes for it) — but see test_bb_squeeze_submit_encoding_reaches_
        backend_as_valid_state() below for a real bug the vocabulary
        comparison alone cannot catch."""
        frontend_states = sorted(ALERT_TARGET_STATES[indicator])
        backend_states = sorted(INDICATOR_ALERT_CONDITIONS[indicator])
        assert frontend_states == backend_states, (
            f"{indicator}: frontend offers {frontend_states} but backend "
            f"INDICATOR_ALERT_CONDITIONS accepts {backend_states}"
        )


def _encode_bb_squeeze_target_state(selected_ui_state: str) -> str:
    """Mirror `_createIndicatorAlert()`'s submit encoding for bb_squeeze.

    bb_squeeze submits the selected state verbatim like every other
    indicator. A boolean coercion (`ts = targetState === 'true'`, valid only
    for a ['true', 'false'] vocabulary) would make every bb_squeeze alert
    submit `False` → 400.
    """
    return selected_ui_state


def test_bb_squeeze_submit_encoding_reaches_backend_as_valid_state():
    """Whichever bb_squeeze state the dropdown offers, the value actually
    PUT on the wire by _createIndicatorAlert() must be one the backend
    accepts — and the legacy boolean coercion must never come back."""
    # Source-level guard: the deleted coercion (or any bb_squeeze
    # special-case in the submit path) reappearing means a human must
    # re-verify the wire encoding against INDICATOR_ALERT_CONDITIONS.
    alert_panel_src = ALERT_PANEL_JS.read_text(encoding='utf-8')
    assert "targetState === 'true'" not in alert_panel_src, (
        "the legacy bb_squeeze boolean coercion is back in alert-panel.js — "
        "it encodes every submitted target_state as False, which the backend "
        "rejects; submit the selected state verbatim instead"
    )
    offered_states = ALERT_TARGET_STATES['bb_squeeze']
    backend_states = INDICATOR_ALERT_CONDITIONS['bb_squeeze']
    for state in offered_states:
        encoded = _encode_bb_squeeze_target_state(state)
        assert encoded in backend_states, (
            f"bb_squeeze target_state {state!r} encodes to {encoded!r} at "
            f"submit time, which is not a member of the backend's accepted "
            f"vocabulary {backend_states!r} — this alert can never fire."
        )


# ---------------------------------------------------------------------------
# Family 2 — Plays: mirror condition-builder.js `_buildStateOptions()`
# ---------------------------------------------------------------------------

_cb_js_text = CONDITION_BUILDER_JS.read_text(encoding='utf-8')
_alias_body = _extract_braced_block(_cb_js_text, 'var _CONDITION_ALIAS_STATES =')
CONDITION_ALIAS_STATES = _parse_key_to_string_list(_alias_body)

_registry_js_text = INDICATOR_REGISTRY_JS.read_text(encoding='utf-8')
_catkeys_body = _extract_braced_block(_registry_js_text, 'var CATEGORY_KEYS =')
_CATEGORY_KEYS = _parse_key_to_string_list(_catkeys_body)
_ALL_CATEGORIZED_KEYS = set()
for _keys in _CATEGORY_KEYS.values():
    _ALL_CATEGORIZED_KEYS.update(_keys)

_ALL_INDICATORS = get_all_indicators()

# Sanity: every registry indicator should belong to exactly one category
# bucket (i.e. getAllKeys() should enumerate the full registry) — if this
# ever drifts, condition-builder.js's STATE_OPTIONS would silently drop an
# indicator's states without either side "changing" in an obvious way.
assert _ALL_CATEGORIZED_KEYS == set(_ALL_INDICATORS), (
    "indicator-registry.js CATEGORY_KEYS no longer enumerates exactly the "
    "same key set as the INDICATORS object — getAllKeys() (used by "
    "condition-builder.js _buildStateOptions()) would silently omit or "
    "include the wrong indicators.\n"
    f"  in INDICATORS but not CATEGORY_KEYS: "
    f"{sorted(set(_ALL_INDICATORS) - _ALL_CATEGORIZED_KEYS)}\n"
    f"  in CATEGORY_KEYS but not INDICATORS: "
    f"{sorted(_ALL_CATEGORIZED_KEYS - set(_ALL_INDICATORS))}"
)

# Registry-sourced half of STATE_OPTIONS: reg.getStates(k) for every key
# getAllKeys() enumerates (mirrors _buildStateOptions()'s first loop).
REGISTRY_PLAY_STATES = {
    key: list(entry['states'])
    for key, entry in _ALL_INDICATORS.items()
    if key in _ALL_CATEGORIZED_KEYS and entry.get('states')
}

assert len(REGISTRY_PLAY_STATES) >= 10, (
    f"Only {len(REGISTRY_PLAY_STATES)} registry indicators carry a `states:` "
    f"array — expected >= 10 (actual count at last audit: 15). The "
    f"indicator-registry.js parser likely broke silently."
)

# Full offered set: registry states, then alias states for any key NOT
# already registry-sourced — mirrors _buildStateOptions()'s
# "never overwrite a registry-sourced entry" merge rule exactly.
PLAY_STATE_OPTIONS = dict(REGISTRY_PLAY_STATES)
for _key, _states in CONDITION_ALIAS_STATES.items():
    if _key not in PLAY_STATE_OPTIONS:
        PLAY_STATE_OPTIONS[_key] = list(_states)

_TOTAL_PLAY_PAIRS = sum(len(v) for v in PLAY_STATE_OPTIONS.values())
assert len(PLAY_STATE_OPTIONS) >= 15, (
    f"Only {len(PLAY_STATE_OPTIONS)} indicators offer indicator_state options "
    f"in the condition builder — expected >= 15 (actual count at last audit: "
    f"21)."
)
assert _TOTAL_PLAY_PAIRS >= 60, (
    f"Only {_TOTAL_PLAY_PAIRS} total (indicator, state) pairs are offered — "
    f"expected >= 60 (actual count at last audit: 84). Parser likely "
    f"broke silently."
)


# ---------------------------------------------------------------------------
# Fixture map: for every offered (indicator, state) pair, an asset_data dict
# that makes carrac.conditions._get_indicator_state(indicator, asset_data)
# return exactly that state. Built by reusing MOCK_ASSET_DATA (imported from
# tests/test_conditions.py) as a base and overriding only what each state
# needs — mirroring the per-state fixtures already exercised in
# tests/test_conditions.py::TestIndicatorState /
# tests/test_indicator_eval_parity.py, rather than duplicating them blind.
# ---------------------------------------------------------------------------

def _with_cockpit(sub_key, sub_value):
    return {
        **MOCK_ASSET_DATA,
        'cockpit_extended': {**MOCK_ASSET_DATA['cockpit_extended'], sub_key: sub_value},
    }


def _cpr_asset(data_key, position, level):
    return {**MOCK_ASSET_DATA, data_key: {'price_position': position, 'price_level': level}}


# The 9-state CPR vocabulary is hierarchical when MATCHING a
# saved Play condition (_eval_indicator_state uses cpr_state_satisfies() —
# e.g. a live 'above_R2' also satisfies a saved 'above_pivot'). Producibility
# is a different question: we call _get_indicator_state() directly, which
# for the CPR family just returns indicator_eval.cpr_position()'s raw output
# unmodified — so the target here is the exact state string, not a
# containment check. One (price_position, price_level) pair per state,
# exactly as CPRCalculator would produce them together (see
# tests/test_indicator_eval_parity.py::TestCprPositionNineState).
_CPR_STATE_INPUTS = [
    ('above_pivot', 'above', 'TC-R1'),
    ('below_pivot', 'below', 'BC-S1'),
    ('between', 'inside', 'BC-CP'),
    ('above_R1', 'above', 'R1-R2'),
    ('above_R2', 'above', 'R2-R3'),
    ('above_R3', 'above', 'Above R3'),
    ('below_S1', 'below', 'S1-S2'),
    ('below_S2', 'below', 'S2-S3'),
    ('below_S3', 'below', 'Below S3'),
]


def _cpr_fixture_family(data_key):
    return {state: _cpr_asset(data_key, pos, level) for state, pos, level in _CPR_STATE_INPUTS}


# MKT_STRUCTURE and STRUCTURE both map to indicator_eval.structure_pattern()
# via the same `if ind in ('STRUCTURE', 'MKT_STRUCTURE')` branch in
# _get_indicator_state() — one fixture family covers both offered keys.
_STRUCTURE_FIXTURES = {
    'HH_HL': {**MOCK_ASSET_DATA, 'structure': {'structure_trend': 'bullish'}},
    'LH_LL': {**MOCK_ASSET_DATA, 'structure': {'structure_trend': 'bearish'}},
    'range': {**MOCK_ASSET_DATA, 'structure': {'structure_trend': 'neutral'}},
}

PLAY_STATE_FIXTURES = {
    'RSI': {
        'oversold': {**MOCK_ASSET_DATA, 'weekly_rsi': 25},
        'overbought': {**MOCK_ASSET_DATA, 'weekly_rsi': 78},
        'neutral': {**MOCK_ASSET_DATA, 'weekly_rsi': 50},
    },
    'MACD': {
        'bullish': {**MOCK_ASSET_DATA, 'macd': {'histogram': 0.5}},
        'bearish': {**MOCK_ASSET_DATA, 'macd': {'histogram': -0.5}},
        'flat': {**MOCK_ASSET_DATA, 'macd': {'histogram': 0}},
    },
    'TTM_SQUEEZE': {
        'squeeze_on': _with_cockpit('ttm_squeeze', {'active': True}),
        'squeeze_off': _with_cockpit(
            'ttm_squeeze', {'active': False, 'momentum': 'neutral', 'bars_since_release': 2}
        ),
        'firing_up': _with_cockpit(
            'ttm_squeeze', {'active': False, 'momentum': 'bullish', 'bars_since_release': 2}
        ),
        'firing_down': _with_cockpit(
            'ttm_squeeze', {'active': False, 'momentum': 'bearish', 'bars_since_release': 2}
        ),
    },
    'EMA_RIBBON': {
        'bullish': {**MOCK_ASSET_DATA, 'ema_ribbon': {'trend': 'bullish'}},
        'bearish': {**MOCK_ASSET_DATA, 'ema_ribbon': {'trend': 'bearish'}},
        'neutral': {**MOCK_ASSET_DATA, 'ema_ribbon': {'trend': 'choppy'}},
    },
    'CONSENSIO': {
        'full_bull': {**MOCK_ASSET_DATA, 'consensio': {'total': {'positions': 9, 'max_positions': 9}}},
        'strong_bull': {**MOCK_ASSET_DATA, 'consensio': {'total': {'positions': 7, 'max_positions': 9}}},
        'bull': {**MOCK_ASSET_DATA, 'consensio': {'total': {'positions': 5, 'max_positions': 9}}},
        'neutral': {**MOCK_ASSET_DATA, 'consensio': {'total': {'positions': 5, 'max_positions': 10}}},
        'bear': {**MOCK_ASSET_DATA, 'consensio': {'total': {'positions': 3, 'max_positions': 9}}},
        'strong_bear': {**MOCK_ASSET_DATA, 'consensio': {'total': {'positions': 0, 'max_positions': 9}}},
    },
    'ALLIGATOR': {
        'eating_bullish': _with_cockpit('alligator', {'state': 'eating_bullish'}),
        'eating_bearish': _with_cockpit('alligator', {'state': 'eating_bearish'}),
        'sleeping': _with_cockpit('alligator', {'state': 'sleeping'}),
        'mixed': _with_cockpit('alligator', {'state': 'mixed'}),
    },
    # ICHIMOKU offers only the *cloud* states here (registry `states:` field)
    # — condition-builder.js's registry-states loop reads `ind.states`, never
    # `ind.statesTkCross`. The TK-cross vocabulary is offered as the
    # SEPARATE 'ICHIMOKU_TK' key below (from _CONDITION_ALIAS_STATES), which
    # is how condition-builder.js keeps Ichimoku's two vocabularies apart.
    'ICHIMOKU': {
        'above_cloud': _with_cockpit('ichimoku', {'cloud_position': 'above_green'}),
        'below_cloud': _with_cockpit('ichimoku', {'cloud_position': 'below_red'}),
        'inside_cloud': _with_cockpit('ichimoku', {'cloud_position': 'inside'}),
    },
    'DONCHIAN': {
        # MOCK_ASSET_DATA price (realtime.price) is 3420.0.
        'at_high': _with_cockpit('donchian', {'upper': 3420.0, 'lower': 3200.0}),
        'at_low': _with_cockpit('donchian', {'upper': 3600.0, 'lower': 3420.0}),
        'inside': _with_cockpit('donchian', {'upper': 3600.0, 'lower': 3200.0}),
    },
    'PSAR': {
        'bullish': _with_cockpit('psar', {'direction': 'long'}),
        'bearish': _with_cockpit('psar', {'direction': 'short'}),
    },
    'EMA_CROSS': {
        'bullish': _with_cockpit('ema_cross', {'state': 'bullish'}),
        'bearish': _with_cockpit('ema_cross', {'state': 'bearish'}),
    },
    'SUPER_TREND': {
        'bullish': _with_cockpit('supertrend', {'direction': 'long'}),
        'bearish': _with_cockpit('supertrend', {'direction': 'short'}),
    },
    'CPR': _cpr_fixture_family('cpr'),
    'WEEKLY_CPR': _cpr_fixture_family('weekly_cpr'),
    'DAILY_CPR': _cpr_fixture_family('daily_cpr'),
    'MKT_STRUCTURE': _STRUCTURE_FIXTURES,
    'STRUCTURE': _STRUCTURE_FIXTURES,
    'ADX': {
        'ranging': {**MOCK_ASSET_DATA, 'adx': {'regime': 'ranging'}},
        'trend_forming': {**MOCK_ASSET_DATA, 'adx': {'regime': 'trend_forming'}},
        'strong_trend': {**MOCK_ASSET_DATA, 'adx': {'regime': 'strong_trend'}},
    },
    'BB_POSITION': {
        'inside': {**MOCK_ASSET_DATA, 'bb': {**MOCK_ASSET_DATA['bb'], 'position': 'inside'}},
        'above_upper': {**MOCK_ASSET_DATA, 'bb': {**MOCK_ASSET_DATA['bb'], 'position': 'above_upper'}},
        'below_lower': {**MOCK_ASSET_DATA, 'bb': {**MOCK_ASSET_DATA['bb'], 'position': 'below_lower'}},
    },
    'BB_SQUEEZE': {
        'squeezed': {**MOCK_ASSET_DATA, 'bb': {**MOCK_ASSET_DATA['bb'], 'squeeze': True}},
        'normal': {**MOCK_ASSET_DATA, 'bb': {**MOCK_ASSET_DATA['bb'], 'squeeze': False, 'width_pct': 3.5}},
        'expanded': {**MOCK_ASSET_DATA, 'bb': {**MOCK_ASSET_DATA['bb'], 'squeeze': False, 'width_pct': 10}},
    },
    'VOLUME': {
        'high': {**MOCK_ASSET_DATA, 'volume_metrics': {'ratio': 3.0}},
        'above_avg': {**MOCK_ASSET_DATA, 'volume_metrics': {'ratio': 1.45}},
        'normal': {**MOCK_ASSET_DATA, 'volume_metrics': {'ratio': 1.0}},
        'low': {**MOCK_ASSET_DATA, 'volume_metrics': {'ratio': 0.3}},
    },
    'ICHIMOKU_TK': {
        'bullish': _with_cockpit('ichimoku', {'tk_cross': 'bullish'}),
        'bearish': _with_cockpit('ichimoku', {'tk_cross': 'bearish'}),
        'none': _with_cockpit('ichimoku', {'tk_cross': 'none'}),
    },
}


class TestPlaysProducibility:
    """Every (indicator, state) pair the condition-builder's indicator_state
    picker offers must be a state carrac.conditions._get_indicator_state()
    can actually return for some real asset_data — otherwise a user can save
    a Play condition that can never, ever be met."""

    def test_every_offered_pair_has_a_fixture_and_is_producible(self):
        missing = []
        mismatches = []
        for indicator in sorted(PLAY_STATE_OPTIONS):
            fixtures_for_indicator = PLAY_STATE_FIXTURES.get(indicator, {})
            for state in PLAY_STATE_OPTIONS[indicator]:
                if state not in fixtures_for_indicator:
                    missing.append((indicator, state))
                    continue
                asset_data = fixtures_for_indicator[state]
                actual = _get_indicator_state(indicator, asset_data)
                if actual != state:
                    mismatches.append((indicator, state, actual))

        if missing:
            lines = [
                f"  - state {state!r} for indicator {indicator!r} is offered in "
                f"the UI but has no producing fixture — either add a fixture "
                f"proving conditions.py can return it, or remove the option"
                for indicator, state in missing
            ]
            pytest.fail("Missing producibility fixtures:\n" + "\n".join(lines))

        if mismatches:
            lines = [
                f"  - {indicator}/{state}: fixture produced {actual!r}, not "
                f"the offered state {state!r}"
                for indicator, state, actual in mismatches
            ]
            pytest.fail(
                "Offered (indicator, state) pairs conditions.py cannot "
                "actually produce:\n" + "\n".join(lines)
            )

    def test_no_stale_fixture_indicators(self):
        """Guard the other direction: a fixture family for an indicator no
        longer offered anywhere would silently stop being exercised — better
        to know if PLAY_STATE_FIXTURES drifts ahead of PLAY_STATE_OPTIONS."""
        stale = set(PLAY_STATE_FIXTURES) - set(PLAY_STATE_OPTIONS)
        assert not stale, (
            f"PLAY_STATE_FIXTURES has fixture families for indicator(s) no "
            f"longer offered by the condition builder: {sorted(stale)}. "
            f"Remove them (or the offering is missing and this is the real bug)."
        )
