import type { Client } from "./client.js";
import type { Play, PlayList, Alert, AlertCreate, UsageResponse } from "./types.js";

class Resource {
  protected client: Client;
  constructor(client: Client) {
    this.client = client;
  }
}

export class Charts extends Resource {
  get<T = unknown>(symbol: string, opts: { timeframe?: string; indicators?: string } = {}) {
    return this.client.get<T>(`/api/chart-internal/${symbol}`, opts);
  }
}

export class Plays extends Resource {
  list(opts: {
    status?: string;
    asset?: string;
    type?: string;
    include_resolved?: boolean;
    limit?: number;
    offset?: number;
  } = {}) {
    return this.client.get<PlayList>("/api/plays", opts);
  }
  active() {
    return this.client.get<PlayList>("/api/plays/active");
  }
  context<T = unknown>() {
    return this.client.get<T>("/api/plays/context");
  }
  get(id: number) {
    return this.client.get<Play>(`/api/plays/${id}`);
  }
  create(fields: Partial<Play>) {
    return this.client.post<Play>("/api/plays", fields);
  }
  update(id: number, fields: Partial<Play>) {
    return this.client.put<Play>(`/api/plays/${id}`, fields);
  }
  delete(id: number) {
    return this.client.delete(`/api/plays/${id}`);
  }
}

export class Alerts extends Resource {
  list(opts: { active?: boolean; triggered_since?: string } = {}) {
    return this.client.get<{ alerts: Alert[] }>("/api/alerts", opts);
  }
  create(fields: AlertCreate) {
    return this.client.post<{ alert: Alert }>("/api/alerts", fields);
  }
  delete(id: number | string) {
    return this.client.delete(`/api/alerts/${id}`);
  }
  toggle(id: number | string) {
    return this.client.post<{ alert: Alert }>(`/api/alerts/${id}/toggle`);
  }
}

export class Composite extends Resource {
  assetDetail<T = unknown>(symbol: string, opts: { format?: "full" | "compact" } = {}) {
    return this.client.get<T>(`/api/asset/${symbol}/detail`, opts);
  }
  livePositions<T = unknown>(opts: { asset?: string; format?: "full" | "compact" } = {}) {
    return this.client.get<T>("/api/positions/live", opts);
  }
  batch<T = unknown>(
    assets: string | string[],
    opts: { include?: string | string[]; format?: "full" | "compact" } = {},
  ) {
    const query: Record<string, string> = {
      assets: Array.isArray(assets) ? assets.join(",") : assets,
    };
    if (opts.include) {
      query.include = Array.isArray(opts.include) ? opts.include.join(",") : opts.include;
    }
    if (opts.format) query.format = opts.format;
    return this.client.get<T>("/api/batch", query);
  }
}

export class Scanner extends Resource {
  list<T = unknown>(opts: { verdict?: string; direction?: "long" | "short" | "both" } = {}) {
    const query = { direction: opts.direction ?? "both", ...(opts.verdict ? { verdict: opts.verdict } : {}) };
    return this.client.get<T>("/api/scanner", query);
  }
}

export class Usage extends Resource {
  get(opts: { history?: boolean; days?: number } = {}) {
    const query: Record<string, string | number> = {};
    if (opts.history === false) query.history = "false";
    if (opts.days !== undefined) query.days = opts.days;
    return this.client.get<UsageResponse>("/api/usage", query);
  }
}

export class Keys extends Resource {
  list<T = unknown>() {
    return this.client.get<T>("/api/keys");
  }
  create(name: string, opts: { scope?: "full" | "read-only" } = {}) {
    return this.client.post("/api/keys", { name, scope: opts.scope ?? "full" });
  }
  revoke(id: number) {
    return this.client.delete(`/api/keys/${id}`);
  }
}
