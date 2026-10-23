"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { supabase } from "@/lib/supabase";
import type { SubstackDraft } from "@/lib/types";
import { when } from "@/lib/format";
import { previewPage, renderMarkdown, words } from "@/lib/markdown";
import { Card, Status } from "@/components/ui";
import { RejectButton, TakeBox, type Feedback } from "@/components/Feedback";

const SUBSTACK = process.env.NEXT_PUBLIC_SUBSTACK_URL?.replace(/\/$/, "");
const DIMENSIONS = ["hook", "thesis", "structure", "depth", "concreteness", "voice", "human"];

export function SubstackQueue({ token }: { token: string }) {
  const [drafts, setDrafts] = useState<SubstackDraft[]>([]);
  const [msg, setMsg] = useState("");

  const load = useCallback(async () => {
    const { data, error } = await supabase.from("substack_drafts").select("*").order("created_at", { ascending: false }).limit(10);
    if (error) setMsg(error.message);
    setDrafts((data ?? []) as SubstackDraft[]);
  }, []);
  useEffect(() => { load(); }, [load]);

  const run = async () => {
    setMsg("Starting the Substack newsroom…");
    const res = await fetch("/api/regenerate", {
      method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ workflow: "substack.yml" }),
    });
    setMsg(res.ok ? "Newsroom started. Watch it live under Agents; the draft lands here in ~5 minutes." : `Failed: ${(await res.json()).error}`);
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-sm text-ink-2">Two posts a week (Wed + Sat), written by the newsroom. Substack has no API: approve, then copy into the editor.</p>
        <button onClick={run} className="ml-auto rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-card">Run the newsroom now</button>
      </div>
      {msg && <p className="text-sm text-ink-2">{msg}</p>}
      {drafts.length === 0 && <Card><p className="text-ink-2">No Substack drafts yet. The newsroom runs every Wednesday and Saturday.</p></Card>}
      {drafts.map((d) => <SubstackCard key={d.id} draft={d} onChange={load} token={token} />)}
    </div>
  );
}

