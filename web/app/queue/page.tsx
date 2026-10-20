"use client";
import { useCallback, useEffect, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase, configured } from "@/lib/supabase";
import type { Draft } from "@/lib/types";
import { when } from "@/lib/format";
import { Card, NotConfigured, Status } from "@/components/ui";
import { SERIES } from "@/lib/linkedin";

const MAX_CHARS = 1300;

export default function Queue() {
  const [session, setSession] = useState<Session | null>(null);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [msg, setMsg] = useState("");
  const [li, setLi] = useState<{ connected: boolean; name?: string; daysLeft?: number } | null>(null);

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => setSession(data.session));
    const { data } = supabase.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => data.subscription.unsubscribe();
  }, []);

  const load = useCallback(async () => {
    const { data, error } = await supabase.from("drafts").select("*").order("created_at", { ascending: false }).limit(10);
    if (error) setMsg(error.message);
    setDrafts((data ?? []) as Draft[]);
  }, []);
  useEffect(() => { if (session) load(); }, [session, load]);
  useEffect(() => {
    if (!session) return;
    fetch("/api/linkedin/status", { headers: { Authorization: `Bearer ${session.access_token}` } })
      .then((r) => (r.ok ? r.json() : null)).then(setLi);
    const result = new URLSearchParams(window.location.search).get("linkedin");
    if (result) {
      setMsg(result === "connected" ? "LinkedIn connected." : `LinkedIn: ${result}`);
      window.history.replaceState(null, "", "/queue");
    }
  }, [session]);

  if (!configured) return <NotConfigured />;
  if (!session) return <SignIn />;

  const regenerate = async () => {
    setMsg("Starting the weekly workflow…");
    const res = await fetch("/api/regenerate", { method: "POST", headers: { Authorization: `Bearer ${session.access_token}` } });
    setMsg(res.ok ? "Workflow started. The new draft shows up here in ~2 minutes." : `Failed: ${(await res.json()).error}`);
  };

  const connectLinkedIn = async () => {
    const res = await fetch("/api/linkedin/start", { method: "POST", headers: { Authorization: `Bearer ${session.access_token}` } });
    const body = await res.json();
    if (body.url) window.location.href = body.url; else setMsg(`LinkedIn: ${body.error}`);
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-semibold">Approval queue</h1>
        <button onClick={regenerate} className="ml-auto rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-card">Generate a new draft</button>
        <button onClick={() => supabase.auth.signOut()} className="text-sm text-ink-2 hover:underline">Sign out</button>
      </div>
      {li && (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-line px-4 py-2 text-sm">
          {li.connected ? (
            <span><span className="text-good">✓</span> LinkedIn connected as <b>{li.name}</b>
              <span className={li.daysLeft! <= 7 ? " text-warn" : " text-muted"}> · reconnect in {li.daysLeft} days</span></span>
          ) : <span className="text-ink-2">LinkedIn not connected. Publishing posts directly needs a one-time connection.</span>}
          <button onClick={connectLinkedIn} className="ml-auto rounded-lg border border-line px-3 py-1 hover:bg-card">
            {li.connected ? "Reconnect" : "Connect LinkedIn"}
          </button>
        </div>
      )}
      {msg && <p className="text-sm text-ink-2">{msg}</p>}
      {drafts.length === 0 && <Card><p className="text-ink-2">No drafts yet. The weekly run creates one every Friday.</p></Card>}
      {drafts.map((d) => <DraftCard key={d.id} draft={d} onChange={load} token={session.access_token} canPublish={!!li?.connected}
        nextSeries={Math.max(0, ...drafts.map((x) => x.series_no ?? 0)) + 1} />)}
    </div>
  );
}

