"""
Config loaders + primary client factory for the morning engine.

Reads analysis_steering.yaml / patterns.yaml / derivatives_regimes.yaml from
either the local package config/ or the Docker bind-mount at /app/morning_engine/config/.

``make_primary_client()`` honours the ``MORNING_PRIMARY_MODEL`` env var to
pick Opus / Sonnet / Grok for report generation.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger('morning_engine')


def load_steering_yaml():
    """Load analysis steering YAML from config directory.

    The text goes into the report model's system prompt verbatim, but it is
    parsed first: an operator-edited file that is not a YAML mapping raises
    ``ValueError`` instead of handing the model a half-read framework.
    """
    paths = [
        os.path.join(os.path.dirname(__file__), "config", "analysis_steering.yaml"),
        "/app/morning_engine/config/analysis_steering.yaml",
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p) as f:
                text = f.read()
            _validate_steering(text, p)
            return text
    log.warning("analysis_steering.yaml not found, using minimal config")
    return "# Steering config not available — use general market analysis framework."


def _validate_steering(text, path):
    """Raise ValueError unless ``text`` parses as a YAML mapping."""
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(
            f"{path} must be a YAML mapping, got {type(data).__name__}"
        )


def load_patterns_yaml():
    """Load patterns memory YAML from config directory."""
    paths = [
        os.path.join(os.path.dirname(__file__), "config", "patterns.yaml"),
        "/app/morning_engine/config/patterns.yaml",
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p) as f:
                return f.read()
    return "# No pattern memory available."


def load_derivatives_regimes():
    """Load derivatives regime playbook YAML for Options Corner context."""
    paths = [
        os.path.join(os.path.dirname(__file__), "config", "derivatives_regimes.yaml"),
        "/app/morning_engine/config/derivatives_regimes.yaml",
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p) as f:
                return f.read()
    return ""


def make_primary_client():
    """Build the primary report-generation client.

    Default: Sonnet 4.6 (cost: Opus on the free shared tier would cost more
    than the paid personal sections; Sonnet is sufficient for
    structured-data synthesis).

    Honours MORNING_PRIMARY_MODEL env var to override:
        - "grok"   → GrokClient (temporary cost/speed override)
        - "opus"   → MorningAnthropicClient with Opus (rollback path if Sonnet
                     quality regresses on a given report; flip the env var,
                     no code change needed)
        - "sonnet" / unset → MorningAnthropicClient with Sonnet 4.6 (default)

    All clients implement the same chat() interface.
    """
    from .anthropic_client import MorningAnthropicClient
    from .config import ANTHROPIC_MODEL, ANTHROPIC_MODEL_FAST
    from .grok_client import GrokClient

    primary = (os.environ.get("MORNING_PRIMARY_MODEL") or "").strip().lower()
    if primary == "grok":
        log.info("PRIMARY MODEL OVERRIDE: Grok (MORNING_PRIMARY_MODEL=grok)")
        return GrokClient()
    if primary == "opus":
        log.info("PRIMARY MODEL OVERRIDE: Opus (MORNING_PRIMARY_MODEL=opus)")
        return MorningAnthropicClient(model=ANTHROPIC_MODEL)
    # Default = Sonnet 4.6.
    return MorningAnthropicClient(model=ANTHROPIC_MODEL_FAST)
