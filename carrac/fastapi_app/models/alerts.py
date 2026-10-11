"""Pydantic models for the alerts write surface.

POST/PUT/DELETE ``/api/alerts`` and sub-paths.
``notification-settings`` and ``analyst-sources`` live in the alerts
blueprint and are bundled here.

Validation philosophy: Pydantic enforces shape (``extra="forbid"``), type
coercion, and field-level constraints. Deep semantic validation (SSRF
checks, INDICATOR_ALERT_CONDITIONS lookup, formula parse, CANONICAL_METRICS
whitelist) stays in the route handlers and the helpers they call.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Price alert (POST /api/alerts)
# ---------------------------------------------------------------------------


class AlertCreateRequest(BaseModel):
    """Body for ``POST /api/alerts`` (price alert).

    Both ``symbol``/``asset`` and ``condition``/``direction`` are accepted
    as aliases. The ``model_validator`` resolves the canonical fields so the
    handler sees ``symbol`` and ``condition`` always.
    ``extra="forbid"`` blocks any field beyond the seven documented ones.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: Optional[str] = None
    asset: Optional[str] = None  # alias for symbol
    condition: Optional[str] = None
    direction: Optional[str] = None  # alias for condition
    price: float
    note: str = ""
    frequency: str = "once_per_bar"
    journal_id: Optional[int] = None
    auto_close: Optional[Literal["tp_hit", "sl_hit"]] = None

    @model_validator(mode="after")
    def resolve_aliases(self) -> "AlertCreateRequest":
        # Merge symbol/asset aliases
        if not self.symbol and self.asset:
            self.symbol = self.asset
        if not self.symbol:
            raise ValueError("Missing field: symbol")
        # Merge condition/direction aliases
        if not self.condition and self.direction:
            self.condition = self.direction
        if not self.condition or self.condition not in ("above", "below"):
            raise ValueError('Condition must be "above" or "below"')
        return self


# ---------------------------------------------------------------------------
# Trendline alert (POST /api/alerts/trendline)
# ---------------------------------------------------------------------------


class TrendlineAlertRequest(BaseModel):
    """Body for ``POST /api/alerts/trendline``."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    drawing_id: Union[str, int]
    p1_ts: float
    p1_val: float
    p2_ts: float
    p2_val: float
    note: str = ""
    frequency: str = "once"

    @model_validator(mode="after")
    def distinct_timestamps(self) -> "TrendlineAlertRequest":
        if self.p1_ts == self.p2_ts:
            raise ValueError("Trendline points must have different timestamps")
        return self


# ---------------------------------------------------------------------------
# Indicator alert (POST /api/alerts/indicator)
# ---------------------------------------------------------------------------


class IndicatorAlertRequest(BaseModel):
    """Body for ``POST /api/alerts/indicator``.

    ``indicator`` and ``target_state`` are validated against
    ``INDICATOR_ALERT_CONDITIONS`` in the handler — Pydantic just
    ensures they are present and of the right type.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str
    indicator: str
    target_state: Any  # validated server-side against INDICATOR_ALERT_CONDITIONS
    note: str = ""
    frequency: str = "once"


# ---------------------------------------------------------------------------
# Formula alert (POST /api/alerts/formula)
# ---------------------------------------------------------------------------


class FormulaAlertRequest(BaseModel):
    """Body for ``POST /api/alerts/formula``."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    expression: str = Field(min_length=1, max_length=500)
    timeframe: str = "1D"
    note: str = ""
    frequency: str = "once"


# ---------------------------------------------------------------------------
# Z-score alert (POST /api/alerts/zscore)  — non-free tier gate
# ---------------------------------------------------------------------------


class ZscoreAlertRequest(BaseModel):
    """Body for ``POST /api/alerts/zscore``."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    condition: Literal["above", "below"]
    threshold: float = Field(gt=0)
    window: int = 90
    note: str = ""
    frequency: str = "once"


# ---------------------------------------------------------------------------
# Regime alert (POST /api/alerts/regime) — non-free tier gate
# ---------------------------------------------------------------------------


class RegimeAlertRequest(BaseModel):
    """Body for ``POST /api/alerts/regime``.

    Only ``note`` and ``frequency`` are read from the body.
    """

    model_config = ConfigDict(extra="forbid")

    note: str = ""
    frequency: str = "once"


