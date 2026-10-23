// Spend and caps for the dashboard, server-side only.
// Caps come from config/budget.yaml on main, the same file the agents enforce (core/budget.py), so one-off
// overrides like October's $15 show here too. OpenRouter's own count needs OPENROUTER_API_KEY in the server env.
import { supabase } from "@/lib/supabase";

const BUDGET_URL = "https://raw.githubusercontent.com/shivamkalakaar/agent-ops-hub/main/config/budget.yaml";

export type SpendRow = { day: string; agent: string; kind: string; tier: string; model: string; calls: number; failed: number;
  tokens: number; cost_usd: number };
export type Caps = { daily: number; monthly: number; standingDaily: number; standingMonthly: number; source: "repo" | "fallback" };

/** The value after `key:` on one line, or the {"k": v} map after it. Enough YAML for budget.yaml's flat keys. */
function yamlNumber(text: string, key: string): number | null {
  const m = text.match(new RegExp(`^${key}:\\s*([0-9.]+)`, "m"));
  return m ? Number(m[1]) : null;
}
function yamlMap(text: string, key: string): Record<string, number> {
  const m = text.match(new RegExp(`^${key}:\\s*\\{([^}]*)\\}`, "m"));
  if (!m) return {};
  return Object.fromEntries([...m[1].matchAll(/"?([\w-]+)"?\s*:\s*([0-9.]+)/g)].map((x) => [x[1], Number(x[2])]));
}

export async function caps(now = new Date()): Promise<Caps> {
  const day = now.toISOString().slice(0, 10), month = day.slice(0, 7);
  try {
    const res = await fetch(BUDGET_URL, { next: { revalidate: 300 } });
    if (!res.ok) throw new Error(String(res.status));
    const text = await res.text();
    const d = yamlNumber(text, "daily_cap_usd"), m = yamlNumber(text, "monthly_cap_usd");
    if (d == null || m == null) throw new Error("caps missing");
    return { daily: yamlMap(text, "daily_cap_overrides")[day] ?? d, monthly: yamlMap(text, "monthly_cap_overrides")[month] ?? m,
      standingDaily: d, standingMonthly: m, source: "repo" };
  } catch {
    const m = Number(process.env.NEXT_PUBLIC_MONTHLY_CAP_USD ?? 10);
    return { daily: 3, monthly: m, standingDaily: 3, standingMonthly: m, source: "fallback" };
  }
}

/** Every row of the spend_by_day view, paging past PostgREST's 1,000-row limit. */
export async function spendRows(since?: string): Promise<SpendRow[]> {
  const out: SpendRow[] = [];
  for (let from = 0; ; from += 1000) {
    let q = supabase.from("spend_by_day").select("*").order("day").range(from, from + 999);
    if (since) q = q.gte("day", since);
    const { data, error } = await q;
    if (error) throw new Error(error.message);
    out.push(...((data ?? []) as SpendRow[]).map((r) => ({ ...r, calls: Number(r.calls), failed: Number(r.failed),
      tokens: Number(r.tokens), cost_usd: Number(r.cost_usd) })));
    if (!data || data.length < 1000) return out;
  }
}

export type OpenRouterUsage = { month: number; total: number; limit: number | null };

/** OpenRouter's own numbers for the agents' key: what the provider will actually bill. */
export async function openRouterUsage(): Promise<OpenRouterUsage | null> {
  const key = process.env.OPENROUTER_API_KEY;
  if (!key) return null;
  try {
    const res = await fetch("https://openrouter.ai/api/v1/key", { headers: { Authorization: `Bearer ${key}` }, next: { revalidate: 120 } });
    if (!res.ok) return null;
    const d = (await res.json()).data;
    return { month: Number(d.usage_monthly ?? 0), total: Number(d.usage ?? 0), limit: d.limit == null ? null : Number(d.limit) };
  } catch {
    return null;
  }
}

export const sum = (rows: SpendRow[]) => rows.reduce((s, r) => s + r.cost_usd, 0);
export const AGENT_NAME: Record<string, string> = { market_intel: "LinkedIn", substack: "Substack", ai_brief: "AI Stack Brief" };
