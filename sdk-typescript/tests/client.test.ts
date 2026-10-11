import { describe, it, expect, vi, afterEach } from "vitest";
import { Client, AuthError, NotFoundError, RateLimitError, ApiError } from "../src/index.js";

afterEach(() => {
  vi.useRealTimers();
});

function mockFetch(handler: (req: Request) => Response | Promise<Response>) {
  return async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = new Request(input as any, init);
    return handler(req);
  };
}

describe("Client construction", () => {
  it("requires an apiKey", () => {
    // @ts-expect-error — intentional bad input
    expect(() => new Client({})).toThrow();
  });

  it("requires a baseUrl (no default host)", () => {
    const saved = process.env.CARRAC_BASE_URL;
    delete process.env.CARRAC_BASE_URL;
    try {
      expect(() => new Client({ apiKey: "k", fetch: (() => {}) as any })).toThrow(/baseUrl is required/);
    } finally {
      if (saved !== undefined) process.env.CARRAC_BASE_URL = saved;
    }
  });

  it("reads CARRAC_BASE_URL from the environment", () => {
    const saved = process.env.CARRAC_BASE_URL;
    process.env.CARRAC_BASE_URL = "http://env.example:1234/";
    try {
      const c = new Client({ apiKey: "k", fetch: (() => {}) as any });
      expect(c.baseUrl).toBe("http://env.example:1234");
    } finally {
      if (saved === undefined) delete process.env.CARRAC_BASE_URL;
      else process.env.CARRAC_BASE_URL = saved;
    }
  });

  it("strips trailing slash from baseUrl", () => {
    const c = new Client({ apiKey: "k", baseUrl: "https://example.com/", fetch: (() => {}) as any });
    expect(c.baseUrl).toBe("https://example.com");
  });
});