# ---------------------------------------------------------------------------
# Chain-strength alert (POST /api/alerts/chain_strength) — non-free tier
# ---------------------------------------------------------------------------


class ChainStrengthAlertRequest(BaseModel):
    """Body for ``POST /api/alerts/chain_strength``."""

    model_config = ConfigDict(extra="forbid")

    chain: str = ""
    condition: Literal["above", "below"] = "below"
    threshold: float = 0.15
    frequency: str = "once"
    note: str = ""


# ---------------------------------------------------------------------------
# Anomaly-cluster alert (POST /api/alerts/anomaly_cluster) — non-free
# ---------------------------------------------------------------------------


class AnomalyClusterAlertRequest(BaseModel):
    """Body for ``POST /api/alerts/anomaly_cluster``."""

    model_config = ConfigDict(extra="forbid")

    min_domains: int = Field(default=2, ge=1)
    min_metrics: int = Field(default=3, ge=1)
    frequency: str = "once"
    note: str = ""


# ---------------------------------------------------------------------------
# Conditional alert (POST /api/alerts/conditional) — non-free tier
# ---------------------------------------------------------------------------


class ConditionalAlertRequest(BaseModel):
    """Body for ``POST /api/alerts/conditional``.

    ``conditions`` may be a list OR a comma-separated string.
    The ``model_validator`` normalises both shapes to a list.
    """

    model_config = ConfigDict(extra="forbid")

    conditions: Union[list[str], str] = Field(default_factory=list)
    target: str = "BTC"
    horizon: int = Field(default=30, ge=1)
    frequency: str = "recurring"
    note: str = ""

    @model_validator(mode="after")
    def normalise_conditions(self) -> "ConditionalAlertRequest":
        if isinstance(self.conditions, str):
            self.conditions = [
                c.strip() for c in self.conditions.split(",") if c.strip()
            ]
        return self


# ---------------------------------------------------------------------------
# Alert update (PUT /api/alerts/<id>)
# ---------------------------------------------------------------------------


class AlertUpdateRequest(BaseModel):
    """Body for ``PUT /api/alerts/{alert_id}``."""

    model_config = ConfigDict(extra="forbid")

    price: Optional[float] = Field(default=None, gt=0)
    condition: Optional[Literal["above", "below"]] = None
    frequency: Optional[Literal["once", "once_per_bar"]] = None
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# Notification settings (POST /api/notification-settings)
# ---------------------------------------------------------------------------


class NtfySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str = Field(default="", max_length=500)


class DiscordSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    webhook_url: str = Field(default="", max_length=500)


class TelegramSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


class EmailSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class WebhookSettings(BaseModel):
    """Webhook card — enabled + url only; the secret and the
    delivery bookkeeping are server-managed and never accepted from input."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str = Field(default="", max_length=500)


class PositionWarningsSettings(BaseModel):
    """Notification TYPE toggle (position-health warnings). Default ON, so
    an absent block never silently disables them."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class NotificationSettingsRequest(BaseModel):
    """Body for ``POST /api/notification-settings``."""

    model_config = ConfigDict(extra="forbid")

    ntfy: NtfySettings = Field(default_factory=NtfySettings)
    discord: DiscordSettings = Field(default_factory=DiscordSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    # The page sends these two blocks; with extra="forbid" and no field for
    # them, every Save on Settings → Notifications would answer 400.
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    position_warnings: PositionWarningsSettings = Field(default_factory=PositionWarningsSettings)


# ---------------------------------------------------------------------------
# Analyst sources (POST /api/user/analyst-sources)
# ---------------------------------------------------------------------------


class AnalystSourcesRequest(BaseModel):
    """Body for ``POST /api/user/analyst-sources``."""

    model_config = ConfigDict(extra="forbid")

    x_handles: list[str] = Field(default_factory=list, max_length=10)
    yt_channels: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "AlertCreateRequest",
    "TrendlineAlertRequest",
    "IndicatorAlertRequest",
    "FormulaAlertRequest",
    "ZscoreAlertRequest",
    "RegimeAlertRequest",
    "ChainStrengthAlertRequest",
    "AnomalyClusterAlertRequest",
    "ConditionalAlertRequest",
    "AlertUpdateRequest",
    "NotificationSettingsRequest",
    "NtfySettings",
    "DiscordSettings",
    "TelegramSettings",
    "EmailSettings",
    "AnalystSourcesRequest",
]
