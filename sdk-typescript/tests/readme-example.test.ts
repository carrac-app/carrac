import { readFileSync } from "node:fs";
import { describe, it, expect } from "vitest";
import { Client, type Alert, type AlertCreate } from "../src/index.js";

// Compile-time exhaustive: adding or removing a field on AlertCreate breaks
// `npm run typecheck` until this list (and the server's create model) agree.
const ALERT_CREATE_KEYS = {
  symbol: true,
  condition: true,
  price: true,
  note: true,
  frequency: true,
  journal_id: true,
  auto_close: true,
} satisfies Record<keyof AlertCreate, true>;

function readmeAlertExample(): Record<string, unknown> {
  const readme = readFileSync(new URL("../README.md", import.meta.url), "utf8");
  const m = readme.match(/carrac\.alerts\.create\((\{[^}]*\})\)/);
  if (!m) throw new Error("README quick-start has no alerts.create example");
  return new Function(`return (${m[1]})`)() as Record<string, unknown>;
}

describe("README alert example", () => {
  it("uses only fields the create type knows and sends them unchanged", async () => {
    const example = readmeAlertExample();
    expect(Object.keys(example).every((k) => k in ALERT_CREATE_KEYS)).toBe(true);

    let body: unknown;
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      const req = new Request(input as any, init);
      body = await req.json();
      return new Response(JSON.stringify({ alert: { id: 1 } }), { status: 201 });
    };
    const c = new Client({ apiKey: "k", baseUrl: "http://localhost:5002", fetch: fetchImpl as any });
    await c.alerts.create(example as unknown as AlertCreate);
    expect(body).toEqual(example);
  });

  it("has a typed twin of the README call", () => {
    const typed: AlertCreate = { symbol: "BTC", condition: "above", price: 120000 };
    expect(typed).toEqual(readmeAlertExample());
  });
});

describe("Alert shape", () => {
  it("matches what the server stores", () => {
    const alert: Alert = {
      id: 1,
      user_id: "7",
      symbol: "BTC",
      condition: "above",
      price: 120000,
      note: "",
      frequency: "once_per_bar",
      created_at: "2026-01-01T00:00:00",
      triggered: false,
      triggered_at: null,
      active: true,
    };
    expect(alert.frequency).toBe("once_per_bar");
  });

  it("rejects the retired fields", () => {
    // @ts-expect-error — the server has no "every_time" frequency
    const bad: AlertCreate["frequency"] = "every_time";
    // @ts-expect-error — alert_type is not a create field
    const extra: AlertCreate = { symbol: "BTC", condition: "above", price: 1, alert_type: "price" };
    expect([bad, extra]).toHaveLength(2);
  });
});