describe("Charts.get", () => {
  it("builds the right URL and attaches headers", async () => {
    let capturedUrl = "";
    let capturedHeaders: Headers | null = null;
    const fetchImpl = mockFetch((req) => {
      capturedUrl = req.url;
      capturedHeaders = new Headers(req.headers);
      return new Response(JSON.stringify({ symbol: "BTC", candles: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    const c = new Client({ apiKey: "test-key", baseUrl: "http://localhost:5002", fetch: fetchImpl as any });
    const out = await c.charts.get("BTC", { timeframe: "1h", indicators: "CPR,RSI" });

    expect(capturedUrl).toContain("/api/chart-internal/BTC");
    expect(capturedUrl).toContain("timeframe=1h");
    expect(capturedUrl).toContain("indicators=CPR%2CRSI");
    expect(capturedHeaders!.get("x-api-key")).toBe("test-key");
    expect(out).toEqual({ symbol: "BTC", candles: [] });
  });
});

describe("Composite.batch", () => {
  it("serialises array assets and include into comma-joined query", async () => {
    let capturedUrl = "";
    const fetchImpl = mockFetch((req) => {
      capturedUrl = req.url;
      return new Response(JSON.stringify({ assets: {} }), { status: 200 });
    });
    const c = new Client({ apiKey: "k", baseUrl: "http://localhost:5002", fetch: fetchImpl as any });
    await c.composite.batch(["BTC", "ETH"], { include: ["market", "scanner"] });
    expect(capturedUrl).toContain("assets=BTC%2CETH");
    expect(capturedUrl).toContain("include=market%2Cscanner");
  });
});

describe("Plays.create", () => {
  it("POSTs JSON body", async () => {
    let method = "";
    let body = "";
    const fetchImpl = mockFetch(async (req) => {
      method = req.method;
      body = await req.text();
      return new Response(JSON.stringify({ id: 1, title: "Test" }), { status: 201 });
    });
    const c = new Client({ apiKey: "k", baseUrl: "http://localhost:5002", fetch: fetchImpl as any });
    await c.plays.create({ title: "Test", primary_asset: "BTC" } as any);
    expect(method).toBe("POST");
    expect(body).toContain(`"title":"Test"`);
    expect(body).toContain(`"primary_asset":"BTC"`);
  });
});

describe("Error handling", () => {
  it("401 → AuthError", async () => {
    const fetchImpl = mockFetch(() => new Response(JSON.stringify({ error: "bad key" }), { status: 401 }));
    const c = new Client({ apiKey: "k", baseUrl: "http://localhost:5002", fetch: fetchImpl as any, maxRetries: 0 });
    await expect(c.charts.get("BTC")).rejects.toBeInstanceOf(AuthError);
  });

  it("404 → NotFoundError", async () => {
    const fetchImpl = mockFetch(() => new Response(JSON.stringify({ error: "nope" }), { status: 404 }));
    const c = new Client({ apiKey: "k", baseUrl: "http://localhost:5002", fetch: fetchImpl as any, maxRetries: 0 });
    await expect(c.plays.get(999)).rejects.toBeInstanceOf(NotFoundError);
  });

  it("429 → RateLimitError after retries with retry_after", async () => {
    vi.useFakeTimers();
    let calls = 0;
    const fetchImpl = mockFetch(() => {
      calls++;
      return new Response(JSON.stringify({ error: "rate limited" }), {
        status: 429,
        headers: { "Retry-After": "0" },
      });
    });
    const c = new Client({ apiKey: "k", baseUrl: "http://localhost:5002", fetch: fetchImpl as any, maxRetries: 1 });
    const assertion = expect(c.charts.get("BTC")).rejects.toBeInstanceOf(RateLimitError);
    await vi.runAllTimersAsync();
    await assertion;
    expect(calls).toBe(2); // initial + 1 retry
  });

  it("500 is retried with exponential backoff starting at 1s", async () => {
    vi.useFakeTimers();
    let calls = 0;
    const fetchImpl = mockFetch(() => {
      calls++;
      if (calls <= 2) return new Response("oops", { status: 500 });
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });
    const c = new Client({ apiKey: "k", baseUrl: "http://localhost:5002", fetch: fetchImpl as any, maxRetries: 3 });
    const pending = c.charts.get("BTC");
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toBe(1);
    await vi.advanceTimersByTimeAsync(999);
    expect(calls).toBe(1); // first retry has not fired before 1s
    await vi.advanceTimersByTimeAsync(1);
    expect(calls).toBe(2); // first retry after 1s
    await vi.advanceTimersByTimeAsync(1999);
    expect(calls).toBe(2); // second retry waits 2s
    await vi.advanceTimersByTimeAsync(1);
    await pending;
    expect(calls).toBe(3);
  }, 15_000); // real backoff sleeps (1s + 2s) — outlast vitest's 5s default under load
});

describe("Resource path building", () => {
  it("alerts.delete → DELETE /api/alerts/{id}", async () => {
    let method = "";
    let path = "";
    const fetchImpl = mockFetch((req) => {
      method = req.method;
      path = new URL(req.url).pathname;
      return new Response(null, { status: 204 });
    });
    const c = new Client({ apiKey: "k", baseUrl: "http://localhost:5002", fetch: fetchImpl as any });
    await c.alerts.delete(42);
    expect(method).toBe("DELETE");
    expect(path).toBe("/api/alerts/42");
  });

  it("keys.revoke → DELETE /api/keys/{id}", async () => {
    let path = "";
    const fetchImpl = mockFetch((req) => {
      path = new URL(req.url).pathname;
      return new Response(JSON.stringify({ success: true }), { status: 200 });
    });
    const c = new Client({ apiKey: "k", baseUrl: "http://localhost:5002", fetch: fetchImpl as any });
    await c.keys.revoke(7);
    expect(path).toBe("/api/keys/7");
  });

  it("usage.get passes history=false and days=7", async () => {
    let url = "";
    const fetchImpl = mockFetch((req) => {
      url = req.url;
      return new Response(JSON.stringify({ user_id: 1, rest: {}, mcp: {} }), { status: 200 });
    });
    const c = new Client({ apiKey: "k", baseUrl: "http://localhost:5002", fetch: fetchImpl as any });
    await c.usage.get({ history: false, days: 7 });
    expect(url).toContain("history=false");
    expect(url).toContain("days=7");
  });
});
