"use client";
import type { ReactNode } from "react";
import type { AgentStep } from "@/lib/types";

const ICONS: Record<string, string> = {
  research: "M11 4a7 7 0 1 0 4.2 12.6l4.1 4.1 1.4-1.4-4.1-4.1A7 7 0 0 0 11 4Zm0 2a5 5 0 1 1 0 10 5 5 0 0 1 0-10Z",
  researcher: "M11 4a7 7 0 1 0 4.2 12.6l4.1 4.1 1.4-1.4-4.1-4.1A7 7 0 0 0 11 4Zm0 2a5 5 0 1 1 0 10 5 5 0 0 1 0-10Z",
  scout: "M12 2a7 7 0 0 0-7 7c0 5.2 7 13 7 13s7-7.8 7-13a7 7 0 0 0-7-7Zm0 9.5A2.5 2.5 0 1 1 12 6.5a2.5 2.5 0 0 1 0 5Z",
  writer: "M4 17.5V20h2.5L17.8 8.7l-2.5-2.5L4 17.5Zm15.7-11.2a1 1 0 0 0 0-1.4l-1.6-1.6a1 1 0 0 0-1.4 0l-1.2 1.2 2.5 2.5 1.7-.7Z",
  editor: "M4 6h16v2H4V6Zm0 5h10v2H4v-2Zm0 5h7v2H4v-2Zm12.3-1.7 1.4-1.4 2.3 2.3-1.4 1.4-2.3-2.3Zm-1.4 1.4 2.3 2.3L15 20.2l-2.6.3.3-2.6 2.2-2.2Z",
  fact_checker: "M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5l-8-3Zm-1.2 13.6-3.5-3.5 1.4-1.4 2.1 2.1 4.7-4.7 1.4 1.4-6.1 6.1Z",
  illustrator: "M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Zm1 2v8.6l3.5-3.5 3 3 4-5L19 14V7H5Zm3 3a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z",
  final_check: "M5 3h2v18H5V3Zm3 1h10l-2 4 2 4H8V4Z",
  onepager: "M6 2h9l5 5v15H6V2Zm8 1.5V8h4.5L14 3.5ZM8 11h8v1.5H8V11Zm0 3h8v1.5H8V14Zm0 3h5v1.5H8V17Z",
};
const DOT = "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z";
const HUB = "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm-7 9a2 2 0 1 0 0 4 2 2 0 0 0 0-4Zm14 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4ZM12 1a2 2 0 1 0 0 4 2 2 0 0 0 0-4ZM8.6 15.4l-2.3 2.3.9.9 2.3-2.3-.9-.9Zm6.8 0-.9.9 2.3 2.3.9-.9-2.3-2.3ZM11.4 5h1.2v3h-1.2V5Z";

type Tone = "running" | "done" | "failed" | "warn" | "idle" | "off";
export type RosterEntry = { specialist: string; role: string };

const Icon = ({ d, className = "" }: { d: string; className?: string }) => (
  <svg viewBox="0 0 24 24" aria-hidden className={`h-4 w-4 shrink-0 fill-current ${className}`}><path d={d} /></svg>
);

/** A team as a block diagram: inputs → coordinator ⇄ specialists → approval queue.
 *  The step that's working has a heavy border and a spinner; the hand-off line moves while it works. */
