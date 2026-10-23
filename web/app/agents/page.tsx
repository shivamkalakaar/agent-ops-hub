"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { supabase, configured } from "@/lib/supabase";
import type { AgentRegistry, AgentSetting, AgentStep, PromptVersion, Run } from "@/lib/types";
import { ms, usd, when } from "@/lib/format";
import { Card, NotConfigured, PageTitle, Status } from "@/components/ui";
import { useSession } from "@/components/auth";
import { AgentGraph } from "@/components/AgentGraph";
import { Learning } from "@/components/Learning";
import { useRunSteps } from "@/lib/useRunSteps";

type StepStat = Pick<AgentStep, "agent" | "specialist" | "kind" | "status" | "cost_usd" | "latency_ms" | "run_id">;
type AgentId = "market_intel" | "substack";

const AGENTS: { agent: AgentId; name: string; workflow: string; action: string; blurb: string; input: { title: string; sub: string };
  drafts: string }[] = [
  { agent: "market_intel", name: "LinkedIn agent", workflow: "weekly.yml", action: "Write a draft now", drafts: "drafts",
    blurb: "Weekdays at 8 am PT it writes one short post from your AI Stack Brief and RSS stories. It skips the day while 2 drafts are waiting for you.",
    input: { title: "Stories", sub: "AI Stack Brief and RSS, scored each morning" } },
  { agent: "substack", name: "Substack agent", workflow: "substack.yml", action: "Write a post now", drafts: "substack_drafts",
    blurb: "Wednesdays and Saturdays it writes one 800–1,200 word post with images, from the week's stories and your published LinkedIn posts.",
    input: { title: "Stories and posts", sub: "AI Stack Brief, RSS, your published LinkedIn posts" } },
];

