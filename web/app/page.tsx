import Link from "next/link";
import { supabase, configured } from "@/lib/supabase";
import type { LlmCall, Run } from "@/lib/types";
import { ms, percentile, usd, when } from "@/lib/format";
import { caps as loadCaps, spendRows, sum } from "@/lib/budget";
import { BudgetMeter, Card, NotConfigured, PageTitle, StatTile, Status } from "@/components/ui";
import { SpendChart } from "@/components/SpendChart";

export const revalidate = 300;

export default async function Overview() {
  if (!configured) return <NotConfigured />;
  const since = new Date(Date.now() - 30 * 864e5).toISOString();
  const month = new Date().toISOString().slice(0, 7);
  // Totals come from the spend_by_day view (Postgres sums every call); llm_calls is read only for latency, and
  // PostgREST returns at most 1,000 of its rows, so the latest 1,000 calls are the sample.
  const [{ data: runs }, { data: calls }, rows, cap] = await Promise.all([
    supabase.from("runs").select("*").order("started_at", { ascending: false }).limit(40),
    supabase.from("llm_calls").select("latency_ms,ok").gte("created_at", since).order("created_at", { ascending: false }).limit(1000),
    spendRows(since.slice(0, 10)), loadCaps(),
  ]);
  const R = (runs ?? []) as Run[];
  const C = (calls ?? []) as Pick<LlmCall, "latency_ms" | "ok">[];

  const monthSpend = sum(rows.filter((r) => r.day.startsWith(month)));
  const lat = C.filter((c) => c.ok).map((c) => c.latency_ms ?? 0);
  const finished = R.filter((r) => r.kind !== "eval" && r.status !== "running" && r.status !== "skipped");
  const successRate = finished.length ? finished.filter((r) => r.status === "succeeded").length / finished.length : 0;
  const draftRuns = R.filter((r) => r.agent === "market_intel" && r.kind === "draft" && r.status === "succeeded");
  const costPerDraft = draftRuns.length ? draftRuns.reduce((s, r) => s + Number(r.cost_usd), 0) / draftRuns.length : 0;
  const lastEval = R.find((r) => r.kind === "eval" && r.stats?.mean != null);

  const days = Array.from({ length: 30 }, (_, i) => {
    const day = new Date(Date.now() - (29 - i) * 864e5).toISOString().slice(0, 10);
    const d = rows.filter((r) => r.day === day);
    return { day, cost: sum(d), calls: d.reduce((s, r) => s + r.calls, 0) };
  });
  const byTier = Object.entries(
    rows.reduce<Record<string, { cost: number; calls: number; tokens: number }>>((acc, r) => {
      const t = (acc[r.tier] ??= { cost: 0, calls: 0, tokens: 0 });
      t.cost += r.cost_usd; t.calls += r.calls; t.tokens += r.tokens;
      return acc;
    }, {}),
  ).sort((a, b) => b[1].cost - a[1].cost);
  const byModel = Object.entries(
    rows.reduce<Record<string, number>>((acc, r) => ((acc[r.model] = (acc[r.model] ?? 0) + r.calls - r.failed), acc), {}),
  ).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);

  return (
    <div className="space-y-8">
      <PageTitle title="Overview">
        Two writing agents. The LinkedIn agent writes one short post every day from your AI Stack Brief and about a dozen
        AI feeds. The Substack agent writes two long posts a week from the same stories. Each is a coordinator with specialists
        that research, write, edit and fact-check. Every model call is checked against the budget and traced, every prompt change is
        evaluated, and nothing is published until you approve it.
      </PageTitle>

      <Card>
        <div className="flex items-baseline justify-between">
          <div>
            <div className="text-sm text-ink-2">Spend this month</div>
            <div className="mt-1 text-5xl font-semibold">{usd(monthSpend)}</div>
          </div>
          <div className="text-right text-sm text-ink-2">cap {usd(cap.monthly)}{cap.monthly !== cap.standingMonthly && ` this month (normally ${usd(cap.standingMonthly)})`} · enforced before every call
            <div><Link href="/spend" className="underline underline-offset-4">Spending details</Link></div></div>
        </div>
        <div className="mt-4"><BudgetMeter spent={monthSpend} cap={cap.monthly} /></div>
      </Card>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <StatTile label="Runs finished cleanly" value={`${Math.round(successRate * 100)}%`} note={`last ${finished.length} runs`} />
        <StatTile label="Cost per LinkedIn draft" value={usd(costPerDraft)} note={`${draftRuns.length} drafts`} />
        <StatTile label="Model latency, median" value={`${ms(percentile(lat, 50))}`} note={`p95 ${ms(percentile(lat, 95))}`} />
        <Link href="/evals" className="block hover:opacity-80">
          <StatTile label="Latest eval score (1–5)"
            value={lastEval?.stats?.mean != null ? String(lastEval.stats.mean) : "–"}
            note={lastEval ? `${lastEval.agent === "substack" ? "Substack" : "LinkedIn"} · ${lastEval.stats?.passed === false ? "failed" : "passed"} · all evals` : "no evals yet"} />
        </Link>
      </div>

      <Card>
        <h2 className="font-semibold">Model spend per day, last 30 days</h2>
        <div className="mt-3"><SpendChart days={days} /></div>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <h2 className="font-semibold">Cost by model tier, last 30 days</h2>
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
          <h2 className="font-semibold">Models that answered</h2>
          <p className="text-xs text-muted">When a model fails or is rate-limited, OpenRouter moves to the next one in its tier.</p>
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
                <td className="text-ink-2">{({ substack: "Substack", ai_brief: "AI Stack Brief", market_intel: "LinkedIn" } as Record<string, string>)[r.agent] ?? r.agent}</td>
                <td><Link className="hover:underline" href={`/runs/${r.id}`}>{r.kind}</Link></td>
                <td><Status status={r.status} /></td>
                <td className="text-right num">{usd(Number(r.cost_usd))}</td>
                <td className="text-right">{r.trace_url && <a className="text-ink underline underline-offset-4 hover:underline" href={r.trace_url}>trace</a>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

function Architecture() {
  const flows: { when: string; steps: string[] }[] = [
    { when: "Every morning, on your Mac", steps: ["AI Stack Brief: read 20 feeds, research, write and email the brief",
      "Send its stories to the hub"] },
    { when: "Every morning, GitHub Actions", steps: ["Fetch about 12 RSS feeds", "Drop stories already seen", "Score each story with a free model"] },
    { when: "Weekdays: LinkedIn agent", steps: ["Scout picks an angle", "Researcher builds a fact sheet", "Writer drafts in your voice",
      "Editor, fact-checker and final check review it; the writer rewrites up to twice", "One-pager", "Your approval"] },
    { when: "Wednesday and Saturday: Substack agent", steps: ["Research brief", "Writer (Sonnet)", "Editor and fact-checker",
      "Images and a chart", "Final check", "Your approval"] },
  ];
  return (
    <div className="mt-4 grid gap-6 md:grid-cols-2">
      {flows.map((f) => (
        <div key={f.when}>
          <div className="mb-2 text-xs uppercase tracking-wide text-ink-2">{f.when}</div>
          <ol className="space-y-1.5 text-sm">
            {f.steps.map((step, i) => (
              <li key={step} className="flex gap-3">
                <span className="num w-5 shrink-0 text-right text-muted">{i + 1}</span>
                <span className={i === f.steps.length - 1 && step === "Your approval" ? "font-semibold" : ""}>{step}</span>
              </li>
            ))}
          </ol>
        </div>
      ))}
      <p className="text-xs text-muted md:col-span-2">
        On every run: a budget check before each model call, fallbacks to other models when one is down, a Langfuse trace, and
        lessons from your past approvals and rejections. On every prompt or code change: evals on past news days, on the PR and again after the merge.
      </p>
    </div>
  );
}
