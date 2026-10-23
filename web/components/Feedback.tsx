"use client";
// The owner's feedback on a draft: why it was rejected, and their own take for a rewrite. Both feed the
// learning loop (core/lessons.py), which the agents read at the start of their next run.
import { useState } from "react";

export const REASONS = ["Sounds like AI", "Recap, no real take", "Not my opinion", "Dull topic", "Weak hook",
  "Wrong or shaky fact", "Too long"];
export type Feedback = { reasons: string[]; note?: string };

export function RejectButton({ busy, onReject }: { busy: boolean; onReject: (fb: Feedback) => void }) {
  const [open, setOpen] = useState(false);
  const [reasons, setReasons] = useState<string[]>([]);
  const [note, setNote] = useState("");
  if (!open) return <button disabled={busy} onClick={() => setOpen(true)} className="rounded-lg border border-line px-4 py-2 text-sm">Reject</button>;
  const toggle = (r: string) => setReasons(reasons.includes(r) ? reasons.filter((x) => x !== r) : [...reasons, r]);
  return (
    <div className="w-full space-y-2 rounded-lg border border-line p-3">
      <div className="text-sm text-ink-2">What was wrong? The agent learns from this before its next draft.</div>
      <div className="flex flex-wrap gap-1.5">
        {REASONS.map((r) => (
          <button key={r} onClick={() => toggle(r)} aria-pressed={reasons.includes(r)}
            className={`rounded-full border px-3 py-1 text-sm ${reasons.includes(r) ? "border-accent bg-accent text-white" : "border-line"}`}>{r}</button>
        ))}
      </div>
      <input value={note} onChange={(e) => setNote(e.target.value.slice(0, 300))} placeholder="Anything else? (optional)"
        className="w-full rounded-lg border border-line bg-bg px-3 py-1.5 text-sm outline-none focus:border-accent" />
      <div className="flex gap-2">
        <button disabled={busy} onClick={() => onReject({ reasons, ...(note.trim() ? { note: note.trim() } : {}) })}
          className="rounded-lg bg-bad px-4 py-1.5 text-sm font-medium text-white disabled:opacity-50">Reject draft</button>
        <button onClick={() => setOpen(false)} className="rounded-lg px-3 py-1.5 text-sm text-ink-2 hover:underline">Cancel</button>
      </div>
    </div>
  );
}

export function TakeBox({ busy, initial, onRewrite, cost }: { busy: boolean; initial: string | null; cost: string;
  onRewrite: (take: string) => void }) {
  const [take, setTake] = useState(initial ?? "");
  const [open, setOpen] = useState(!!initial);
  if (!open) return <button onClick={() => setOpen(true)} className="rounded-lg border border-line px-4 py-2 text-sm">Add my take</button>;
  return (
    <div className="w-full space-y-2 rounded-lg border border-line p-3">
      <label className="block text-sm text-ink-2" htmlFor="take">Your take, in your words. The agent rewrites the draft around it.</label>
      <textarea id="take" value={take} onChange={(e) => setTake(e.target.value.slice(0, 400))} rows={2}
        placeholder="e.g. The year-long reliability test is the whole story; the 1,500 picks an hour is the demo."
        className="w-full rounded-lg border border-line bg-bg p-2 text-sm outline-none focus:border-accent" />
      <div className="flex items-center gap-2">
        <button disabled={busy || take.trim().length < 10} onClick={() => onRewrite(take.trim())}
          className="rounded-lg bg-accent px-4 py-1.5 text-sm font-medium text-white disabled:opacity-50">Rewrite around my take</button>
        <span className="text-xs text-muted">{cost}</span>
      </div>
    </div>
  );
}
