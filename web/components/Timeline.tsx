"use client";
import { useEffect, useState } from "react";
import { supabase } from "@/lib/supabase";
import type { AgentStep } from "@/lib/types";
import { ms, usd, when } from "@/lib/format";
import { Card } from "@/components/ui";

const ROLE: Record<string, string> = {
  supervisor: "Super agent", research: "Research", writer: "Writer", editor: "Editor",
  fact_checker: "Fact-checker", illustrator: "Illustrator", final_check: "Final check",
};

/** Live run timeline: supervisor decisions and specialist turns from agent_steps, streamed with Supabase Realtime
 *  (falls back to polling every 4 s while the run is going, in case Realtime isn't enabled on the table). */
export function Timeline({ runId, running }: { runId: string; running: boolean }) {
  const [steps, setSteps] = useState<AgentStep[]>([]);
  const [open, setOpen] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => supabase.from("agent_steps").select("*").eq("run_id", runId).order("seq")
      .then(({ data }) => alive && setSteps((data ?? []) as AgentStep[]));
    load();
    const upsert = (row: AgentStep) => setSteps((prev) =>
      [...prev.filter((s) => s.id !== row.id), row].sort((a, b) => a.seq - b.seq || a.id - b.id));
    const channel = supabase.channel(`steps-${runId}`)
      .on("postgres_changes", { event: "*", schema: "public", table: "agent_steps", filter: `run_id=eq.${runId}` },
        (p) => p.new && upsert(p.new as AgentStep))
      .subscribe();
    const poll = running ? setInterval(load, 4000) : undefined;
    return () => { alive = false; supabase.removeChannel(channel); if (poll) clearInterval(poll); };
  }, [runId, running]);

  if (!steps.length) return null;
  const total = steps.reduce((s, x) => s + Number(x.cost_usd), 0);
  const live = steps.some((s) => s.status === "running");

  return (
    <Card>
      <div className="flex flex-wrap items-baseline gap-3">
        <h2 className="font-semibold">Agent timeline</h2>
        {live && <span className="inline-flex items-center gap-1.5 text-sm text-accent"><span className="h-2 w-2 animate-pulse rounded-full bg-accent" />live</span>}
        <span className="ml-auto text-sm text-ink-2 num">{steps.filter((s) => s.kind === "work").length} turns · {usd(total)}</span>
      </div>
      <ol className="mt-4 space-y-0">
        {steps.map((s) => s.kind === "decision" ? (
          <li key={s.id} className="ml-[7px] border-l border-line py-1 pl-5 text-xs text-ink-2">
            <span className="font-medium text-ink">{s.summary ?? "deciding…"}</span>
            {s.reason && <span> · {s.reason}</span>}
            {(s.output as { by?: string } | null)?.by === "fallback" && <span className="ml-1 text-warn">(fallback)</span>}
          </li>
        ) : (
          <li key={s.id} className="relative ml-[7px] border-l border-line pb-3 pl-5">
            <span aria-hidden className={`absolute -left-[7px] top-1.5 h-3.5 w-3.5 rounded-full border-2 border-card ${
              s.status === "running" ? "animate-pulse bg-accent" : s.status === "failed" ? "bg-bad" : "bg-good"}`} />
            <button onClick={() => setOpen(open === s.id ? null : s.id)} className="w-full text-left">
              <div className="flex flex-wrap items-baseline gap-x-3">
                <span className="font-medium">{ROLE[s.specialist] ?? s.specialist}</span>
                <span className="text-xs text-muted num">{when(s.started_at)} · {ms(s.latency_ms)} · {usd(Number(s.cost_usd))}</span>
              </div>
              <p className={`text-sm ${s.status === "failed" ? "text-bad" : "text-ink-2"}`}>{s.summary ?? "working…"}</p>
              {s.reason && s.specialist !== "supervisor" && <p className="text-xs text-muted">asked: {s.reason}</p>}
            </button>
            {open === s.id && s.output && <StepOutput step={s} />}
          </li>
        ))}
      </ol>
    </Card>
  );
}

function StepOutput({ step }: { step: AgentStep }) {
  const out = step.output as Record<string, unknown>;
  const images = (out.images as { url: string; alt: string; kind: string }[] | undefined)?.filter((i) => i.url.startsWith("https://"));
  return (
    <div className="mt-2 space-y-2">
      {typeof out.md === "string" && (
        <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-lg border border-line bg-bg p-3 text-xs">{out.md}</pre>
      )}
      {images && images.length > 0 && (
        <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          {images.map((i) => <img key={i.url} src={i.url} alt={i.alt} className="rounded-lg border border-line" />)}
        </div>
      )}
      <pre className="max-h-80 overflow-auto rounded-lg border border-line bg-bg p-3 text-xs text-ink-2">
        {JSON.stringify(Object.fromEntries(Object.entries(out).filter(([k]) => k !== "md")), null, 2)}
      </pre>
    </div>
  );
}