function DraftCard({ draft, onChange, token, canPublish, nextSeries }:
  { draft: Draft; onChange: () => void; token: string; canPublish: boolean; nextSeries: number }) {
  const [pubMsg, setPubMsg] = useState("");
  const [text, setText] = useState(draft.edited_post_md ?? draft.post_md);
  const [preview, setPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  const pending = draft.status === "pending";
  const words = text.trim().split(/\s+/).filter(Boolean).length;
  // "accent" is the old 0-100 humanizer score on early drafts; "humanizer" is the same check on the 1-5 scale.
  const scores = (Object.entries(draft.judge_scores ?? {}).filter(([k, v]) => typeof v === "number" && !["accent", "overall"].includes(k)) as [string, number][])
    .sort(([a], [b]) => (a === "humanizer" ? -1 : b === "humanizer" ? 1 : 0));
  const overall = typeof draft.judge_scores?.overall === "number" ? (draft.judge_scores.overall as number) : null;
  const violations = (draft.judge_scores?.violations as string[] | undefined) ?? [];
  const onepagerUrl = typeof window !== "undefined" ? `${window.location.origin}/p/${draft.week}` : "";

  const decide = async (status: "approved" | "rejected") => {
    setBusy(true);
    const edited = text !== draft.post_md ? text : null;
    await supabase.from("drafts").update({
      status, edited_post_md: edited, approved_at: status === "approved" ? new Date().toISOString() : null,
    }).eq("id", draft.id);
    setBusy(false);
    onChange();
  };

  const publish = async () => {
    if (!window.confirm("Publish this post to your LinkedIn profile now? It will be public.")) return;
    setBusy(true);
    setPubMsg("Publishing…");
    const res = await fetch("/api/linkedin/publish", {
      method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ draftId: draft.id }),
    });
    const body = await res.json();
    setBusy(false);
    setPubMsg(res.ok ? `Published as ${SERIES} #${body.seriesNo}. First comment ${body.comment}.` : `Failed: ${body.error}`);
    onChange();
  };

  return (
    <Card>
      <div className="flex flex-wrap items-baseline gap-3">
        <h2 className="font-semibold">{draft.week}</h2>
        <Status status={draft.status} />
        <span className="text-sm text-muted">{when(draft.created_at)} · {draft.revisions} revision{draft.revisions === 1 ? "" : "s"}</span>
        <a href={`/runs/${draft.run_id}`} className="ml-auto text-sm text-accent hover:underline">run details</a>
      </div>
      {draft.angle?.angle && <p className="mt-2 text-sm text-ink-2"><b>Angle:</b> {draft.angle.angle}</p>}
      {draft.judge_scores?.passed_review === false && (
        <p className="mt-2 text-sm text-warn"><b>! Didn&apos;t pass review</b> after {draft.revisions} rewrites. Edit it, reject it, or generate a new one.</p>
      )}
      {typeof draft.judge_scores?.critique === "string" && draft.judge_scores.critique && (
        <p className="mt-2 text-sm text-ink-2"><b>Critic:</b> {draft.judge_scores.critique}</p>
      )}

      <div className="mt-4 grid gap-6 md:grid-cols-[1fr_220px]">
        <div>
          <textarea value={text} onChange={(e) => setText(e.target.value)} disabled={!pending} rows={14}
            className="w-full rounded-lg border border-line bg-bg p-3 text-[15px] leading-relaxed outline-none focus:border-accent" />
          <div className={`mt-1 text-xs num ${text.length > MAX_CHARS ? "text-bad" : "text-muted"}`}>
            {text.length} / {MAX_CHARS} characters · {words} words
            {draft.series_no ? ` · published as ${SERIES} #${draft.series_no}`
              : draft.status !== "rejected" && ` · publishes as ${SERIES} #${nextSeries} (added as the last line)`}
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            {pending ? (
              <>
                <button disabled={busy || text.length > MAX_CHARS} onClick={() => decide("approved")} className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-50">
                  Approve{text !== draft.post_md ? " with edits" : ""}
                </button>
                <button disabled={busy} onClick={() => decide("rejected")} className="rounded-lg border border-line px-4 py-2 text-sm">Reject</button>
              </>
            ) : draft.status === "approved" && (
              <>
                {draft.linkedin_url ? (
                  <a href={draft.linkedin_url} target="_blank" rel="noopener" className="rounded-lg bg-good px-4 py-2 text-sm font-medium text-white">
                    ✓ {draft.series_no ? `${SERIES} #${draft.series_no}` : "Live"} on LinkedIn · view post
                  </a>
                ) : (
                  <button disabled={busy || !canPublish} onClick={publish} title={canPublish ? "" : "Connect LinkedIn first"}
                    className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-50">Publish to LinkedIn</button>
                )}
                <button onClick={() => navigator.clipboard.writeText(text)} className="rounded-lg border border-line px-4 py-2 text-sm">Copy post</button>
                <button onClick={() => navigator.clipboard.writeText(`Sources and the full breakdown: ${onepagerUrl}`)} className="rounded-lg border border-line px-4 py-2 text-sm">Copy first comment</button>
                <a href="https://www.linkedin.com/feed/?shareActive=true" target="_blank" rel="noopener" className="rounded-lg border border-line px-4 py-2 text-sm">Open LinkedIn</a>
              </>
            )}
            {pubMsg && <span className="self-center text-sm text-ink-2">{pubMsg}</span>}
            <button onClick={() => setPreview(!preview)} className="rounded-lg px-3 py-2 text-sm text-ink-2 hover:underline">
              {preview ? "Hide" : "Preview"} one-pager
            </button>
          </div>
        </div>

        <aside className="space-y-4 text-sm">
          <div>
            {overall !== null && (
              <div className="mb-3">
                <div className="text-ink-2">Overall</div>
                <div className={`num text-2xl font-semibold ${overall < 4 ? "text-warn" : ""}`}>{overall.toFixed(1)}/5</div>
                <div className="text-xs text-muted">humanizer 40% · other scores share 60%</div>
              </div>
            )}
            <div className="mb-1 text-ink-2">Scores <span className="text-muted">(1–5, 5 is best)</span></div>
            {scores.map(([k, v]) => (
              <div key={k} className="flex items-center justify-between border-t border-line py-1">
                <span>{k}</span>
                <span className={`num font-medium ${v < 4 ? "text-warn" : ""}`}>{v}/5</span>
              </div>
            ))}
          </div>
          {violations.length > 0 && (
            <div className="text-warn"><div className="font-medium">! Guardrail flags</div>
              <ul className="list-disc pl-4">{violations.map((v) => <li key={v}>{v}</li>)}</ul></div>
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
      {preview && <iframe title="One-pager preview" srcDoc={draft.onepager_html} sandbox="" className="mt-4 h-[70vh] w-full rounded-lg border border-line" />}
    </Card>
  );
}

function SignIn() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const send = async () => {
    const { error } = await supabase.auth.signInWithOtp({ email, options: { emailRedirectTo: `${window.location.origin}/queue` } });
    if (!error) setSent(true);
  };
  return (
    <Card className="mx-auto max-w-md">
      <h1 className="text-xl font-semibold">Approval queue</h1>
      <p className="mt-1 text-sm text-ink-2">Owner only. Drafts are visible and editable only to the approved account (enforced by row-level security).</p>
      {sent ? <p className="mt-4 text-sm">Check your email for a sign-in link.</p> : (
        <div className="mt-4 flex gap-2">
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com"
            className="flex-1 rounded-lg border border-line bg-bg px-3 py-2 text-sm outline-none focus:border-accent" />
          <button onClick={send} className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white">Send link</button>
        </div>
      )}
    </Card>
  );
}
