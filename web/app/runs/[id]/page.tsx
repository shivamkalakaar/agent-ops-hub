import Link from "next/link";
import { notFound } from "next/navigation";
import { supabase, configured } from "@/lib/supabase";
import type { LlmCall, Run } from "@/lib/types";
import { ms, usd, when } from "@/lib/format";
import { Card, NotConfigured, Status } from "@/components/ui";
import { Timeline } from "@/components/Timeline";

export const revalidate = 60;

export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  if (!configured) return <NotConfigured />;
  const { id } = await params;
  const [{ data: run }, { data: calls }] = await Promise.all([
    supabase.from("runs").select("*").eq("id", id).maybeSingle(),
    supabase.from("llm_calls").select("*").eq("run_id", id).order("id"),
  ]);
  if (!run) notFound();
  const r = run as Run;
  const C = (calls ?? []) as LlmCall[];
  const maxLat = Math.max(1, ...C.map((c) => c.latency_ms ?? 0));

  return (
    <div className="space-y-6">
      <Link href="/" className="text-sm text-ink-2 hover:underline">← Overview</Link>
      <div className="flex flex-wrap items-baseline gap-4">
        <h1 className="text-2xl font-semibold">{r.agent} · {r.kind}</h1>
        <Status status={r.status} />
        {r.trace_url && <a href={r.trace_url} className="text-ink underline underline-offset-4 hover:underline">Open trace in Langfuse →</a>}
      </div>
      <div className="text-sm text-ink-2">
        {when(r.started_at)} · {usd(Number(r.cost_usd))} · {C.length} LLM calls · commit <code>{r.git_sha}</code>
      </div>
      {r.error && <Card className="border-ink"><pre className="whitespace-pre-wrap text-sm">{r.error}</pre></Card>}

      <Timeline runId={r.id} running={r.status === "running"} />

      <Card>
        <h2 className="font-semibold">LLM calls</h2>
        <table className="mt-3 w-full text-sm">
          <thead className="text-left text-ink-2">
            <tr><th className="py-1 font-normal">Node</th><th className="font-normal">Model</th><th className="font-normal">Latency</th><th className="font-normal text-right">Tokens in/out</th><th className="font-normal text-right">Cost</th></tr>
          </thead>
          <tbody className="num">
            {C.map((c) => (
              <tr key={c.id} className="border-t border-line">
                <td className="py-1.5">{c.ok ? c.node : <span className="text-ink font-semibold">✕ {c.node}</span>}<span className="ml-2 text-xs text-muted">{c.tier}</span></td>
                <td><code className="text-xs">{c.model ?? "–"}</code></td>
                <td className="w-48">
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 rounded-full bg-ink-2" style={{ width: `${((c.latency_ms ?? 0) / maxLat) * 100}px` }} />
                    <span className="text-xs text-ink-2">{ms(c.latency_ms)}</span>
                  </div>
                </td>
                <td className="text-right">{c.prompt_tokens ?? "–"} / {c.completion_tokens ?? "–"}</td>
                <td className="text-right">{usd(Number(c.cost_usd))}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      {r.kind === "eval" && Array.isArray(r.stats?.results) && <EvalCases stats={r.stats} />}

      {r.stats && (
        <Card>
          <details>
            <summary className="cursor-pointer font-semibold">Run output (raw)</summary>
            <pre className="mt-2 overflow-x-auto text-xs text-ink-2">{JSON.stringify(r.stats, null, 2)}</pre>
          </details>
        </Card>
      )}
    </div>
  );
}

type EvalCase = { case: string; mean: number; humanizer: number; scores?: Record<string, number>; violations?: string[];
  post?: string; title?: string; passed_final?: boolean };

/** One eval run's cases: the post each one produced, its scores and its flags. */
function EvalCases({ stats }: { stats: Record<string, unknown> }) {
  const cases = stats.results as EvalCase[];
  const gate = (stats.gate as string[] | undefined) ?? [];
  return (
    <Card>
      <h2 className="font-semibold">Eval cases</h2>
      <p className="mt-1 text-sm text-ink-2">
        Mean {String(stats.mean ?? "–")}{stats.baseline != null && ` against a baseline of ${String(stats.baseline)}`}.{" "}
        {stats.passed === false ? `Failed: ${gate.join("; ")}.` : stats.passed ? "Passed." : ""}
        {typeof stats.ci_url === "string" && <> <a href={stats.ci_url} className="underline">CI job</a></>}
      </p>
      <ul className="mt-3 divide-y divide-line">
        {cases.map((c) => (
          <li key={c.case} className="py-3">
            <details>
              <summary className="cursor-pointer">
                <span className="font-medium">{c.case}</span>
                <span className="num ml-3 text-sm text-ink-2">overall {c.mean} · humanizer {c.humanizer}/5
                  {c.passed_final != null && ` · final check ${c.passed_final ? "passed" : "failed"}`}
                  {c.violations?.length ? ` · ${c.violations.length} flag${c.violations.length === 1 ? "" : "s"}` : ""}</span>
              </summary>
              {c.violations && c.violations.length > 0 && (
                <ul className="mt-2 list-disc pl-5 text-sm">{c.violations.map((v) => <li key={v}>{v}</li>)}</ul>
              )}
              {c.scores && (
                <p className="num mt-2 text-xs text-ink-2">{Object.entries(c.scores).map(([k, v]) => `${k} ${v}`).join(" · ")}</p>
              )}
              {c.post && <pre className="mt-2 whitespace-pre-wrap rounded-lg border border-line bg-bg p-3 text-sm">{c.title ? `# ${c.title}\n\n` : ""}{c.post}</pre>}
            </details>
          </li>
        ))}
      </ul>
    </Card>
  );
}
