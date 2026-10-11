/**
 * Minimal hand-curated types for v0.1 of the SDK. A future version will
 * auto-generate full types from /api/openapi.json using openapi-typescript.
 */

export interface Play {
  id: number;
  title: string;
  thesis?: string;
  type: string;
  direction?: string;
  status: string;
  primary_asset: string;
  related_assets?: string[];
  conditions?: Record<string, unknown>;
  timeframe?: string;
  expiry?: string;
  created_at: string;
  updated_at: string;
}

export interface PlayList {
  plays: Play[];
  count: number;
}

export type AlertCondition = "above" | "below";
export type AlertFrequency = "once" | "once_per_bar";

/** Body of `POST /api/alerts`. The server rejects any field not listed here. */
export interface AlertCreate {
  symbol: string;
  condition: AlertCondition;
  price: number;
  note?: string;
  frequency?: AlertFrequency;
  journal_id?: number;
  auto_close?: "tp_hit" | "sl_hit";
}

export interface Alert {
  id: number;
  user_id: string;
  symbol: string;
  condition: AlertCondition;
  price: number;
  note: string;
  frequency: AlertFrequency;
  created_at: string;
  triggered: boolean;
  triggered_at: string | null;
  active: boolean;
  journal_id?: number;
  auto_close?: "tp_hit" | "sl_hit";
  drawing_id?: number;
}

export interface UsageWindow {
  window: "minute" | "hour" | "day";
  window_seconds: number;
  used: number;
  limit: number;
  remaining: number;
  retry_after: number;
}

export interface UsageSurface {
  tier: string;
  unlimited: boolean;
  usage: UsageWindow[];
}

export interface UsageHistoryRow {
  date: string;
  rest: number;
  mcp: number;
  total: number;
}

export interface UsageResponse {
  user_id: number;
  tier: string;
  rest: UsageSurface;
  mcp: UsageSurface;
  history?: UsageHistoryRow[];
}

export interface ClientOptions {
  apiKey: string;
  baseUrl?: string;
  timeoutMs?: number;
  maxRetries?: number;
  userAgent?: string;
  fetch?: typeof fetch;
}
