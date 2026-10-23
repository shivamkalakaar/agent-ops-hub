"use client";
// Every eval run, from PRs and from main after a merge: score against the baseline, the gate result, and a link to
// each run's cases. Refreshes every 15 seconds so a running eval shows up while CI is still on it.
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { supabase, configured } from "@/lib/supabase";
import type { Run } from "@/lib/types";
import { usd, when } from "@/lib/format";
import { Card, NotConfigured, Status } from "@/components/ui";

const REPO = "https://github.com/shivamkalakaar/agent-ops-hub";
const AGENT_NAME: Record<string, string> = { market_intel: "LinkedIn", substack: "Substack" };

type EvalStats = {
  mean?: number; humanizer_mean?: number; guardrail_pass_rate?: number; cases?: number; baseline?: number | null;
  passed?: boolean; gate?: string[]; trigger?: string; pr?: string | null; commit?: string | null; branch?: string | null;
  ci_url?: string | null; golden_cases?: number; rejection_cases?: number; rejection_repeats?: number;
};

export default function Evals() {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [agent, setAgent] = useState<"all" | "market_intel" | "substack">("all");

  const load = useCallback(async () => {
    const { data } = await supabase.from("runs").select("*").eq("kind", "eval").order("started_at", { ascending: false }).limit(60);
    setRuns((data ?? []) as Run[]);
  }, []);
  useEffect(() => {
    if (!configured) return;
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  if (!configured) return <NotConfigured />;
  const shown = (runs ?? []).filter((r) => agent === "all" || r.agent === agent);

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold">Evals</h1>
        <p className="text-ink-2">Each change to an agent&apos;s prompts or code runs it on a fixed set of past news days, on the PR and again on main
          after the merge. A run fails if the mean score drops more than 0.3 below the baseline, the humanizer average falls under 3,
          or a post breaks a guardrail.</p>
      </div>

      <div role="tablist" className="flex w-fit rounded-lg border border-line p-0.5 text-sm">
        {(["all", "market_intel", "substack"] as const).map((a) => (
          <button key={a} role="tab" aria-selected={agent === a} onClick={() => setAgent(a)}
            className={`rounded-md px-3 py-1 ${agent === a ? "bg-card font-medium shadow-sm" : "text-ink-2"}`}>
            {a === "all" ? "Both" : AGENT_NAME[a]}
          </button>
        ))}
      </div>

      {(agent === "all" ? ["market_intel", "substack"] : [agent]).map((a) => (
        <Trend key={a} agent={a} runs={(runs ?? []).filter((r) => r.agent === a)} />
      ))}

      <Card>
        <h2 className="font-semibold">Runs</h2>
        {runs === null ? <p className="mt-3 text-sm text-ink-2">Loading…</p> : shown.length === 0 ? (
          <p className="mt-3 text-sm text-ink-2">No eval runs yet. They start when a PR changes an agent&apos;s prompts or code.</p>
        ) : (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead className="text-left text-ink-2">
                <tr>
                  <th className="py-1 font-normal">Started</th><th className="font-normal">Agent</th><th className="font-normal">From</th>
                  <th className="font-normal text-right">Mean</th><th className="font-normal text-right">Baseline</th>
                  <th className="font-normal text-right">Humanizer</th><th className="font-normal text-right">Clean</th>
                  <th className="font-normal">Result</th><th className="font-normal text-right">Cost</th>
                </tr>
              </thead>
              <tbody className="num">
                {shown.map((r) => {
                  const s = (r.stats ?? {}) as EvalStats;
                  const running = r.status === "running";
                  return (
                    <tr key={r.id} className="border-t border-line align-top">
                      <td className="py-2"><Link href={`/runs/${r.id}`} className="hover:underline">{when(r.started_at)}</Link></td>
                      <td>{AGENT_NAME[r.agent] ?? r.agent}</td>
                      <td>
                        {s.pr ? <a href={`${REPO}/pull/${s.pr}`} className="hover:underline">PR #{s.pr}</a>
                          : s.trigger === "main" ? "main" : s.trigger ?? "local"}
                        {(s.commit ?? r.git_sha) && <code className="ml-2 text-xs text-muted">{(s.commit ?? r.git_sha ?? "").slice(0, 7)}</code>}
                      </td>
                      <td className="text-right font-medium">{s.mean?.toFixed(2) ?? "–"}</td>
                      <td className="text-right text-ink-2">{s.baseline?.toFixed(2) ?? "–"}</td>
                      <td className="text-right">{s.humanizer_mean != null ? `${s.humanizer_mean.toFixed(1)}/5` : "–"}</td>
                      <td className="text-right">{s.guardrail_pass_rate != null && s.cases ? `${Math.round(s.guardrail_pass_rate * (s.golden_cases ?? s.cases))}/${s.golden_cases ?? s.cases}` : "–"}
                        {s.rejection_cases ? <div className="text-xs text-ink-2">{s.rejection_repeats}/{s.rejection_cases} rejected problems back</div> : null}</td>
                      <td>
                        {running ? <Link href={`/runs/${r.id}`} className="hover:underline"><Status status="running" /></Link>
                          : <Status status={s.passed === false || r.status === "failed" ? "failed" : r.status} />}
                        {s.gate && s.gate.length > 0 && <div className="text-xs text-ink-2">{s.gate.join("; ")}</div>}
                        {r.status === "budget_exceeded" && <div className="text-xs text-ink-2">stopped by the budget cap</div>}
                      </td>
                      <td className="text-right">{usd(Number(r.cost_usd))}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

/** Mean score per eval run, oldest to newest, with the current baseline as a dashed line. */
function Trend({ agent, runs }: { agent: string; runs: Run[] }) {
  const pts = runs.filter((r) => typeof (r.stats as EvalStats | null)?.mean === "number").slice(0, 30).reverse();
  if (pts.length < 2) return null;
  const W = 640, H = 120, pad = { t: 10, r: 10, b: 18, l: 34 };
  const vals = pts.map((r) => (r.stats as EvalStats).mean!);
  const base = (pts[pts.length - 1].stats as EvalStats).baseline ?? null;
  const lo = Math.max(1, Math.floor(Math.min(...vals, base ?? 5) * 2) / 2 - 0.5), hi = 5;
  const x = (i: number) => pad.l + (i * (W - pad.l - pad.r)) / (pts.length - 1);
  const y = (v: number) => pad.t + (H - pad.t - pad.b) * (1 - (v - lo) / (hi - lo));
  return (
    <Card>
      <h2 className="font-semibold">{AGENT_NAME[agent]}: mean score per eval run</h2>
      <svg viewBox={`0 0 ${W} ${H}`} className="mt-2 w-full" role="img" aria-label={`${AGENT_NAME[agent]} eval mean over time`}>
        {[lo, (lo + hi) / 2, hi].map((v) => (
          <g key={v}>
            <line x1={pad.l} x2={W - pad.r} y1={y(v)} y2={y(v)} stroke="var(--line)" />
            <text x={pad.l - 6} y={y(v) + 4} textAnchor="end" fontSize={11} fill="var(--muted)">{v.toFixed(1)}</text>
          </g>
        ))}
        {base != null && <line x1={pad.l} x2={W - pad.r} y1={y(base)} y2={y(base)} stroke="var(--ink-2)" strokeDasharray="4 4" />}
        <polyline fill="none" stroke="var(--ink)" strokeWidth={1.5} points={vals.map((v, i) => `${x(i)},${y(v)}`).join(" ")} />
        {pts.map((r, i) => {
          const failed = (r.stats as EvalStats).passed === false;
          return (
            <a key={r.id} href={`/runs/${r.id}`}>
              <circle cx={x(i)} cy={y(vals[i])} r={4} fill={failed ? "var(--bg)" : "var(--ink)"} stroke="var(--ink)" strokeWidth={1.5}>
                <title>{`${when(r.started_at)}: ${vals[i].toFixed(2)}${failed ? " (failed)" : ""}`}</title>
              </circle>
            </a>
          );
        })}
      </svg>
      <p className="text-xs text-muted">Filled dot: passed · hollow dot: failed · dashed line: baseline{base != null ? ` (${base.toFixed(2)})` : ""}</p>
    </Card>
  );
}