export function AgentGraph({ steps, runFinished, roster, input, disabled = [] }:
  { steps: AgentStep[]; runFinished: boolean; roster: RosterEntry[]; input: { title: string; sub: string }; disabled?: string[] }) {
  const work = steps.filter((s) => s.kind === "work");
  const running = steps.find((s) => s.status === "running");
  const lastDecision = [...steps].reverse().find((s) => s.kind === "decision");
  const load = work.find((s) => s.specialist === "supervisor" && s.seq === 1);
  const finish = [...work].reverse().find((s) => s.specialist === "supervisor" && s.seq > 1 && s.seq < 900);
  const passed = !!finish?.summary?.includes("passed every check");
  const team = roster.filter((r) => r.specialist !== "supervisor");

  const toneOf = (name: string): Tone => {
    if (disabled.includes(name)) return "off";
    if (running?.specialist === name && running.kind === "work") return "running";
    const mine = work.filter((s) => s.specialist === name);
    if (mine.at(-1)?.status === "failed") return "failed";
    return mine.length ? "done" : "idle";
  };
  const coordinatorBusy = running?.specialist === "supervisor";
  const active = running && running.specialist !== "supervisor" ? running.specialist : null;

  return (
    <div role="img" aria-label={`Diagram of the team${active ? `: ${active} is working` : ""}`}>
      <div className="grid items-stretch gap-2 md:grid-cols-[1fr_auto_1.6fr_auto_1fr]">
        <Box tone={load ? "done" : "idle"} title={input.title} sub={load?.summary ?? input.sub} />
        <Flow on={!!load && !finish} className="hidden md:block" />
        <Box tone={coordinatorBusy ? "running" : steps.length ? "done" : "idle"} strong
          title={<span className="flex items-center gap-1.5"><Icon d={HUB} />Coordinator</span>}
          sub={coordinatorBusy && running?.kind === "decision" ? "choosing the next step"
            : lastDecision?.summary ? `${lastDecision.summary}${lastDecision.reason ? ` · ${lastDecision.reason}` : ""}` : "chooses who works next"} />
        <Flow on={!!finish} className="hidden md:block" />
        <Box tone={finish ? (passed ? "done" : "warn") : "idle"} title="Approval queue"
          badge={finish ? (passed ? "● passed" : "! flagged") : undefined}
          sub={finish?.summary ?? (runFinished && steps.length ? "the run ended before saving" : "waits for your approval")} />
      </div>

      <div className="relative hidden h-8 md:block" aria-hidden>
        <div className={`absolute left-1/2 top-0 h-4 w-px -translate-x-1/2 ${active || coordinatorBusy ? "flow-y w-0.5" : "bg-line-strong"}`} />
        <div className="absolute top-4 h-px bg-line-strong" style={{ left: `${50 / team.length}%`, right: `${50 / team.length}%` }} />
      </div>

      <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3 md:mt-0 md:[grid-template-columns:var(--cols)]"
        style={{ "--cols": `repeat(${team.length}, minmax(0, 1fr))` } as React.CSSProperties}>
        {team.map((sp) => {
          const tone = toneOf(sp.specialist);
          const mine = work.filter((s) => s.specialist === sp.specialist);
          return (
            <div key={sp.specialist} className="flex flex-col">
              <div aria-hidden className={`mx-auto hidden h-4 md:block ${tone === "running" ? "flow-y w-0.5" : "w-px bg-line-strong"}`} />
              <Box tone={tone} title={<span className="flex items-center gap-1.5"><Icon d={ICONS[sp.specialist] ?? DOT} />{sp.role}</span>}
                badge={tone === "running" ? "working" : tone === "done" ? `${mine.length} turn${mine.length === 1 ? "" : "s"}`
                  : tone === "failed" ? "✕ failed" : tone === "off" ? "off" : undefined}
                sub={tone === "running" ? (running?.reason ?? "") : mine.at(-1)?.summary ?? ""} />
            </div>
          );
        })}
      </div>
    </div>
  );
}

const TONE: Record<Tone, string> = {
  running: "border-2 border-ink bg-card",
  done: "border border-line-strong bg-card",
  failed: "border-2 border-ink bg-sunk",
  warn: "border border-dashed border-ink bg-card",
  idle: "border border-line bg-bg text-ink-2",
  off: "border border-dashed border-line bg-bg text-muted opacity-60",
};

function Box({ tone, title, sub, badge, strong }: { tone: Tone; title: ReactNode; sub?: string; badge?: string; strong?: boolean }) {
  return (
    <div className={`flex h-full min-w-0 flex-col rounded-lg p-3 ${TONE[tone]}`}>
      <div className="flex items-center justify-between gap-2">
        <div className={`text-sm ${strong ? "font-semibold" : "font-medium"}`}>{title}</div>
        {tone === "running" ? <span aria-hidden className="spinner shrink-0" />
          : badge && <span className={`shrink-0 text-[11px] ${tone === "failed" || tone === "warn" ? "font-semibold" : "text-ink-2"}`}>{badge}</span>}
      </div>
      {sub && <p className="mt-1 line-clamp-2 text-xs text-ink-2" title={sub}>{sub}</p>}
    </div>
  );
}

function Flow({ on, className = "" }: { on: boolean; className?: string }) {
  return (
    <div aria-hidden className={`self-center ${className}`}>
      <div className={`w-8 ${on ? "flow-x h-0.5" : "h-px bg-line-strong"}`} />
    </div>
  );
}
