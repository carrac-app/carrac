import { ApiError, AuthError, NotFoundError, RateLimitError } from "./errors.js";
import type { ClientOptions } from "./types.js";
import { Charts, Plays, Alerts, Composite, Scanner, Usage, Keys } from "./resources.js";

const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_USER_AGENT = "carrac-ts-sdk/0.2.0";

export class Client {
  readonly apiKey: string;
  readonly baseUrl: string;
  readonly timeoutMs: number;
  readonly maxRetries: number;
  readonly userAgent: string;
  private readonly _fetch: typeof fetch;

  readonly charts: Charts;
  readonly plays: Plays;
  readonly alerts: Alerts;
  readonly composite: Composite;
  readonly scanner: Scanner;
  readonly usage: Usage;
  readonly keys: Keys;

  constructor(options: ClientOptions) {
    if (!options.apiKey) throw new Error("Client: apiKey is required");
    this.apiKey = options.apiKey;
    const baseUrl =
      options.baseUrl ||
      (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env?.CARRAC_BASE_URL;
    if (!baseUrl) {
      throw new Error(
        "Client: baseUrl is required (the address of your own Carrac instance, e.g. http://localhost:5002) - pass { baseUrl } or set CARRAC_BASE_URL",
      );
    }
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.maxRetries = options.maxRetries ?? 3;
    this.userAgent = options.userAgent ?? DEFAULT_USER_AGENT;
    this._fetch = options.fetch ?? globalThis.fetch;
    if (!this._fetch) {
      throw new Error("No fetch implementation found. Pass { fetch } or run on Node ≥ 18.");
    }

    this.charts = new Charts(this);
    this.plays = new Plays(this);
    this.alerts = new Alerts(this);
    this.composite = new Composite(this);
    this.scanner = new Scanner(this);
    this.usage = new Usage(this);
    this.keys = new Keys(this);
  }

  async request<T = unknown>(
    method: string,
    path: string,
    opts: { query?: Record<string, string | number | boolean | undefined>; body?: unknown } = {},
  ): Promise<T> {
    const url = this._buildUrl(path, opts.query);
    const headers: Record<string, string> = {
      "X-API-Key": this.apiKey,
      Accept: "application/json",
      "User-Agent": this.userAgent,
    };
    const init: RequestInit = { method, headers };
    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }

    let attempt = 0;
    // Loop handles 429/5xx retries.
    // eslint-disable-next-line no-constant-condition
    while (true) {
      attempt++;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.timeoutMs);
      let resp: Response;
      try {
        resp = await this._fetch(url, { ...init, signal: controller.signal });
      } catch (err) {
        clearTimeout(timer);
        if (attempt > this.maxRetries) throw new ApiError(`Network error: ${(err as Error).message}`);
        await sleep(Math.min(1000 * 2 ** (attempt - 1), 10_000));
        continue;
      }
      clearTimeout(timer);

      if (resp.status === 429 && attempt <= this.maxRetries) {
        const retryAfter = parseInt(resp.headers.get("Retry-After") || "1", 10);
        await sleep(Math.min(retryAfter * 1000, 30_000));
        continue;
      }
      if (resp.status >= 500 && resp.status < 600 && attempt <= this.maxRetries) {
        await sleep(Math.min(1000 * 2 ** (attempt - 1), 10_000));
        continue;
      }

      await this._raiseIfError(resp);

      if (resp.status === 204) return undefined as unknown as T;
      const text = await resp.text();
      if (!text) return undefined as unknown as T;
      try {
        return JSON.parse(text) as T;
      } catch {
        throw new ApiError(`Non-JSON response: ${text.slice(0, 200)}`);
      }
    }
  }

  private _buildUrl(path: string, query?: Record<string, string | number | boolean | undefined>): string {
    const url = new URL(path, this.baseUrl + "/");
    if (query) {
      for (const [k, v] of Object.entries(query)) {
        if (v === undefined || v === null) continue;
        url.searchParams.append(k, String(v));
      }
    }
    return url.toString();
  }

  private async _raiseIfError(resp: Response): Promise<void> {
    if (resp.ok) return;
    let body: any = {};
    let rawText = "";
    try {
      rawText = await resp.clone().text();
      body = rawText ? JSON.parse(rawText) : {};
    } catch {
      /* non-JSON; body stays {} */
    }
    const message: string = body?.error ?? rawText ?? resp.statusText;

    if (resp.status === 401 || resp.status === 403) {
      throw new AuthError(message, resp.status, body);
    }
    if (resp.status === 404) {
      throw new NotFoundError(message, resp.status, body);
    }
    if (resp.status === 429) {
      const retryAfter = parseInt(resp.headers.get("Retry-After") || "0", 10);
      throw new RateLimitError(message, retryAfter, body);
    }
    throw new ApiError(message, resp.status, body);
  }

  // Convenience shortcuts used by resource namespaces.
  get<T = unknown>(path: string, query?: Record<string, string | number | boolean | undefined>) {
    return this.request<T>("GET", path, { query });
  }

  post<T = unknown>(path: string, body?: unknown, query?: Record<string, string | number | boolean | undefined>) {
    return this.request<T>("POST", path, { body, query });
  }

  put<T = unknown>(path: string, body?: unknown, query?: Record<string, string | number | boolean | undefined>) {
    return this.request<T>("PUT", path, { body, query });
  }

  delete<T = unknown>(path: string, query?: Record<string, string | number | boolean | undefined>) {
    return this.request<T>("DELETE", path, { query });
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