function download(name: string, text: string, type: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

function SubstackCard({ draft, onChange, token }: { draft: SubstackDraft; onChange: () => void; token: string }) {
  const [title, setTitle] = useState(draft.edited_title ?? draft.title);
  const [subtitle, setSubtitle] = useState(draft.edited_subtitle ?? draft.subtitle ?? "");
  const [body, setBody] = useState(draft.edited_body_md ?? draft.body_md);
  const [preview, setPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const pending = draft.status === "pending";
  const html = useMemo(() => renderMarkdown(body), [body]);
  const n = words(body.split(/^## Sources\s*$/m)[0]);
  const edited = title !== draft.title || subtitle !== (draft.subtitle ?? "") || body !== draft.body_md;
  const js = draft.judge_scores ?? {};
  const overall = typeof js.overall === "number" ? js.overall : null;
  const violations = (js.violations as string[] | undefined) ?? [];
  const claims = draft.fact_report?.claims ?? [];
  const count = (v: string) => v === "supported" && typeof draft.fact_report?.supported === "number"
    ? draft.fact_report.supported : claims.filter((c) => c.verdict === v).length;

  const save = async (patch: Partial<SubstackDraft>) => {
    setBusy(true);
    const { error } = await supabase.from("substack_drafts").update(patch).eq("id", draft.id);
    setBusy(false);
    if (error) setNote(error.message); else onChange();
  };
  const decide = (status: "approved" | "rejected", feedback: Feedback | null = null) => save({
    status, approved_at: status === "approved" ? new Date().toISOString() : null, feedback,
    edited_title: title !== draft.title ? title : null, edited_subtitle: subtitle !== (draft.subtitle ?? "") ? subtitle : null,
    edited_body_md: body !== draft.body_md ? body : null,
  });
  const copyRich = async () => {
    try {
      await navigator.clipboard.write([new ClipboardItem({
        "text/html": new Blob([html], { type: "text/html" }), "text/plain": new Blob([body], { type: "text/plain" }),
      })]);
      setNote("Copied. Paste into the Substack editor's body; headings, links and images come along.");
    } catch {
      await navigator.clipboard.writeText(body);
      setNote("This browser blocked rich copy, so the markdown was copied instead.");
    }
  };
  const fixIt = async (take?: string) => {
    const n = draft.final_report?.blockers?.length ?? 0;
    if (!window.confirm((take ? "Send this draft back to the agents to rewrite it around your take? "
      : `Send this draft back to the agents to fix ${n} blocker${n === 1 ? "" : "s"}? `)
      + `${edited ? "Your unsaved edits will be saved first and used as the starting point. " : ""}`
      + "It takes ~3-5 minutes and costs about $0.06-0.12.")) return;
    setBusy(true);
    if (take) await supabase.from("substack_drafts").update({ my_take: take }).eq("id", draft.id);
    if (edited) await supabase.from("substack_drafts").update({
      edited_title: title !== draft.title ? title : null, edited_subtitle: subtitle !== (draft.subtitle ?? "") ? subtitle : null,
      edited_body_md: body !== draft.body_md ? body : null }).eq("id", draft.id);
    const res = await fetch("/api/regenerate", {
      method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ workflow: "substack.yml", revise_draft_id: draft.id }),
    });
    setBusy(false);
    setNote(res.ok ? "The agents are on it. Watch them on the Agents page; this draft updates in ~3-5 minutes (refresh)."
      : `Failed: ${(await res.json()).error}`);
  };
  const markPublished = () => {
    const url = window.prompt("Paste the published Substack URL");
    if (url && /^https:\/\/\S+$/.test(url)) save({ status: "published", published_url: url });
  };

  return (
    <Card>
      <div className="flex flex-wrap items-baseline gap-3">
        <h2 className="font-semibold">{draft.slot}</h2>
        <Status status={draft.status} />
        <span className="text-sm text-muted">{when(draft.created_at)} · {draft.revisions} rewrite{draft.revisions === 1 ? "" : "s"}
          {draft.seed_draft_id && " · expands a LinkedIn musing"}</span>
        <a href={`/runs/${draft.run_id}`} className="ml-auto text-sm text-accent hover:underline">agent timeline</a>
      </div>
      {js.passed_review === false && (
        <div className="mt-2 flex flex-wrap items-center gap-3 text-sm text-warn">
          <span><b>! Didn&apos;t pass the final check.</b> Fix the blockers yourself, or send it back to the agents.</span>
          {pending && <button disabled={busy} onClick={() => fixIt()}
            className="rounded-lg border border-accent px-3 py-1 font-medium text-accent hover:bg-bg disabled:opacity-50">Let the agents fix this</button>}
        </div>
      )}

      <div className="mt-4 grid gap-6 md:grid-cols-[1fr_240px]">
        <div className="min-w-0 space-y-2">
          <input value={title} onChange={(e) => setTitle(e.target.value)} disabled={!pending} aria-label="Title"
            className="w-full rounded-lg border border-line bg-bg px-3 py-2 text-lg font-semibold outline-none focus:border-accent" />
          <input value={subtitle} onChange={(e) => setSubtitle(e.target.value)} disabled={!pending} aria-label="Subtitle"
            className="w-full rounded-lg border border-line bg-bg px-3 py-2 text-ink-2 outline-none focus:border-accent" />
          {(draft.images ?? []).length > 0 && (
            <div className="flex gap-2 overflow-x-auto py-1">
              {(draft.images ?? []).filter((i) => i.url.startsWith("https://")).map((i) => (
                <a key={i.url} href={i.url} target="_blank" rel="noopener" title={`${i.kind}: ${i.alt}`} className="shrink-0">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={i.url} alt={i.alt} className="h-20 rounded-md border border-line object-cover" />
                  <span className="block text-center text-[11px] text-muted">{i.kind}{i.check?.score ? ` · ${i.check.score}/5` : ""}</span>
                </a>
              ))}
            </div>
          )}
          <textarea value={body} onChange={(e) => setBody(e.target.value)} disabled={!pending} rows={22} aria-label="Post body (markdown)"
            className="w-full rounded-lg border border-line bg-bg p-3 font-mono text-[13px] leading-relaxed outline-none focus:border-accent" />
          <div className={`text-xs num ${n < 700 || n > 1350 ? "text-warn" : "text-muted"}`}>
            {n} words · {Math.max(1, Math.round(n / 230))} min read · images are markdown, so you can move or delete them
          </div>
          <div className="flex flex-wrap gap-2">
            {pending ? (
              <>
                <button disabled={busy} onClick={() => decide("approved")} className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-50">
                  Approve{edited ? " with edits" : ""}
                </button>
                <RejectButton busy={busy} onReject={(fb) => decide("rejected", fb)} />
                <TakeBox busy={busy} initial={draft.my_take} onRewrite={(t) => fixIt(t)} cost="~3-5 min · about $0.06-0.12" />
              </>
            ) : draft.status !== "rejected" && (
              <>
                <button onClick={copyRich} className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white">Copy for Substack</button>
                <button onClick={() => navigator.clipboard.writeText(title)} className="rounded-lg border border-line px-3 py-2 text-sm">Copy title</button>
                <button onClick={() => navigator.clipboard.writeText(subtitle)} className="rounded-lg border border-line px-3 py-2 text-sm">Copy subtitle</button>
                <button onClick={() => download(`${draft.slot}.md`, `# ${title}\n\n*${subtitle}*\n\n${body}\n`, "text/markdown")}
                  className="rounded-lg border border-line px-3 py-2 text-sm">.md</button>
                <button onClick={() => download(`${draft.slot}.html`, previewPage(title, subtitle, html), "text/html")}
                  className="rounded-lg border border-line px-3 py-2 text-sm">.html</button>
                {SUBSTACK && <a href={`${SUBSTACK}/publish/post`} target="_blank" rel="noopener" className="rounded-lg border border-line px-3 py-2 text-sm">Open Substack</a>}
                {draft.status === "published" && draft.published_url
                  ? <a href={draft.published_url} target="_blank" rel="noopener" className="rounded-lg bg-good px-4 py-2 text-sm font-medium text-white">✓ Live · view post</a>
                  : <button onClick={markPublished} className="rounded-lg border border-line px-3 py-2 text-sm">Mark published</button>}
              </>
            )}
            <button onClick={() => setPreview(!preview)} className="rounded-lg px-3 py-2 text-sm text-ink-2 hover:underline">{preview ? "Hide" : "Preview"}</button>
            {note && <span className="self-center text-sm text-ink-2">{note}</span>}
          </div>
        </div>

        <aside className="space-y-4 text-sm">
          {overall !== null && (
            <div>
              <div className="text-ink-2">Overall</div>
              <div className={`num text-2xl font-semibold ${overall < 4 ? "text-warn" : ""}`}>{overall.toFixed(1)}/5</div>
              <div className="text-xs text-muted">humanizer 40% · editor&apos;s rubric 60%</div>
            </div>
          )}
          <div>
            <div className="mb-1 text-ink-2">Editor <span className="text-muted">(1–5)</span></div>
            {["humanizer", ...DIMENSIONS].filter((k) => typeof js[k] === "number").map((k) => (
              <div key={k} className="flex justify-between border-t border-line py-1">
                <span>{k}</span><span className={`num font-medium ${(js[k] as number) < 4 ? "text-warn" : ""}`}>{js[k] as number}/5</span>
              </div>
            ))}
          </div>
          {draft.fact_report?.claims && (
            <div>
              <div className="mb-1 text-ink-2">Fact check</div>
              <p className="num">{count("supported")} supported · {count("hedge")} hedged · <span className={count("unsupported") ? "text-bad" : ""}>{count("unsupported")} unsupported</span></p>
            </div>
          )}
          {(draft.final_report?.blockers?.length || violations.length) ? (
            <div className="text-warn"><div className="font-medium">! Blockers</div>
              <ul className="list-disc pl-4">{[...new Set([...(draft.final_report?.blockers ?? []), ...violations])].map((b) => <li key={b}>{b}</li>)}</ul></div>
          ) : draft.final_report?.passed && <p className="text-good">✓ Passed every check</p>}
          {(draft.final_report?.polish?.length ?? 0) > 0 && (
            <div><div className="mb-1 text-ink-2">Polish ideas</div><ul className="list-disc pl-4 text-ink-2">{draft.final_report!.polish!.map((p) => <li key={p}>{p}</li>)}</ul></div>
          )}
          <div>
            <div className="mb-1 text-ink-2">Sources</div>
            <ul className="space-y-1">
              {(draft.sources ?? []).map((s) => (
                <li key={s.url}><a href={s.url} target="_blank" rel="noopener" className="hover:underline">{s.title}</a> <span className="text-muted">· {s.source}{s.origin === "brief" && " · via your AI Stack Brief"}</span></li>
              ))}
            </ul>
          </div>
        </aside>
      </div>
      {preview && <iframe title="Post preview" srcDoc={previewPage(title, subtitle, html)} sandbox="" className="mt-4 h-[75vh] w-full rounded-lg border border-line bg-white" />}
    </Card>
  );
}
