import type { ReactNode } from "react";

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`rounded-xl border border-line bg-card p-5 ${className}`}>{children}</section>;
}

export function StatTile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <Card>
      <div className="text-sm text-ink-2">{label}</div>
      <div className="mt-1 text-3xl font-semibold">{value}</div>
      {note && <div className="mt-1 text-xs text-muted">{note}</div>}
    </Card>
  );
}

const STATUS: Record<string, { icon: string; cls: string; label: string }> = {
  succeeded: { icon: "✓", cls: "text-good", label: "Succeeded" },
  failed: { icon: "✕", cls: "text-bad", label: "Failed" },
  budget_exceeded: { icon: "!", cls: "text-warn", label: "Budget stop" },
  running: { icon: "…", cls: "text-ink-2", label: "Running" },
  pending: { icon: "…", cls: "text-warn", label: "Pending" },
  approved: { icon: "✓", cls: "text-good", label: "Approved" },
  rejected: { icon: "✕", cls: "text-bad", label: "Rejected" },
};

export function Status({ status }: { status: string }) {
  const s = STATUS[status] ?? { icon: "·", cls: "text-ink-2", label: status };
  return (
    <span className={`inline-flex items-center gap-1 text-sm font-medium ${s.cls}`}>
      <span aria-hidden>{s.icon}</span>
      {s.label}
    </span>
  );
}

/** Budget meter: fill carries severity; track is a lighter step of the same ramp. */
export function BudgetMeter({ spent, cap }: { spent: number; cap: number }) {
  const pct = Math.min(100, (spent / cap) * 100);
  const fill = pct >= 90 ? "var(--bad)" : pct >= 70 ? "var(--warn)" : "var(--series-1)";
  return (
    <div role="meter" aria-valuemin={0} aria-valuemax={cap} aria-valuenow={spent} aria-label="Monthly spend vs cap"
      className="h-2 w-full rounded-full" style={{ background: "var(--series-1-track)" }}>
      <div className="h-2 rounded-full" style={{ width: `${Math.max(pct, 1.5)}%`, background: fill }} />
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
