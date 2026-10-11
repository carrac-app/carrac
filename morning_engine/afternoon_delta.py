"""Afternoon delta builder — deterministic, pure, unit-tested.

build_delta(morning_ctx, current_ctx) -> dict
  Constructs the structured "what moved" ledger for the afternoon delta report.
  Never raises — missing or None inputs produce safe defaults.

Shape:
  {
    "regime": {"was": str, "now": str, "changed": bool},
    "fear_greed": {"was": int|None, "now": int|None, "dir": "up"|"down"|"flat"},
    "levels_tested": [{"asset": str, "level": float, "outcome": "held"|"reclaimed"|"lost"}],
    "signal_flips": [{"asset": str, "from": str, "to": str}],
    "new_prints": [{"event": str, "expected": str|None, "actual": str, "ts": str}],
  }
"""


def _dir(was, now):
    """Directional label for a numeric comparison; returns 'flat' on None."""
    if was is None or now is None:
        return "flat"
    try:
        return "up" if now > was else "down" if now < was else "flat"
    except TypeError:
        return "flat"


def build_delta(morning_ctx: dict, current_ctx: dict) -> dict:
    """Build the structured afternoon delta object.

    Parameters
    ----------
    morning_ctx : dict
        Context captured at morning-report time.  Expected keys: regime (str),
        fear_greed (int|None).  Missing keys are silently defaulted.
    current_ctx : dict
        Current afternoon context.  Expected keys: regime (str),
        fear_greed (int|None), levels_tested (list), signal_flips (list),
        new_prints (list).  Missing keys default to empty/safe values.

    Returns
    -------
    dict
        Deterministic delta object; never raises.
    """
    if morning_ctx is None:
        morning_ctx = {}
    if current_ctx is None:
        current_ctx = {}

    morning_regime = morning_ctx.get("regime") or "UNKNOWN"
    current_regime = current_ctx.get("regime") or "UNKNOWN"

    morning_fg = morning_ctx.get("fear_greed")
    current_fg = current_ctx.get("fear_greed")

    return {
        "regime": {
            "was": morning_regime,
            "now": current_regime,
            "changed": morning_regime != current_regime,
        },
        "fear_greed": {
            "was": morning_fg,
            "now": current_fg,
            "dir": _dir(morning_fg, current_fg),
        },
        "levels_tested": list(current_ctx.get("levels_tested") or []),
        "signal_flips": list(current_ctx.get("signal_flips") or []),
        "new_prints": list(current_ctx.get("new_prints") or []),
    }