export default function Agents() {
  const { session } = useSession();
  const [registry, setRegistry] = useState<AgentRegistry[]>([]);
  const [settings, setSettings] = useState<Record<string, AgentSetting>>({});
  const [runs, setRuns] = useState<Run[]>([]);
  const [steps, setSteps] = useState<StepStat[]>([]);
  const [kept, setKept] = useState<Record<string, { kept: number; decided: number }>>({});
  const [msg, setMsg] = useState("");

  const load = useCallback(async () => {
    const since = new Date(Date.now() - 30 * 864e5).toISOString();
    const [reg, set, r, s] = await Promise.all([
      supabase.from("agent_registry").select("*").order("position"),
      supabase.from("agent_settings").select("*"),
      supabase.from("runs").select("*").gte("started_at", since).order("started_at", { ascending: false }).limit(300),
      supabase.from("agent_steps").select("agent,specialist,kind,status,cost_usd,latency_ms,run_id").gte("started_at", since).limit(5000),
    ]);
    setRegistry((reg.data ?? []) as AgentRegistry[]);
    setSettings(Object.fromEntries(((set.data ?? []) as AgentSetting[]).map((x) => [`${x.agent}/${x.specialist}`, x])));
    setRuns((r.data ?? []) as Run[]);
    setSteps((s.data ?? []) as StepStat[]);
  }, []);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);
  useEffect(() => {  // drafts are owner-only, so "kept by you" needs a session
    if (!session) return;
    Promise.all(AGENTS.map(async (a) => {
      const { data } = await supabase.from(a.drafts).select("status").neq("status", "pending").limit(500);
      const rows = (data ?? []) as { status: string }[];
      return [a.agent, { kept: rows.filter((d) => d.status !== "rejected").length, decided: rows.length }] as const;
    })).then((x) => setKept(Object.fromEntries(x)));
  }, [session]);

  if (!configured) return <NotConfigured />;
  const token = session?.access_token;

  const start = async (workflow: string) => {
    if (!token) return setMsg("Sign in on the approval queue first.");
    setMsg("Starting…");
    const res = await fetch("/api/regenerate", { method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ workflow }) });
    setMsg(res.ok ? "Started on GitHub Actions. The run shows here within a minute." : `Failed: ${(await res.json()).error}`);
  };

  return (
    <div className="space-y-12">
      <div className="space-y-3">
        <PageTitle title="Agents">
          Both writing agents, the specialists inside each one, and what they&apos;ve learned from your feedback. Numbers cover the last 30 days.
        </PageTitle>
        <div className="flex flex-wrap gap-4 text-sm">
          {[...AGENTS, { agent: "ai_brief", name: "AI Stack Brief" }].map((a) => <a key={a.agent} href={`#${a.agent}`} className="text-ink-2 underline underline-offset-4 hover:text-ink">{a.name}</a>)}
        </div>
        {!session && <p className="text-sm text-ink-2">Read-only. <Link href="/queue" className="underline underline-offset-4">Sign in</Link> to change settings or edit prompts.</p>}
        {msg && <p className="text-sm text-ink-2">{msg}</p>}
      </div>

      {AGENTS.map((a) => {
        const own = runs.filter((r) => r.agent === a.agent && r.kind !== "eval");  // evals live on /evals
        const evals = runs.filter((r) => r.agent === a.agent && r.kind === "eval");
        const live = own.find((r) => r.status === "running");
        const shown = live ?? own.find((r) => r.kind === "draft") ?? own[0];
        const team = registry.filter((r) => r.agent === a.agent);
        const disabled = Object.values(settings).filter((x) => x.agent === a.agent && !x.enabled).map((x) => x.specialist);
        return (
          <section key={a.agent} id={a.agent} className="scroll-mt-6 space-y-4">
            <AgentHeader name={a.name} runs={own} blurb={a.blurb} kept={kept[a.agent]} live={live}
              action={session ? () => start(a.workflow) : undefined} actionLabel={a.action} />
            <TeamDiagram run={shown} team={team} input={a.input} disabled={disabled} />
            <LatestEval run={evals[0]} />
            {team.length > 0 ? (
              <details className="group rounded-lg border border-line">
                <summary className="flex cursor-pointer list-none items-center justify-between px-5 py-3 text-sm font-medium">
                  <span>Specialists ({team.length}): models, call limits and prompts</span>
                  <span aria-hidden className="text-ink-2 group-open:rotate-90">›</span>
                </summary>
                <div className="grid gap-4 border-t border-line p-4 md:grid-cols-2">
                  {team.map((r) => (
                    <SpecialistCard key={r.specialist} reg={r} setting={settings[`${a.agent}/${r.specialist}`]}
                      steps={steps.filter((s) => s.specialist === r.specialist && s.agent === a.agent)}
                      runs={own} live={live} canEdit={!!session} onSaved={load} />
                  ))}
                </div>
              </details>
            ) : <Card><p className="text-sm text-ink-2">The team registers its specialists on its first run.</p></Card>}
            <Learning agent={a.agent} canEdit={!!session} />
          </section>
        );
      })}

      <section id="ai_brief" className="scroll-mt-6 space-y-4">
        <AgentHeader name="AI Stack Brief" runs={runs.filter((r) => r.agent === "ai_brief")}
          blurb="Runs on your Mac every morning: reads 20 feeds, researches and writes the brief, sends its stories to both writing agents, and emails it at 10:00. When sending needs your approval, type /brief in Claude Code." />
      </section>
    </div>
  );
}

function TeamDiagram({ run, team, input, disabled }: { run?: Run; team: AgentRegistry[]; input: { title: string; sub: string }; disabled: string[] }) {
  const running = run?.status === "running";
  const steps = useRunSteps(run?.id, running);
  return (
    <Card>
      <div className="mb-4 flex flex-wrap items-baseline gap-2">
        <h3 className="font-semibold">{running ? "Current run" : "Last run"}</h3>
        <span className="text-sm text-ink-2">{!run ? "none yet" : running ? `started ${when(run.started_at)}` : when(run.started_at)}</span>
        {run && <Link href={`/runs/${run.id}`} className="ml-auto text-sm underline underline-offset-4">All steps</Link>}
      </div>
      {team.length ? <AgentGraph steps={steps} runFinished={!!run && !running} roster={team} input={input} disabled={disabled} />
        : <p className="text-sm text-ink-2">The diagram appears after the first run.</p>}
    </Card>
  );
}

function LatestEval({ run }: { run?: Run }) {
  if (!run) return null;
  const s = (run.stats ?? {}) as { mean?: number; baseline?: number | null; passed?: boolean; pr?: string | null; trigger?: string };
  return (
    <p className="text-sm text-ink-2">
      Latest eval: {run.status === "running" ? "running now" : <>mean <span className="num text-ink">{s.mean ?? "–"}</span>
        {s.baseline != null && <> against a baseline of <span className="num">{s.baseline}</span></>}, {s.passed === false ? <b className="text-ink">failed</b> : "passed"}</>}
      {run.status !== "running" && <> · {s.pr ? `PR #${s.pr}` : s.trigger ?? "local"}</>} · {when(run.started_at)} · <Link href="/evals" className="underline underline-offset-4">all evals</Link>
    </p>
  );
}

