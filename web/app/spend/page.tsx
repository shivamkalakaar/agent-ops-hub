import { configured } from "@/lib/supabase";
import { usd } from "@/lib/format";
import { AGENT_NAME, caps as loadCaps, openRouterUsage, spendRows, sum, type SpendRow } from "@/lib/budget";
import { BudgetMeter, Card, NotConfigured, PageTitle, StatTile } from "@/components/ui";
import { SpendChart } from "@/components/SpendChart";

export const revalidate = 120;

const KIND: Record<string, string> = { draft: "Writing posts", eval: "Evals (CI and local)", collect: "Scoring stories", learn: "Learning from feedback",
  revise: "Rewrites you asked for", style_guide: "Style guide", bakeoff: "Model bake-off", manual: "Runs started by hand", tick: "AI Stack Brief", unknown: "Other" };

function group(rows: SpendRow[], key: (r: SpendRow) => string) {
  const m = new Map<string, { cost: number; calls: number; tokens: number; failed: number }>();
  for (const r of rows) {
    const g = m.get(key(r)) ?? { cost: 0, calls: 0, tokens: 0, failed: 0 };
    g.cost += r.cost_usd; g.calls += r.calls; g.tokens += r.tokens; g.failed += r.failed;
    m.set(key(r), g);
  }
  return [...m.entries()].sort((a, b) => b[1].cost - a[1].cost);
}

export default async function Spend() {
  if (!configured) return <NotConfigured />;
  const now = new Date();
  const today = now.toISOString().slice(0, 10), month = today.slice(0, 7);
  const [all, cap, or] = await Promise.all([spendRows(), loadCaps(now), openRouterUsage()]);
  const thisMonth = all.filter((r) => r.day.startsWith(month));
  const daySpend = sum(all.filter((r) => r.day === today)), monthSpend = sum(thisMonth), total = sum(all);
  const days = Array.from({ length: 30 }, (_, i) => {
    const day = new Date(now.getTime() - (29 - i) * 864e5).toISOString().slice(0, 10);
    const d = all.filter((r) => r.day === day);
    return { day, cost: sum(d), calls: d.reduce((s, r) => s + r.calls, 0) };
  });
  const byAgent = group(thisMonth, (r) => AGENT_NAME[r.agent] ?? r.agent);
  const byKind = group(thisMonth, (r) => KIND[r.kind] ?? r.kind);
  const byModel = group(thisMonth, (r) => r.model);
  const max = (g: [string, { cost: number }][]) => Math.max(0.0001, ...g.map(([, v]) => v.cost));
  const gap = or ? or.month - monthSpend : null;

  return (
    <div className="space-y-8">
      <PageTitle title="Spending">
        Every model call the agents make, from the call log. Caps come from <code>config/budget.yaml</code>, the file the agents
        check before each call, so one-off raises show here as soon as they&apos;re on main.
      </PageTitle>

      <Card>
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <div className="text-xs uppercase tracking-wide text-ink-2">This month</div>
            <div className="num mt-1 text-5xl font-medium">{usd(monthSpend)}</div>
          </div>
          <div className="text-right text-sm text-ink-2">
            cap <span className="num text-ink">{usd(cap.monthly)}</span>
            {cap.monthly !== cap.standingMonthly && <> for {now.toLocaleString("en-US", { month: "long", timeZone: "UTC" })} (normally {usd(cap.standingMonthly)})</>}
            <div>{usd(Math.max(0, cap.monthly - monthSpend))} left{cap.source === "fallback" && " · couldn't read budget.yaml, showing the Vercel setting"}</div>
          </div>
        </div>
        <div className="mt-4"><BudgetMeter spent={monthSpend} cap={cap.monthly} /></div>
      </Card>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <StatTile label="Today (UTC)" value={usd(daySpend)} note={`cap ${usd(cap.daily)}${cap.daily !== cap.standingDaily ? ` today, normally ${usd(cap.standingDaily)}` : ""}`} />
        <StatTile label="All time" value={usd(total)} note={`since ${all[0]?.day ?? "–"}`} />
        <StatTile label="Model calls this month" value={thisMonth.reduce((s, r) => s + r.calls, 0).toLocaleString()}
          note={`${thisMonth.reduce((s, r) => s + r.failed, 0)} failed`} />
        <StatTile label="OpenRouter's count" value={or ? usd(or.month) : "–"}
          note={or ? `this month${or.limit != null ? `, key limit ${usd(or.limit)}` : ""}` : "add OPENROUTER_API_KEY in Vercel to show it"} />
      </div>

      {gap != null && Math.abs(gap) >= 0.05 && (
        <p className="text-sm text-ink-2">
          OpenRouter has counted <span className="num text-ink">{usd(Math.abs(gap))}</span> {gap > 0 ? "more" : "less"} than the call log this month.
          {gap > 0 && " The provider's number is what you're billed, and its key limit stops every call when it's reached."}
        </p>
      )}

      <Card>
        <h2 className="font-semibold">Spend per day, last 30 days</h2>
        <div className="mt-3"><SpendChart days={days} /></div>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Breakdown title="By agent, this month" rows={byAgent} max={max(byAgent)} />
        <Breakdown title="By kind of work, this month" rows={byKind} max={max(byKind)} />
      </div>

      <Card>
        <h2 className="font-semibold">By model, this month</h2>
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[560px] text-sm">
            <thead className="text-left text-ink-2"><tr><th className="py-1 font-normal">Model</th><th className="font-normal text-right">Calls</th>
              <th className="font-normal text-right">Tokens</th><th className="font-normal text-right">Per call</th><th className="font-normal text-right">Cost</th></tr></thead>
            <tbody className="num">
              {byModel.map(([m, v]) => (
                <tr key={m} className="border-t border-line">
                  <td className="py-1.5 font-sans"><code className="text-xs">{m}</code>{m.endsWith(":free") && <span className="ml-2 text-xs text-muted">free</span>}</td>
                  <td className="text-right">{v.calls.toLocaleString()}</td>
                  <td className="text-right">{v.tokens.toLocaleString()}</td>
                  <td className="text-right">{usd(v.cost / Math.max(1, v.calls))}</td>
                  <td className="text-right">{usd(v.cost)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

function Breakdown({ title, rows, max }: { title: string; rows: [string, { cost: number; calls: number }][]; max: number }) {
  return (
    <Card>
      <h2 className="font-semibold">{title}</h2>
      <ul className="mt-3 space-y-2 text-sm">
        {rows.map(([name, v]) => (
          <li key={name}>
            <div className="flex justify-between gap-3"><span>{name}</span><span className="num">{usd(v.cost)}</span></div>
            <div className="mt-1 h-1.5 rounded-full bg-sunk"><div className="h-1.5 rounded-full bg-ink" style={{ width: `${(v.cost / max) * 100}%` }} /></div>
          </li>
        ))}
        {rows.length === 0 && <li className="text-ink-2">No spend yet this month.</li>}
      </ul>
    </Card>
  );
}
