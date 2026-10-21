"use client";
import type { ReactNode } from "react";
import type { AgentStep } from "@/lib/types";

const SPECIALISTS = [
  { name: "research", role: "Research", icon: "M11 4a7 7 0 1 0 4.2 12.6l4.1 4.1 1.4-1.4-4.1-4.1A7 7 0 0 0 11 4Zm0 2a5 5 0 1 1 0 10 5 5 0 0 1 0-10Z" },
  { name: "writer", role: "Writer", icon: "M4 17.5V20h2.5L17.8 8.7l-2.5-2.5L4 17.5Zm15.7-11.2a1 1 0 0 0 0-1.4l-1.6-1.6a1 1 0 0 0-1.4 0l-1.2 1.2 2.5 2.5 1.7-.7Z" },
  { name: "editor", role: "Editor", icon: "M4 6h16v2H4V6Zm0 5h10v2H4v-2Zm0 5h7v2H4v-2Zm12.3-1.7 1.4-1.4 2.3 2.3-1.4 1.4-2.3-2.3Zm-1.4 1.4 2.3 2.3L15 20.2l-2.6.3.3-2.6 2.2-2.2Z" },
  { name: "fact_checker", role: "Fact-checker", icon: "M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5l-8-3Zm-1.2 13.6-3.5-3.5 1.4-1.4 2.1 2.1 4.7-4.7 1.4 1.4-6.1 6.1Z" },
  { name: "illustrator", role: "Illustrator", icon: "M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Zm1 2v8.6l3.5-3.5 3 3 4-5L19 14V7H5Zm3 3a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z" },
  { name: "final_check", role: "Final check", icon: "M5 3h2v18H5V3Zm3 1h10l-2 4 2 4H8V4Z" },
];
const HUB = "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm-7 9a2 2 0 1 0 0 4 2 2 0 0 0 0-4Zm14 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4ZM12 1a2 2 0 1 0 0 4 2 2 0 0 0 0-4ZM8.6 15.4l-2.3 2.3.9.9 2.3-2.3-.9-.9Zm6.8 0-.9.9 2.3 2.3.9-.9-2.3-2.3ZM11.4 5h1.2v3h-1.2V5Z";

type State = "running" | "done" | "failed" | "idle" | "off";

const Icon = ({ d, className = "" }: { d: string; className?: string }) => (
  <svg viewBox="0 0 24 24" aria-hidden className={`h-4 w-4 shrink-0 fill-current ${className}`}><path d={d} /></svg>
);

/** The newsroom as a block diagram: inputs → super agent ⇄ six specialists → approval queue.
 *  Whoever is working right now glows, with an animated hand-off line from the super agent. */