function AgentHeader({ name, runs, blurb, kept, action, actionLabel, live }:
  { name: string; runs: Run[]; blurb: string; kept?: { kept: number; decided: number }; action?: () => void; actionLabel?: string; live?: Run }) {
  const finished = runs.filter((r) => r.status !== "running" && r.status !== "skipped");
  const drafts = runs.filter((r) => r.kind === "draft" && r.status === "succeeded");
  const last = runs[0];
  // the most recent problem, if it's newer than the last success: what failed and why, without opening the run
  const lastOk = runs.find((r) => r.status === "succeeded");
  const problem = runs.find((r) => ["failed", "blocked", "budget_exceeded"].includes(r.status));
  const showProblem = problem && (!lastOk || problem.started_at > lastOk.started_at);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-0 flex-1 space-y-1">
          <h2 className="text-2xl font-semibold tracking-tight">{name}</h2>
          <p className="max-w-2xl text-sm text-ink-2">{blurb}</p>
        </div>
        {live && <Link href={`/runs/${live.id}`} className="inline-flex items-center gap-1.5 text-sm underline underline-offset-4"><span aria-hidden className="spinner" />Current run</Link>}
        {action && <button onClick={action} className="rounded-lg border border-ink px-3 py-1.5 text-sm font-medium hover:bg-ink hover:text-bg">{actionLabel}</button>}
      </div>
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-line bg-line md:grid-cols-4">
        <Stat label="Runs" value={String(runs.length)} note={finished.length ? `${Math.round((finished.filter((r) => r.status === "succeeded").length / finished.length) * 100)}% finished cleanly` : undefined} />
        <Stat label="Kept by you" value={kept?.decided ? `${Math.round((kept.kept / kept.decided) * 100)}%` : "–"}
          note={kept ? `${kept.kept} of ${kept.decided} decided drafts` : "sign in to see"} />
        <Stat label="Cost per draft" value={drafts.length ? usd(drafts.reduce((s, r) => s + Number(r.cost_usd), 0) / drafts.length) : "–"}
          note={`${usd(runs.reduce((s, r) => s + Number(r.cost_usd), 0))} in total`} />
        <div className="bg-card p-4 text-sm">
          <div className="text-xs uppercase tracking-wide text-ink-2">Last run</div>
          <div className="mt-1.5">{last ? <Link href={`/runs/${last.id}`} className="hover:underline">{when(last.started_at)}</Link> : "–"}</div>
          {last && <div className="mt-1"><Status status={last.status} /></div>}
        </div>
      </div>
      {showProblem && (
        <Link href={`/runs/${problem.id}`} className="block rounded-lg border-2 border-ink px-4 py-2 text-sm hover:underline">
          <b>! {problem.status === "blocked" ? "Blocked" : problem.status === "budget_exceeded" ? "Stopped by the budget cap" : "Failed"} {when(problem.started_at)}:</b>{" "}
          {problem.error ?? "see the run for details"}
        </Link>
      )}
    </div>
  );
}

const Stat = ({ label, value, note }: { label: string; value: string; note?: string }) => (
  <div className="bg-card p-4 text-sm">
    <div className="text-xs uppercase tracking-wide text-ink-2">{label}</div>
    <div className="num mt-1 text-xl font-medium">{value}</div>
    {note && <div className="mt-0.5 text-xs text-muted">{note}</div>}
  </div>
);

