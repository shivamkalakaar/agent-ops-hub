"use client";
import { useCallback, useEffect, useState } from "react";
import { supabase, configured } from "@/lib/supabase";
import type { Draft } from "@/lib/types";
import { when } from "@/lib/format";
import { Card, NotConfigured, Status } from "@/components/ui";
import { SERIES, withSeries } from "@/lib/linkedin";
import { SignIn, useSession } from "@/components/auth";
import { SubstackQueue } from "@/components/SubstackQueue";
import { RejectButton, TakeBox, type Feedback } from "@/components/Feedback";
const COMPOSER = "https://www.linkedin.com/feed/?shareActive=true";
const POST_URL = /^https:\/\/(www\.)?linkedin\.com\/(feed\/update|posts)\/\S+$/;

const MAX_CHARS = 1300;

export default function Queue() {
  const { session } = useSession();
  const [tab, setTab] = useState<"linkedin" | "substack">("linkedin");
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [msg, setMsg] = useState("");
  const [li, setLi] = useState<{ connected: boolean; name?: string; daysLeft?: number } | null>(null);

  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("tab") === "substack") setTab("substack");
  }, []);
  const pickTab = (t: "linkedin" | "substack") => {
    setTab(t);
    window.history.replaceState(null, "", t === "substack" ? "/queue?tab=substack" : "/queue");
  };

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
    setMsg("Starting the LinkedIn workflow…");
    const res = await fetch("/api/regenerate", { method: "POST", headers: { Authorization: `Bearer ${session.access_token}` } });
    setMsg(res.ok ? "Started. The new draft appears here in about 3 minutes." : `Failed: ${(await res.json()).error}`);
  };

  const expand = async (draftId: string) => {
    setMsg("Starting the Substack agent on this post…");
    const res = await fetch("/api/regenerate", {
      method: "POST", headers: { Authorization: `Bearer ${session.access_token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ workflow: "substack.yml", seed_draft_id: draftId }),
    });
    setMsg(res.ok ? "Started. The Substack draft appears in the Substack tab in about 5 minutes; the Agents page shows each step."
      : `Failed: ${(await res.json()).error}`);
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
        <div role="tablist" className="flex rounded-lg border border-line p-0.5 text-sm">
          {(["linkedin", "substack"] as const).map((t) => (
            <button key={t} role="tab" aria-selected={tab === t} onClick={() => pickTab(t)}
              className={`rounded-md px-3 py-1 ${tab === t ? "bg-card font-medium shadow-sm" : "text-ink-2"}`}>
              {t === "linkedin" ? "LinkedIn" : "Substack"}
            </button>
          ))}
        </div>
        {tab === "linkedin" && <button onClick={regenerate} className="ml-auto rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-card">Generate a new draft</button>}
        <button onClick={() => supabase.auth.signOut()} className="text-sm text-ink-2 hover:underline">Sign out</button>
      </div>
      {tab === "substack" && <SubstackQueue token={session.access_token} />}
      {tab === "linkedin" && li && (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-line px-4 py-2 text-sm">
          {li.connected ? (
            <span><span className="text-ink">✓</span> LinkedIn connected as <b>{li.name}</b>
              <span className={li.daysLeft! <= 7 ? " text-ink font-semibold" : " text-muted"}> · reconnect in {li.daysLeft} days</span></span>
          ) : <span className="text-ink-2">LinkedIn not connected. Publishing posts directly needs a one-time connection.</span>}
          <button onClick={connectLinkedIn} className="ml-auto rounded-lg border border-line px-3 py-1 hover:bg-card">
            {li.connected ? "Reconnect" : "Connect LinkedIn"}
          </button>
        </div>
      )}
      {msg && <p className="text-sm text-ink-2">{msg}</p>}
      {tab === "linkedin" && drafts.length === 0 && <Card><p className="text-ink-2">No drafts yet. A new one arrives every weekday morning (skipped while 2 are waiting).</p></Card>}
      {tab === "linkedin" && drafts.map((d) => <DraftCard key={d.id} draft={d} onChange={load} token={session.access_token} canPublish={!!li?.connected}
        nextSeries={Math.max(0, ...drafts.map((x) => x.series_no ?? 0)) + 1} onExpand={expand} />)}
    </div>
  );
}

function DraftCard({ draft, onChange, token, canPublish, nextSeries, onExpand }:
  { draft: Draft; onChange: () => void; token: string; canPublish: boolean; nextSeries: number; onExpand: (id: string) => void }) {
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

  const decide = async (status: "approved" | "rejected", feedback: Feedback | null = null) => {
    setBusy(true);
    const edited = text !== draft.post_md ? text : null;
    await supabase.from("drafts").update({
      status, edited_post_md: edited, approved_at: status === "approved" ? new Date().toISOString() : null, feedback,
    }).eq("id", draft.id);
    setBusy(false);
    onChange();
  };

  const rewrite = async (take: string) => {
    setBusy(true);
    await supabase.from("drafts").update({ my_take: take }).eq("id", draft.id);
    const res = await fetch("/api/regenerate", {
      method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ workflow: "weekly.yml", revise_draft_id: draft.id }),
    });
    setBusy(false);
    setPubMsg(res.ok ? "Rewriting around your take. This draft updates in about 3 minutes; refresh to see it." : `Failed: ${(await res.json()).error}`);
  };

  const [handedOff, setHandedOff] = useState(false);
  const [liveUrl, setLiveUrl] = useState("");
  const [finalText, setFinalText] = useState("");

  /** LinkedIn's API can't create scheduled posts, so hand the finished text to LinkedIn's own composer. */
  const scheduleInLinkedIn = () => {
    navigator.clipboard.writeText(withSeries(text, nextSeries)).catch(() => {});  // start the copy inside the click
    window.open(COMPOSER, "_blank", "noopener");
    setHandedOff(true);
    setPubMsg(`Copied, with ${SERIES} #${nextSeries} on the last line. In LinkedIn: paste (⌘V), edit if you like, then use the clock icon to schedule it.`);
  };

  /** Record a post that went out through LinkedIn, so the queue shows it live and the agent learns from the final text. */
  const markLive = async () => {
    setBusy(true);
    const changed = finalText.trim() && finalText.trim() !== withSeries(text, nextSeries).trim();
    const label = new RegExp(`^${SERIES} #\\d+\\s*$`, "m");
    await supabase.from("drafts").update({
      linkedin_url: liveUrl.trim(), published_at: new Date().toISOString(), series_no: nextSeries,
      ...(changed ? { edited_post_md: finalText.replace(label, "").replace(/\n{3,}/g, "\n\n").trim(), learned_at: null } : {}),
    }).eq("id", draft.id);
    setBusy(false);
    setPubMsg(changed ? "Saved as live, with your final text: the agent learns from your changes on its next run." : "Saved as live.");
    onChange();
  };

  const publish = async () => {
    if (!window.confirm("Publish this post to your LinkedIn profile now? It will be public. Weekday mornings usually reach more people, and editing it after it's live may cost reach.")) return;
    setBusy(true);
    setPubMsg("Publishing…");
    const res = await fetch("/api/linkedin/publish", {
      method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ draftId: draft.id }),
    });
    const body = await res.json();
    setBusy(false);
    setPubMsg(res.ok
      ? `Published as ${SERIES} #${body.seriesNo}.${body.tagged?.length ? ` Tagged ${body.tagged.join(", ")}.` : ""}`
        + `${body.tagsFailed ? " LinkedIn rejected the tags, so it went out untagged: check those company IDs." : ""} First comment ${body.comment}.`
      : `Failed: ${body.error}`);
    onChange();
  };

  return (
    <Card>
      <div className="flex flex-wrap items-baseline gap-3">
        <h2 className="font-semibold">{draft.week}</h2>
        <Status status={draft.status} />
        <span className="text-sm text-muted">{when(draft.created_at)} · {draft.revisions} revision{draft.revisions === 1 ? "" : "s"}</span>
        <a href={`/runs/${draft.run_id}`} className="ml-auto text-sm text-ink underline underline-offset-4 hover:underline">run details</a>
      </div>
      {draft.angle?.angle && <p className="mt-2 text-sm text-ink-2"><b>Angle:</b> {draft.angle.angle}</p>}
      {draft.judge_scores?.passed_review === false && (
        <p className="mt-2 text-sm text-ink font-semibold"><b>! Didn&apos;t pass the final check</b> after {draft.revisions} rewrite{draft.revisions === 1 ? "" : "s"}. Edit it, add your take, or reject it.</p>
      )}
      {typeof draft.judge_scores?.critique === "string" && draft.judge_scores.critique && (
        <p className="mt-2 text-sm text-ink-2"><b>Editor:</b> {draft.judge_scores.critique}</p>
      )}

      <div className="mt-4 grid gap-6 md:grid-cols-[1fr_220px]">
        <div>
          <textarea value={text} onChange={(e) => setText(e.target.value)} disabled={!pending} rows={14}
            className="w-full rounded-lg border border-line bg-bg p-3 text-[15px] leading-relaxed outline-none focus:border-ink" />
          {pending && <p className="mt-1 text-xs text-muted">Make your edits here before approving: what you approve is exactly what gets published, and changing a post once it&apos;s live may cost reach.</p>}
          <div className={`mt-1 text-xs num ${text.length > MAX_CHARS ? "text-ink font-semibold" : "text-muted"}`}>
            {text.length} / {MAX_CHARS} characters · {words} words
            {draft.series_no ? ` · published as ${SERIES} #${draft.series_no}`
              : draft.status !== "rejected" && ` · publishes as ${SERIES} #${nextSeries} (added as the last line)`}
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            {pending ? (
              <>
                <button disabled={busy || text.length > MAX_CHARS} onClick={() => decide("approved")} className="rounded-lg bg-ink px-4 py-2 text-sm font-medium text-bg disabled:opacity-50">
                  Approve{text !== draft.post_md ? " with edits" : ""}
                </button>
                <RejectButton busy={busy} onReject={(fb) => decide("rejected", fb)} />
                <TakeBox busy={busy} initial={draft.my_take} onRewrite={rewrite} cost="about 3 min · about $0.05" />
              </>
            ) : draft.status === "approved" && (
              <>
                {draft.linkedin_url ? (
                  <a href={draft.linkedin_url} target="_blank" rel="noopener" className="rounded-lg bg-ink px-4 py-2 text-sm font-medium text-bg">
                    ✓ {draft.series_no ? `${SERIES} #${draft.series_no}` : "Live"} on LinkedIn · view post
                  </a>
                ) : (
                  <>
                    <button disabled={busy} onClick={scheduleInLinkedIn}
                      className="rounded-lg bg-ink px-4 py-2 text-sm font-medium text-bg disabled:opacity-50">Schedule in LinkedIn</button>
                    <button disabled={busy || !canPublish} onClick={publish} title={canPublish ? "" : "Connect LinkedIn first"}
                      className="rounded-lg border border-line px-4 py-2 text-sm disabled:opacity-50">Publish now</button>
                  </>
                )}
                <button onClick={() => navigator.clipboard.writeText(`Sources and the full breakdown: ${onepagerUrl}`)} className="rounded-lg border border-line px-4 py-2 text-sm">Copy first comment</button>
                <button onClick={() => window.confirm("Write a Substack post that expands this one? It takes about 5 minutes and costs about $0.25.") && onExpand(draft.id)}
                  className="rounded-lg border border-line px-4 py-2 text-sm">Expand to Substack</button>
              </>
            )}
            {pubMsg && <span className="self-center text-sm text-ink-2">{pubMsg}</span>}
            {draft.status === "approved" && !draft.linkedin_url && (
              <details open={handedOff} className="w-full rounded-lg border border-line p-3 text-sm">
                <summary className="cursor-pointer text-ink-2">Posted it from LinkedIn? Add the link once it&apos;s live</summary>
                <div className="mt-2 space-y-2">
                  <input value={liveUrl} onChange={(e) => setLiveUrl(e.target.value)} placeholder="https://www.linkedin.com/feed/update/urn:li:share:…"
                    aria-label="LinkedIn post link" className="w-full rounded-lg border border-line bg-bg px-3 py-1.5 outline-none focus:border-ink" />
                  <textarea value={finalText} onChange={(e) => setFinalText(e.target.value)} rows={4} aria-label="Final text"
                    placeholder="Changed it in LinkedIn? Paste the final text here so the agent learns from your edits (optional)."
                    className="w-full rounded-lg border border-line bg-bg p-2 outline-none focus:border-ink" />
                  <button disabled={busy || !POST_URL.test(liveUrl.trim())} onClick={markLive}
                    className="rounded-lg bg-ink px-4 py-1.5 font-medium text-bg disabled:opacity-50">Mark as live</button>
                  <p className="text-xs text-muted">Copy first comment adds the one-pager link; LinkedIn posts it under your post when you paste it as a comment.</p>
                </div>
              </details>
            )}
            {draft.linkedin_url && <Performance draft={draft} onChange={onChange} />}
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
                <div className={`num text-2xl font-semibold ${overall < 4 ? "text-ink font-semibold" : ""}`}>{overall.toFixed(1)}/5</div>
                <div className="text-xs text-muted">humanizer 40% · other scores share 60%</div>
              </div>
            )}
            <div className="mb-1 text-ink-2">Scores <span className="text-muted">(1–5, 5 is best)</span></div>
            {scores.map(([k, v]) => (
              <div key={k} className="flex items-center justify-between border-t border-line py-1">
                <span>{k}</span>
                <span className={`num font-medium ${v < 4 ? "text-ink font-semibold" : ""}`}>{v}/5</span>
              </div>
            ))}
          </div>
          {violations.length > 0 && (
            <div className="text-ink font-semibold"><div className="font-medium">! Guardrail flags</div>
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

const PERFORMANCE = [["well", "Did well"], ["average", "Average"], ["poorly", "Didn't land"]] as const;

/** After a post is live: how readers responded. The next run learns from it, and posts that did well become the
 *  writer's main reference (core/lessons.py, agents/market_intel/specialists.py). */
function Performance({ draft, onChange }: { draft: Draft; onChange: () => void }) {
  const current = draft.feedback?.performance;
  const mark = async (performance: string) => {
    const feedback = { ...(draft.feedback ?? {}), performance: current === performance ? undefined : performance };
    await supabase.from("drafts").update({ feedback, learned_at: null }).eq("id", draft.id);  // learned again next run
    onChange();
  };
  return (
    <div className="flex w-full flex-wrap items-center gap-2 text-sm">
      <span className="text-ink-2">How did it do?</span>
      {PERFORMANCE.map(([value, label]) => (
        <button key={value} onClick={() => mark(value)} aria-pressed={current === value}
          className={`rounded-full border px-3 py-1 ${current === value ? "border-ink bg-ink text-bg" : "border-line hover:border-ink"}`}>{label}</button>
      ))}
      {current === "well" && <span className="text-xs text-muted">The writer now uses this post as a reference.</span>}
    </div>
  );
}
