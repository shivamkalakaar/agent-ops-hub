import Link from "next/link";
import { supabase, configured } from "@/lib/supabase";
import type { LlmCall, Run } from "@/lib/types";
import { MONTHLY_CAP, ms, percentile, usd, when } from "@/lib/format";
import { BudgetMeter, Card, NotConfigured, StatTile, Status } from "@/components/ui";
import { SpendChart } from "@/components/SpendChart";

export const revalidate = 300;

export default async function Overview() {
  if (!configured) return <NotConfigured />;
  const since = new Date(Date.now() - 30 * 864e5).toISOString();
  const monthStart = new Date(Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth(), 1)).toISOString();
  const [{ data: runs }, { data: calls }, { count: approved }] = await Promise.all([
    supabase.from("runs").select("*").order("started_at", { ascending: false }).limit(40),
    supabase.from("llm_calls").select("*").gte("created_at", since).limit(10000),
    supabase.from("drafts").select("id", { count: "exact", head: true }).eq("status", "approved"),
  ]);
  const R = (runs ?? []) as Run[];
  const C = (calls ?? []) as LlmCall[];

  const monthSpend = C.filter((c) => c.created_at >= monthStart).reduce((s, c) => s + Number(c.cost_usd), 0);
  const okCalls = C.filter((c) => c.ok);
  const lat = okCalls.map((c) => c.latency_ms ?? 0);
  const finished = R.filter((r) => r.kind !== "eval" && r.status !== "running" && r.status !== "skipped");
  const successRate = finished.length ? finished.filter((r) => r.status === "succeeded").length / finished.length : 0;
  const draftRuns = R.filter((r) => r.agent === "market_intel" && r.kind === "draft" && r.status === "succeeded");
  const costPerDraft = draftRuns.length ? draftRuns.reduce((s, r) => s + Number(r.cost_usd), 0) / draftRuns.length : 0;
  const lastEval = R.find((r) => r.kind === "eval" && r.stats?.mean != null);

  const days = Array.from({ length: 30 }, (_, i) => {
    const day = new Date(Date.now() - (29 - i) * 864e5).toISOString().slice(0, 10);
    const dc = C.filter((c) => c.created_at.slice(0, 10) === day);
    return { day, cost: dc.reduce((s, c) => s + Number(c.cost_usd), 0), calls: dc.length };
  });
  const byTier = Object.entries(
    C.reduce<Record<string, { cost: number; calls: number; tokens: number }>>((acc, c) => {
      const t = (acc[c.tier] ??= { cost: 0, calls: 0, tokens: 0 });
      t.cost += Number(c.cost_usd); t.calls += 1; t.tokens += (c.prompt_tokens ?? 0) + (c.completion_tokens ?? 0);
      return acc;
    }, {}),
  );
  const byModel = Object.entries(
    okCalls.reduce<Record<string, number>>((acc, c) => ((acc[c.model ?? "?"] = (acc[c.model ?? "?"] ?? 0) + 1), acc), {}),
  ).sort((a, b) => b[1] - a[1]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Agent operations</h1>
        <p className="mt-1 max-w-2xl text-ink-2">
          Two agents. A market-intel agent reads ~12 AI feeds and my daily AI brief, scores what matters and drafts one
          short LinkedIn post every weekday. A Substack newsroom (a super agent running six specialists) turns the same news into
          two long posts a week with images. Every call is budget-checked, traced and evaluated; nothing ships without
          human approval.
        </p>
      </div>

      <Card>
        <div className="flex items-baseline justify-between">
          <div>
            <div className="text-sm text-ink-2">Spend this month</div>
            <div className="mt-1 text-5xl font-semibold">{usd(monthSpend)}</div>
          </div>
          <div className="text-right text-sm text-ink-2">cap {usd(MONTHLY_CAP)} · enforced before every call</div>
        </div>
        <div className="mt-4"><BudgetMeter spent={monthSpend} cap={MONTHLY_CAP} /></div>
      </Card>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <StatTile label="Run success rate" value={`${Math.round(successRate * 100)}%`} note={`last ${finished.length} runs`} />
        <StatTile label="Cost per LinkedIn draft" value={usd(costPerDraft)} note={`${draftRuns.length} drafts`} />
        <StatTile label="LLM latency p50 / p95" value={`${ms(percentile(lat, 50))}`} note={`p95 ${ms(percentile(lat, 95))}`} />
        <Link href="/evals" className="block hover:opacity-80">
          <StatTile label="Latest eval score (1–5)"
            value={lastEval?.stats?.mean != null ? String(lastEval.stats.mean) : "–"}
            note={lastEval ? `${lastEval.agent === "substack" ? "Substack" : "LinkedIn"} · ${lastEval.stats?.passed === false ? "failed" : "passed"} · all evals` : "no evals yet"} />
        </Link>
      </div>

      <Card>
        <h2 className="font-semibold">Daily LLM spend, last 30 days</h2>
        <div className="mt-3"><SpendChart days={days} /></div>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <h2 className="font-semibold">Cost by model tier</h2>
          <table className="mt-3 w-full text-sm">
            <thead className="text-left text-ink-2"><tr><th className="py-1 font-normal">Tier</th><th className="font-normal text-right">Calls</th><th className="font-normal text-right">Tokens</th><th className="font-normal text-right">Cost</th></tr></thead>
            <tbody className="num">
              {byTier.map(([tier, t]) => (
                <tr key={tier} className="border-t border-line">
                  <td className="py-1.5">{tier}</td><td className="text-right">{t.calls}</td>
                  <td className="text-right">{t.tokens.toLocaleString()}</td><td className="text-right">{usd(t.cost)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
        <Card>
          <h2 className="font-semibold">Models that actually served calls</h2>
          <p className="text-xs text-muted">OpenRouter falls back down each tier when a model fails or is rate-limited.</p>
          <ul className="mt-3 space-y-1 text-sm">
            {byModel.map(([m, n]) => (
              <li key={m} className="flex justify-between border-t border-line pt-1.5"><code>{m}</code><span className="num">{n}</span></li>
            ))}
          </ul>
        </Card>
      </div>

      <Card>
        <h2 className="font-semibold">How it works</h2>
        <Architecture />
      </Card>

      <Card>
        <h2 className="font-semibold">Recent runs</h2>
        <table className="mt-3 w-full text-sm">
          <thead className="text-left text-ink-2"><tr><th className="py-1 font-normal">Started</th><th className="font-normal">Agent</th><th className="font-normal">Workflow</th><th className="font-normal">Status</th><th className="font-normal text-right">Cost</th><th></th></tr></thead>
          <tbody>
            {R.slice(0, 15).map((r) => (
              <tr key={r.id} className="border-t border-line">
                <td className="py-1.5">{when(r.started_at)}</td>
                <td className="text-ink-2">{r.agent === "substack" ? "Substack" : "Market intel"}</td>
                <td><Link className="hover:underline" href={`/runs/${r.id}`}>{r.kind}</Link></td>
                <td><Status status={r.status} /></td>
                <td className="text-right num">{usd(Number(r.cost_usd))}</td>
                <td className="text-right">{r.trace_url && <a className="text-accent hover:underline" href={r.trace_url}>trace</a>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

function Architecture() {
  const box = "rounded-lg border border-line px-3 py-2 text-sm";
  const arrow = <span aria-hidden className="text-muted">→</span>;
  return (
    <div className="mt-3 space-y-4 text-sm">
      <div>
        <div className="mb-1 text-xs uppercase tracking-wide text-muted">Daily · GitHub Actions cron</div>
        <div className="flex flex-wrap items-center gap-2">
          <span className={box}>Fetch 12 RSS feeds</span>{arrow}<span className={box}>Dedupe (Postgres)</span>{arrow}
          <span className={box}>Score importance · <b>free model</b></span>{arrow}<span className={box}>Store</span>
        </div>
      </div>
      <div>
        <div className="mb-1 text-xs uppercase tracking-wide text-muted">Weekdays · LangGraph</div>
        <div className="flex flex-wrap items-center gap-2">
          <span className={box}>Pick one angle</span>{arrow}<span className={box}>Write post</span>{arrow}
          <span className={box}>Critic (other model family)</span>{arrow}<span className={box}>Guardrails</span>{arrow}
          <span className={`${box} border-dashed`}>Revise ↺ max 2</span>{arrow}<span className={box}>One-pager</span>{arrow}
          <span className={`${box} border-accent`}>Human approval</span>
        </div>
      </div>
      <div>
        <div className="mb-1 text-xs uppercase tracking-wide text-muted">Wed + Sat · Substack newsroom (super agent + six specialists)</div>
        <div className="flex flex-wrap items-center gap-2">
          <span className={`${box} border-accent`}>Super agent picks the next move</span><span aria-hidden className="text-muted">⇄</span>
          <span className={box}>Research</span><span className={box}>Writer · <b>Sonnet</b></span><span className={box}>Editor</span>
          <span className={box}>Fact-checker</span><span className={box}>Illustrator · <b>Nano Banana</b></span><span className={box}>Final check</span>
          {arrow}<span className={`${box} border-accent`}>Human approval</span>
        </div>
      </div>
      <p className="text-xs text-muted">
        Across every step: budget guard before each call · OpenRouter model fallbacks · Langfuse trace per run ·
        prompt regression evals in CI.
      </p>
    </div>
  );
}