function SpecialistCard({ reg, setting, steps, runs, live, canEdit, onSaved }:
  { reg: AgentRegistry; setting?: AgentSetting; steps: StepStat[]; runs: Run[]; live?: Run; canEdit: boolean; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [err, setErr] = useState("");
  const work = steps.filter((s) => s.kind === "work");
  const decisions = steps.filter((s) => s.kind === "decision");
  const counted = reg.specialist === "supervisor" ? decisions : work;
  const done = counted.filter((s) => s.status !== "running");
  const lat = done.map((s) => s.latency_ms ?? 0);
  const runIds = new Set(work.map((s) => s.run_id));
  const enabled = setting?.enabled ?? true;
  const running = !!live && steps.some((s) => s.run_id === live.id && s.status === "running");
  const maxCalls = setting?.max_calls ?? reg.default_max_calls;

  const save = async (patch: Partial<AgentSetting>) => {
    setErr("");
    const row = { agent: reg.agent, specialist: reg.specialist, enabled, model: setting?.model ?? null,
      max_calls: setting?.max_calls ?? null, prompt_version_id: setting?.prompt_version_id ?? null, ...patch, updated_at: new Date().toISOString() };
    const { error } = await supabase.from("agent_settings").upsert(row);
    if (error) setErr(error.message); else onSaved();
  };

  return (
    <Card className={enabled ? "" : "opacity-70"}>
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-semibold">{reg.role}</h3>
            {running ? <span className="inline-flex items-center gap-1.5 text-xs"><span aria-hidden className="spinner" />working</span>
              : <span className="text-xs text-muted">{enabled ? "idle" : "off"}</span>}
            {reg.required && <span className="rounded border border-line px-1.5 text-[11px] text-muted">required</span>}
          </div>
          <p className="mt-0.5 text-sm text-ink-2">{reg.description}</p>
        </div>
        <label className="flex items-center gap-2 text-sm" title={reg.required ? "Required: can't be switched off" : ""}>
          <input type="checkbox" checked={enabled} disabled={!canEdit || reg.required} onChange={(e) => save({ enabled: e.target.checked })} className="accent-[var(--ink)]" />
          on
        </label>
      </div>
      <div className="mt-3 grid grid-cols-4 gap-2 text-sm">
        <MiniStat label={reg.specialist === "supervisor" ? "Decisions" : "Turns"} value={String(counted.length)} />
        <MiniStat label="Avg latency" value={lat.length ? ms(lat.reduce((a, b) => a + b, 0) / lat.length) : "–"} />
        <MiniStat label="Cost" value={usd(steps.reduce((s, x) => s + Number(x.cost_usd), 0))} />
        <MiniStat label={reg.specialist === "supervisor" ? "OK" : "In runs"} value={reg.specialist === "supervisor"
          ? (done.length ? `${Math.round((done.filter((s) => s.status === "ok").length / done.length) * 100)}%` : "–")
          : (runs.length ? `${Math.round((runIds.size / runs.length) * 100)}%` : "–")} />
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
        <select value={setting?.model ?? ""} disabled={!canEdit} onChange={(e) => save({ model: e.target.value || null })}
          aria-label={`${reg.role} model`} className="max-w-full rounded-lg border border-line bg-bg px-2 py-1">
          <option value="">default · {reg.default_model}</option>
          {(reg.allowed_models ?? []).filter((m) => m !== reg.default_model).map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
        {reg.specialist !== "supervisor" && (
          <label className="flex items-center gap-1 text-ink-2">max calls
            <input type="number" min={1} max={5} value={maxCalls ?? ""} disabled={!canEdit} aria-label={`${reg.role} max calls per run`}
              onChange={(e) => { const v = Number(e.target.value); if (v >= 1 && v <= 5) save({ max_calls: v }); }}
              className="w-14 rounded-lg border border-line bg-bg px-2 py-1 num" />
          </label>
        )}
        {canEdit && <button onClick={() => setEditing(!editing)} className="ml-auto rounded-lg border border-line px-3 py-1 hover:bg-bg">{editing ? "Close prompt" : "Edit prompt"}</button>}
      </div>
      {err && <p className="mt-2 text-sm text-ink font-semibold">{err}</p>}
      {editing && <PromptEditor reg={reg} activeId={setting?.prompt_version_id ?? null} onActivate={(id) => save({ prompt_version_id: id })} />}
    </Card>
  );
}

function PromptEditor({ reg, activeId, onActivate }: { reg: AgentRegistry; activeId: string | null; onActivate: (id: string | null) => void }) {
  const [versions, setVersions] = useState<PromptVersion[]>([]);
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [diffWith, setDiffWith] = useState<PromptVersion | null>(null);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    const { data, error } = await supabase.from("prompt_versions").select("*").eq("agent", reg.agent).eq("specialist", reg.specialist)
      .order("version", { ascending: false });
    if (error) return setErr(error.message);
    const V = (data ?? []) as PromptVersion[];
    setVersions(V);
    setText((V.find((v) => v.id === activeId) ?? V.find((v) => v.version === 1) ?? V[0])?.body ?? "");
  }, [reg.agent, reg.specialist, activeId]);
  useEffect(() => { load(); }, [load]);

  const active = versions.find((v) => v.id === activeId);
  const saveNew = async () => {
    const version = Math.max(0, ...versions.map((v) => v.version)) + 1;
    const id = Array.from(crypto.getRandomValues(new Uint8Array(6)), (b) => b.toString(16).padStart(2, "0")).join("");
    const { error } = await supabase.from("prompt_versions").insert({ id, agent: reg.agent, specialist: reg.specialist, version,
      body: text, note: note || null, created_at: new Date().toISOString() });
    if (error) return setErr(error.message);
    setNote("");
    onActivate(id);
  };

  return (
    <div className="mt-4 space-y-3 border-t border-line pt-4">
      <p className="text-xs text-muted">
        Active: <b>{active ? `v${active.version}` : "repo file"}</b>. New versions take effect on the next run. Keep the {"{{PLACEHOLDERS}}"}:
        the run fills them in. CI evals test the repo files, so copy a version you like into <code>agents/{reg.agent}/prompts/</code>.
      </p>
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={14} aria-label="Prompt"
        className="w-full rounded-lg border border-line bg-bg p-3 font-mono text-xs leading-relaxed outline-none focus:border-ink" />
      <div className="flex flex-wrap gap-2">
        <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="What changed?" aria-label="Version note"
          className="min-w-40 flex-1 rounded-lg border border-line bg-bg px-3 py-1.5 text-sm outline-none focus:border-ink" />
        <button onClick={saveNew} disabled={!text.trim()} className="rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-bg disabled:opacity-50">Save as new version</button>
        {activeId && <button onClick={() => onActivate(null)} className="rounded-lg border border-line px-3 py-1.5 text-sm">Use repo file</button>}
      </div>
      {err && <p className="text-sm text-ink font-semibold">{err}</p>}
      <ul className="divide-y divide-line text-sm">
        {versions.map((v) => (
          <li key={v.id} className="flex flex-wrap items-center gap-2 py-1.5">
            <b className="num">v{v.version}</b>
            <span className="text-ink-2">{v.note ?? ""}</span>
            <span className="text-xs text-muted">{when(v.created_at)}</span>
            {v.id === activeId && <span className="text-xs text-ink">active</span>}
            <span className="ml-auto flex gap-2">
              <button onClick={() => setText(v.body)} className="text-ink underline underline-offset-4 hover:underline">load</button>
              <button onClick={() => setDiffWith(diffWith?.id === v.id ? null : v)} className="text-ink underline underline-offset-4 hover:underline">diff</button>
              {v.id !== activeId && <button onClick={() => onActivate(v.id)} className="text-ink underline underline-offset-4 hover:underline">activate</button>}
            </span>
          </li>
        ))}
      </ul>
      {diffWith && <Diff a={diffWith.body} b={text} label={`v${diffWith.version} → editor`} />}
    </div>
  );
}

