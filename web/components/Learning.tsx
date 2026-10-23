"use client";
// What an agent has learned from the owner's decisions (core/lessons.py), and whether its drafts are getting
// better: fewer rejections and smaller edits over time.
import { useCallback, useEffect, useState } from "react";
import { supabase } from "@/lib/supabase";
import { Card } from "@/components/ui";

type Lesson = { id: string; lesson: string; kind: string | null; evidence: number; updated_at: string };
type Decision = { id: string; status: string; created_at: string; share: number; reasons: string[] };

const SOURCES = {
  market_intel: { table: "drafts", cols: "id,status,created_at,post_md,edited_post_md,feedback",
    text: (d: Record<string, string>) => [d.post_md, d.edited_post_md] as const },
  substack: { table: "substack_drafts", cols: "id,status,created_at,body_md,edited_body_md,feedback",
    text: (d: Record<string, string>) => [d.body_md, d.edited_body_md] as const },
};

/** Share of the draft's words the owner changed (0 = approved as written), from a word-level LCS. */
function editShare(a: string, b: string | null): number {
  if (!b || a.trim() === b.trim()) return 0;
  const x = a.split(/\s+/).slice(0, 1500), y = b.split(/\s+/).slice(0, 1500);
  let prev = new Array(y.length + 1).fill(0);
  for (const w of x) {
    const cur = [0];
    for (let j = 0; j < y.length; j++) cur.push(w === y[j] ? prev[j] + 1 : Math.max(prev[j + 1], cur[j]));
    prev = cur;
  }
  return 1 - (2 * prev[y.length]) / (x.length + y.length);
}

export function Learning({ agent, canEdit }: { agent: "market_intel" | "substack"; canEdit: boolean }) {
  const [lessons, setLessons] = useState<Lesson[]>([]);
  const [decisions, setDecisions] = useState<Decision[] | null>(null);

  const load = useCallback(async () => {
    const { data } = await supabase.from("lessons").select("id,lesson,kind,evidence,updated_at")
      .eq("agent", agent).eq("active", true).order("evidence", { ascending: false }).order("updated_at", { ascending: false });
    setLessons((data ?? []) as Lesson[]);
    if (!canEdit) return; // drafts are owner-only
    const src = SOURCES[agent];
    const { data: rows } = await supabase.from(src.table).select(src.cols).neq("status", "pending")
      .order("created_at", { ascending: false }).limit(20);
    setDecisions(((rows ?? []) as unknown as Record<string, string>[]).reverse().map((d) => {
      const [draft, final] = src.text(d);
      const fb = (d.feedback ?? {}) as unknown as { reasons?: string[] };
      return { id: d.id, status: d.status, created_at: d.created_at, share: d.status === "rejected" ? 1 : editShare(draft, final),
        reasons: fb.reasons ?? [] };
    }));
  }, [agent, canEdit]);
  useEffect(() => { load(); }, [load]);

  const forget = async (id: string) => {
    await supabase.from("lessons").update({ active: false, updated_at: new Date().toISOString() }).eq("id", id);
    load();
  };

  const good = (ds: Decision[]) => ds.length ? ds.filter((d) => d.status !== "rejected" && d.share < 0.15).length / ds.length : 0;
  const recent = decisions?.slice(-5) ?? [], before = decisions?.slice(-10, -5) ?? [];

  return (
    <Card>
      <div className="flex flex-wrap items-baseline gap-3">
        <h3 className="font-semibold">Lessons from your feedback</h3>
        <span className="text-sm text-muted">from your approvals, edits and reject reasons; the agent reads them at the start of every run</span>
      </div>
      {decisions && decisions.length > 0 && (
        <div className="mt-3 space-y-1 text-sm">
          <div className="flex items-center gap-1" aria-label="Last decisions, oldest first">
            {decisions.map((d) => (
              <span key={d.id} title={`${d.created_at.slice(0, 10)}: ${d.status === "rejected" ? `rejected${d.reasons.length ? ` (${d.reasons.join(", ")})` : ""}`
                : d.share === 0 ? "approved as written" : `approved, ${Math.round(d.share * 100)}% edited`}`}
                className={`flex h-3.5 w-3.5 items-center justify-center rounded-full text-[9px] leading-none ${d.status === "rejected"
                  ? "border border-ink text-ink" : d.share < 0.15 ? "bg-ink" : "border border-ink bg-[linear-gradient(90deg,var(--ink)_50%,transparent_50%)]"}`}>
                {d.status === "rejected" ? "✕" : ""}</span>
            ))}
            <span className="ml-2 text-xs text-muted">oldest to newest · ● kept (15% edited or less) · ◐ rewritten by you · ✕ rejected</span>
          </div>
          {before.length === 5 ? (
            <p className="text-ink-2">Kept with light edits: <b className="num">{Math.round(good(recent) * 100)}%</b> of the last 5
              vs <span className="num">{Math.round(good(before) * 100)}%</span> of the 5 before{good(recent) > good(before) ? ", up" : good(recent) < good(before) ? ", down" : ""}.</p>
          ) : <p className="text-ink-2">The trend shows after 10 decisions ({decisions.length} so far).</p>}
        </div>
      )}
      {lessons.length === 0 ? (
        <p className="mt-3 text-sm text-ink-2">Nothing yet. Approve, edit or reject a draft (with a reason) and the next run learns from it.</p>
      ) : (
        <ul className="mt-3 divide-y divide-line text-sm">
          {lessons.map((l) => (
            <li key={l.id} className="flex items-start gap-3 py-2">
              <span className="w-20 shrink-0 text-xs uppercase tracking-wide text-muted">{l.kind ?? "voice"}</span>
              <span className="flex-1">{l.lesson}</span>
              <span className="num shrink-0 text-xs text-muted" title="decisions that back it up">{l.evidence}×</span>
              {canEdit && <button onClick={() => forget(l.id)} className="shrink-0 text-xs text-ink-2 hover:underline">Forget</button>}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
