"use client";
// Your edit of a LinkedIn draft, scored next to the agent's own version by the same reviewers on the same sources
// (agents/market_intel/score_edit.py): what each score did, which flags your edit fixed or added, and the words you changed.

type Report = {
  scores: Record<string, number>; humanizer: number; humanizer_why?: string[]; overall: number; weak: boolean;
  violations: string[]; fixes: { kind: string; quote: string; problem: string; fix: string }[];
  unexplained?: string[]; unsupported?: string[]; hedges?: string[];
};
export type EditCheck = { text: string; scored_at: string; run_id: string; cost_usd?: number; error?: string; agent?: Report; edit?: Report };

/** Word-level diff (LCS): runs of kept, removed and added words. */
export function wordDiff(a: string, b: string): { op: "same" | "del" | "add"; text: string }[] {
  const x = a.split(/(\s+)/), y = b.split(/(\s+)/);
  const m = x.length, n = y.length;
  const L = Array.from({ length: m + 1 }, () => new Array<number>(n + 1).fill(0));
  for (let i = m - 1; i >= 0; i--) for (let j = n - 1; j >= 0; j--)
    L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out: { op: "same" | "del" | "add"; text: string }[] = [];
  const push = (op: "same" | "del" | "add", text: string) => {
    const last = out[out.length - 1];
    if (last && last.op === op) last.text += text; else out.push({ op, text });
  };
  let i = 0, j = 0;
  while (i < m && j < n) {
    if (x[i] === y[j]) { push("same", x[i]); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) push("del", x[i++]);
    else push("add", y[j++]);
  }
  while (i < m) push("del", x[i++]);
  while (j < n) push("add", y[j++]);
  return out;
}

function Change({ before, after, digits = 0 }: { before?: number; after?: number; digits?: number }) {
  if (before == null || after == null) return <span className="text-muted">–</span>;
  const d = Number((after - before).toFixed(digits || 1));
  if (d === 0) return <span className="text-muted">same</span>;
  return <span className="font-semibold">{d > 0 ? "▲ +" : "▼ "}{digits ? d.toFixed(digits) : d}<span className="sr-only">{d > 0 ? " better" : " worse"}</span></span>;
}

const flagsOf = (r?: Report) => [...(r?.violations ?? []), ...(r?.unsupported ?? []).map((c) => `unsupported claim: ${c}`),
  ...(r?.unexplained?.length ? [`unexplained terms: ${r.unexplained.join(", ")}`] : [])];

export function EditScore({ check, text, original }: { check: EditCheck; text: string; original: string }) {
  const stale = check.text.trim() !== text.trim();
  if (check.error) return <p className="text-sm text-ink-2">Scoring your edit: {check.error}</p>;
  const { agent: a, edit: e } = check;
  if (!a || !e) return null;
  const dims = Array.from(new Set([...Object.keys(a.scores), ...Object.keys(e.scores)]));
  const before = flagsOf(a), after = flagsOf(e);
  const fixed = before.filter((f) => !after.includes(f)), added = after.filter((f) => !before.includes(f));
  const kept = after.filter((f) => before.includes(f));
  const scored = check.text.trim() === text.trim() ? text : check.text;

  return (
    <div className="mt-4 rounded-lg border border-line p-4 text-sm">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 className="font-semibold">Your edit vs the agent&apos;s draft</h3>
        <span className="text-xs text-muted">same reviewers, same sources · scored {new Date(check.scored_at).toLocaleString()}
          {check.cost_usd != null && ` · $${check.cost_usd.toFixed(3)}`}</span>
      </div>
      {stale && <p className="mt-1 text-ink font-semibold">You&apos;ve changed the text since this was scored. Score it again to see the latest.</p>}

      <div className="mt-3 grid gap-6 md:grid-cols-2">
        <table className="w-full">
          <thead><tr className="text-left text-ink-2"><th className="font-normal">Score</th><th className="font-normal">Agent</th>
            <th className="font-normal">Yours</th><th className="font-normal">Change</th></tr></thead>
          <tbody className="num">
            <tr className="border-t border-line font-semibold"><td className="py-1">overall</td><td>{a.overall.toFixed(2)}</td><td>{e.overall.toFixed(2)}</td>
              <td><Change before={a.overall} after={e.overall} digits={2} /></td></tr>
            <tr className="border-t border-line"><td className="py-1">humanizer</td><td>{a.humanizer}</td><td>{e.humanizer}</td><td><Change before={a.humanizer} after={e.humanizer} /></td></tr>
            {dims.map((k) => (
              <tr key={k} className="border-t border-line"><td className="py-1">{k}</td>
                <td className={(a.scores[k] ?? 5) < 4 ? "font-semibold" : ""}>{a.scores[k] ?? "–"}</td>
                <td className={(e.scores[k] ?? 5) < 4 ? "font-semibold" : ""}>{e.scores[k] ?? "–"}</td>
                <td><Change before={a.scores[k]} after={e.scores[k]} /></td></tr>
            ))}
            <tr className="border-t border-line"><td className="py-1">would send back</td><td>{a.weak ? "yes" : "no"}</td><td>{e.weak ? "yes" : "no"}</td><td /></tr>
          </tbody>
        </table>

        <div className="space-y-3">
          {fixed.length > 0 && <div><div className="font-medium">✓ Your edit fixed</div>
            <ul className="list-disc pl-4 text-ink-2">{fixed.map((f) => <li key={f}>{f}</li>)}</ul></div>}
          {added.length > 0 && <div><div className="font-medium">! Your edit added</div>
            <ul className="list-disc pl-4">{added.map((f) => <li key={f}>{f}</li>)}</ul></div>}
          {kept.length > 0 && <div><div className="font-medium">Still flagged</div>
            <ul className="list-disc pl-4 text-ink-2">{kept.map((f) => <li key={f}>{f}</li>)}</ul></div>}
          {!before.length && !after.length && <p className="text-ink-2">No guardrail or fact-check flags on either version.</p>}
          {(e.hedges ?? []).length > 0 && <div><div className="font-medium">Could be stated more carefully</div>
            <ul className="list-disc pl-4 text-ink-2">{e.hedges!.map((h) => <li key={h}>{h}</li>)}</ul></div>}
          {e.humanizer < a.humanizer && (e.humanizer_why ?? []).length > 0 && <div><div className="font-medium">Why the humanizer score dropped</div>
            <ul className="list-disc pl-4 text-ink-2">{e.humanizer_why!.map((h) => <li key={h}>{h}</li>)}</ul></div>}
          {e.fixes.length > 0 && <div><div className="font-medium">The editor on your edit</div>
            <ul className="list-disc pl-4 text-ink-2">{e.fixes.map((f, n) => (
              <li key={n}><b>{f.kind}</b>{f.quote && <>: &ldquo;{f.quote}&rdquo;</>} {f.problem} → {f.fix}</li>))}</ul></div>}
        </div>
      </div>

      <details className="mt-3">
        <summary className="cursor-pointer text-ink-2">What you changed</summary>
        <p className="mt-2 whitespace-pre-wrap leading-relaxed">
          {wordDiff(original, scored).map((p, n) => p.op === "same" ? <span key={n}>{p.text}</span>
            : p.op === "del" ? <del key={n} className="text-muted line-through" aria-label="removed">{p.text}</del>
            : <ins key={n} className="font-semibold underline underline-offset-2" aria-label="added">{p.text}</ins>)}
        </p>
        <p className="mt-1 text-xs text-muted">struck through = the agent&apos;s words you removed · underlined = words you added</p>
      </details>
    </div>
  );
}