export function AgentGraph({ steps, runFinished, disabled = [] }:
  { steps: AgentStep[]; runFinished: boolean; disabled?: string[] }) {
  const work = steps.filter((s) => s.kind === "work");
  const running = steps.find((s) => s.status === "running");
  const lastDecision = [...steps].reverse().find((s) => s.kind === "decision");
  const load = work.find((s) => s.specialist === "supervisor" && s.seq === 1);
  const finish = [...work].reverse().find((s) => s.specialist === "supervisor" && s.seq > 1);
  const passed = !!finish?.summary?.includes("passed every check");

  const stateOf = (name: string): State => {
    if (disabled.includes(name)) return "off";
    if (running?.specialist === name && running.kind === "work") return "running";
    const mine = work.filter((s) => s.specialist === name);
    if (mine.at(-1)?.status === "failed") return "failed";
    return mine.length ? "done" : "idle";
  };
  const supervisorBusy = running?.specialist === "supervisor";
  const active = running && running.specialist !== "supervisor" ? running.specialist : null;

  return (
    <div className="space-y-0" role="img" aria-label={`Newsroom diagram${active ? `: ${active} is working` : ""}`}>
      {/* row 1: inputs → super agent → approval queue */}
      <div className="grid items-stretch gap-2 md:grid-cols-[1fr_auto_1.6fr_auto_1fr]">
        <Box tone={load ? "done" : "idle"} title="News + musings" sub={load?.summary ?? "AI brief, RSS feeds, your published musings"} />
        <Flow on={!!load && !finish} className="hidden md:block" />
        <Box tone={supervisorBusy ? "running" : steps.length ? "done" : "idle"} strong
          title={<span className="flex items-center gap-1.5"><Icon d={HUB} />Super agent</span>}
          sub={supervisorBusy && running?.kind === "decision" ? "deciding who works next…"
            : lastDecision?.summary ? `${lastDecision.summary}${lastDecision.reason ? ` · ${lastDecision.reason}` : ""}` : "decides who works next"} />
        <Flow on={!!finish} className="hidden md:block" />
        <Box tone={finish ? (passed ? "done" : "warn") : "idle"} title="Approval queue"
          sub={finish?.summary ?? (runFinished && steps.length ? "run ended before saving" : "waits for you to approve")} />
      </div>

      {/* hand-off bus: super agent → specialists */}
      <div className="relative hidden h-8 md:block" aria-hidden>
        <div className={`absolute left-1/2 top-0 h-4 w-0.5 -translate-x-1/2 ${active || supervisorBusy ? "flow-y" : "bg-line"}`} />
        <div className={`absolute left-[8.33%] right-[8.33%] top-4 h-0.5 ${active ? "bg-accent/40" : "bg-line"}`} />
      </div>

      <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3 md:mt-0 md:grid-cols-6">
        {SPECIALISTS.map((sp) => {
          const st = stateOf(sp.name);
          const mine = work.filter((s) => s.specialist === sp.name);
          return (
            <div key={sp.name} className="flex flex-col">
              <div aria-hidden className={`mx-auto hidden h-4 w-0.5 md:block ${st === "running" ? "flow-y" : "bg-line"}`} />
              <Box tone={st} title={<span className="flex items-center gap-1.5"><Icon d={sp.icon} />{sp.role}</span>}
                badge={st === "running" ? "working…" : st === "done" ? `✓ ${mine.length} turn${mine.length === 1 ? "" : "s"}`
                  : st === "failed" ? "✕ failed" : st === "off" ? "off" : "idle"}
                sub={st === "running" ? (running?.reason ?? "") : mine.at(-1)?.summary ?? ""} />
            </div>
          );
        })}
      </div>
    </div>
  );
}

const TONE: Record<string, string> = {
  running: "border-accent bg-card shadow-[0_0_0_4px_color-mix(in_srgb,var(--accent)_22%,transparent)] node-glow",
  done: "border-line bg-card",
  failed: "border-bad bg-card",
  warn: "border-warn bg-card",
  idle: "border-line bg-bg text-ink-2",
  off: "border-dashed border-line bg-bg text-muted opacity-60",
};
const BADGE: Record<string, string> = { running: "text-accent", done: "text-good", failed: "text-bad", warn: "text-warn", idle: "text-muted", off: "text-muted" };

function Box({ tone, title, sub, badge, strong }: { tone: string; title: ReactNode; sub?: string; badge?: string; strong?: boolean }) {
  return (
    <div className={`flex h-full min-w-0 flex-col rounded-xl border p-3 transition-shadow ${TONE[tone] ?? TONE.idle}`}>
      <div className="flex items-center justify-between gap-2">
        <div className={`text-sm ${strong ? "font-semibold" : "font-medium"}`}>{title}</div>
        {badge && <span className={`shrink-0 text-[11px] font-medium ${BADGE[tone] ?? ""}`}>{badge}</span>}
      </div>
      {sub && <p className="mt-1 line-clamp-2 text-xs text-ink-2" title={sub}>{sub}</p>}
    </div>
  );
}

function Flow({ on, className = "" }: { on: boolean; className?: string }) {
  return (
    <div aria-hidden className={`self-center ${className}`}>
      <div className={`h-0.5 w-8 ${on ? "flow-x" : "bg-line"}`} />
    </div>
  );
}
