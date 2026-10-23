import type { ReactNode } from "react";

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`rounded-lg border border-line bg-card p-5 ${className}`}>{children}</section>;
}

export function StatTile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <Card className="h-full">
      <div className="text-xs uppercase tracking-wide text-ink-2">{label}</div>
      <div className="num mt-2 text-3xl font-medium">{value}</div>
      {note && <div className="mt-1 text-xs text-muted">{note}</div>}
    </Card>
  );
}

export function PageTitle({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="space-y-1.5">
      <h1 className="text-3xl font-semibold tracking-tight">{title}</h1>
      {children && <div className="max-w-3xl text-ink-2">{children}</div>}
    </div>
  );
}

/* Status by shape and weight, no color: ● done, ○ waiting, ◐ running, ✕ failed, – skipped, ! needs attention */
const STATUS: Record<string, { icon: string; cls: string; label: string }> = {
  succeeded: { icon: "●", cls: "text-ink", label: "Succeeded" },
  ok: { icon: "●", cls: "text-ink", label: "Done" },
  failed: { icon: "✕", cls: "text-ink font-semibold", label: "Failed" },
  blocked: { icon: "!", cls: "text-ink font-semibold underline underline-offset-4", label: "Blocked" },
  degraded: { icon: "!", cls: "text-ink", label: "Degraded" },
  budget_exceeded: { icon: "!", cls: "text-ink font-semibold underline underline-offset-4", label: "Budget stop" },
  running: { icon: "◐", cls: "text-ink", label: "Running" },
  pending: { icon: "○", cls: "text-ink-2", label: "Waiting for you" },
  approved: { icon: "●", cls: "text-ink", label: "Approved" },
  rejected: { icon: "✕", cls: "text-ink-2", label: "Rejected" },
  published: { icon: "●", cls: "text-ink font-semibold", label: "Published" },
  skipped: { icon: "–", cls: "text-muted", label: "Skipped" },
};

export function Status({ status }: { status: string }) {
  const s = STATUS[status] ?? { icon: "·", cls: "text-ink-2", label: status };
  return (
    <span className={`inline-flex items-center gap-1.5 text-sm ${s.cls}`}>
      {status === "running" ? <span aria-hidden className="spinner" /> : <span aria-hidden className="text-[0.8em]">{s.icon}</span>}
      {s.label}
    </span>
  );
}

/** Budget meter: a solid bar on a grey track; hatched from 80% of the cap. */
export function BudgetMeter({ spent, cap }: { spent: number; cap: number }) {
  const pct = Math.min(100, (spent / cap) * 100);
  return (
    <div role="meter" aria-valuemin={0} aria-valuemax={cap} aria-valuenow={spent} aria-label="Monthly spend vs cap"
      className="h-2 w-full overflow-hidden rounded-full bg-sunk">
      <div className={`h-2 rounded-full ${pct >= 80 ? "hatch" : "bg-ink"}`} style={{ width: `${Math.max(pct, 1.5)}%` }} />
    </div>
  );
}

export function NotConfigured() {
  return (
    <Card>
      <p className="text-ink-2">
        Supabase isn&apos;t configured yet. Set <code>NEXT_PUBLIC_SUPABASE_URL</code> and{" "}
        <code>NEXT_PUBLIC_SUPABASE_ANON_KEY</code> (see the README).
      </p>
    </Card>
  );
}
