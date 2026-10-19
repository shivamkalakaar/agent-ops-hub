import Link from "next/link";
import { notFound } from "next/navigation";
import { supabase, configured } from "@/lib/supabase";
import type { LlmCall, Run } from "@/lib/types";
import { ms, usd, when } from "@/lib/format";
import { Card, NotConfigured, Status } from "@/components/ui";

export const revalidate = 300;

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
        {r.trace_url && <a href={r.trace_url} className="text-accent hover:underline">Open trace in Langfuse →</a>}
      </div>
      <div className="text-sm text-ink-2">
        {when(r.started_at)} · {usd(Number(r.cost_usd))} · {C.length} LLM calls · commit <code>{r.git_sha}</code>
      </div>
      {r.error && <Card className="border-bad"><pre className="whitespace-pre-wrap text-sm">{r.error}</pre></Card>}

      <Card>
        <h2 className="font-semibold">Steps</h2>
        <table className="mt-3 w-full text-sm">
          <thead className="text-left text-ink-2">
            <tr><th className="py-1 font-normal">Node</th><th className="font-normal">Model</th><th className="font-normal">Latency</th><th className="font-normal text-right">Tokens in/out</th><th className="font-normal text-right">Cost</th></tr>
          </thead>
          <tbody className="num">
            {C.map((c) => (
              <tr key={c.id} className="border-t border-line">
                <td className="py-1.5">{c.ok ? c.node : <span className="text-bad">✕ {c.node}</span>}<span className="ml-2 text-xs text-muted">{c.tier}</span></td>
                <td><code className="text-xs">{c.model ?? "–"}</code></td>
                <td className="w-48">
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 rounded-full bg-series" style={{ width: `${((c.latency_ms ?? 0) / maxLat) * 100}px` }} />
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

      {r.stats && (
        <Card>
          <h2 className="font-semibold">Run output</h2>
          <pre className="mt-2 overflow-x-auto text-xs text-ink-2">{JSON.stringify(r.stats, null, 2)}</pre>
        </Card>
      )}
    </div>
  );
}
