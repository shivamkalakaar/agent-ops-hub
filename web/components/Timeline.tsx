"use client";
import { useEffect, useState } from "react";
import { supabase } from "@/lib/supabase";
import type { AgentRegistry, AgentStep } from "@/lib/types";
import { useRunSteps } from "@/lib/useRunSteps";
import { AgentGraph } from "@/components/AgentGraph";
import { ms, usd, when } from "@/lib/format";
import { Card } from "@/components/ui";

const ROLE: Record<string, string> = {
  supervisor: "Coordinator", research: "Research", researcher: "Researcher", scout: "Scout", writer: "Writer", editor: "Editor",
  fact_checker: "Fact-checker", illustrator: "Illustrator", final_check: "Final check", onepager: "One-pager", eval_case: "Eval case",
  fetch_feeds: "Fetch feeds", push_to_hub: "Send stories to the hub", send: "Email", inbox: "Read inbox",
};
const INPUT: Record<string, { title: string; sub: string }> = {
  market_intel: { title: "Stories", sub: "AI Stack Brief and RSS" },
  substack: { title: "Stories and posts", sub: "AI Stack Brief, RSS, your LinkedIn posts" },
};

/** Live run timeline: the team diagram on top (multi-agent runs), then every coordinator decision and specialist turn. */
export function Timeline({ runId, running }: { runId: string; running: boolean }) {
  const steps = useRunSteps(runId, running);
  const [open, setOpen] = useState<number | null>(null);
  const [roster, setRoster] = useState<AgentRegistry[]>([]);
  const agent = steps[0]?.agent;
  useEffect(() => {
    if (!agent || !INPUT[agent]) return;
    supabase.from("agent_registry").select("*").eq("agent", agent).order("position").then(({ data }) => setRoster((data ?? []) as AgentRegistry[]));
  }, [agent]);

  if (!steps.length) return null;
  const total = steps.reduce((s, x) => s + Number(x.cost_usd), 0);
  const live = steps.some((s) => s.status === "running");

  return (
    <Card>
      <div className="flex flex-wrap items-baseline gap-3">
        <h2 className="font-semibold">Steps</h2>
        {live && <span className="inline-flex items-center gap-1.5 text-sm"><span aria-hidden className="spinner" />running</span>}
        <span className="ml-auto text-sm text-ink-2 num">{steps.filter((s) => s.kind === "work").length} turns · {usd(total)}</span>
      </div>
      {agent && INPUT[agent] && roster.length > 0 && !steps.some((s) => s.specialist === "eval_case") && (
        <div className="mt-4"><AgentGraph steps={steps} runFinished={!running && !live} roster={roster} input={INPUT[agent]} /></div>
      )}
      <ol className="mt-6 space-y-0">
        {steps.map((s) => s.kind === "decision" ? (
          <li key={s.id} className="ml-[7px] border-l border-line py-1 pl-5 text-xs text-ink-2">
            <span className="font-medium text-ink">{s.summary ?? "deciding…"}</span>
            {s.reason && <span> · {s.reason}</span>}
            {(s.output as { by?: string } | null)?.by === "fallback" && <span className="ml-1 text-ink font-semibold">(fallback)</span>}
          </li>
        ) : (
          <li key={s.id} className="relative ml-[7px] border-l border-line pb-3 pl-5">
            <span aria-hidden className={`absolute -left-[7px] top-1.5 flex h-3.5 w-3.5 items-center justify-center rounded-full border-2 border-card text-[8px] leading-none ${
              s.status === "running" ? "bg-card ring-1 ring-ink" : s.status === "failed" ? "bg-ink text-bg" : s.status === "blocked" || s.status === "degraded"
              ? "bg-card ring-1 ring-ink" : "bg-ink"}`}>{s.status === "failed" ? "✕" : s.status === "blocked" || s.status === "degraded" ? "!" : ""}</span>
            <button onClick={() => setOpen(open === s.id ? null : s.id)} className="w-full text-left">
              <div className="flex flex-wrap items-baseline gap-x-3">
                <span className="font-medium">{ROLE[s.specialist] ?? s.specialist}</span>
                <span className="text-xs text-muted num">{when(s.started_at)} · {ms(s.latency_ms)} · {usd(Number(s.cost_usd))}</span>
              </div>
              <p className={`text-sm ${s.status === "failed" || s.status === "blocked" ? "font-semibold text-ink" : "text-ink-2"}`}>{s.summary ?? "working…"}</p>
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