/** Line diff (LCS). Prompts are short, so O(n·m) is fine. */
function Diff({ a, b, label }: { a: string; b: string; label: string }) {
  const A = a.split("\n"), B = b.split("\n");
  const L = Array.from({ length: A.length + 1 }, () => new Array<number>(B.length + 1).fill(0));
  for (let i = A.length - 1; i >= 0; i--) for (let j = B.length - 1; j >= 0; j--)
    L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out: { t: " " | "-" | "+"; s: string }[] = [];
  let i = 0, j = 0;
  while (i < A.length || j < B.length) {
    if (i < A.length && j < B.length && A[i] === B[j]) { out.push({ t: " ", s: A[i] }); i++; j++; }
    else if (j < B.length && (i >= A.length || L[i][j + 1] >= L[i + 1][j])) out.push({ t: "+", s: B[j++] });
    else out.push({ t: "-", s: A[i++] });
  }
  const changed = out.filter((l) => l.t !== " ").length;
  return (
    <div>
      <div className="mb-1 text-xs text-muted">{label} · {changed} changed line{changed === 1 ? "" : "s"}</div>
      <pre className="max-h-72 overflow-auto rounded-lg border border-line bg-bg p-3 text-xs">
        {out.map((l, n) => (
          <div key={n} className={l.t === "+" ? "font-semibold text-ink" : l.t === "-" ? "text-ink-2 line-through" : "text-muted"}>{l.t} {l.s}</div>
        ))}
      </pre>
    </div>
  );
}

const MiniStat = ({ label, value }: { label: string; value: string }) => (
  <div><div className="text-xs text-ink-2">{label}</div><div className="num font-medium">{value}</div></div>
);
