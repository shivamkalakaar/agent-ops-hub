"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { supabase, configured } from "@/lib/supabase";
import type { AgentRegistry, AgentSetting, AgentStep, PromptVersion, Run } from "@/lib/types";
import { ms, usd, when } from "@/lib/format";
import { Card, NotConfigured, Status } from "@/components/ui";
import { useSession } from "@/components/auth";
import { AgentGraph } from "@/components/AgentGraph";
import { Learning } from "@/components/Learning";
import { useRunSteps } from "@/lib/useRunSteps";

type StepStat = Pick<AgentStep, "agent" | "specialist" | "kind" | "status" | "cost_usd" | "latency_ms" | "run_id">;

export default function Agents() {
  const { session } = useSession();
  const [registry, setRegistry] = useState<AgentRegistry[]>([]);
  const [settings, setSettings] = useState<Record<string, AgentSetting>>({});
  const [runs, setRuns] = useState<Run[]>([]);
  const [steps, setSteps] = useState<StepStat[]>([]);
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

  if (!configured) return <NotConfigured />;
  const token = session?.access_token;

  const start = async (workflow: string) => {
    if (!token) return setMsg("Sign in on the approval queue first.");
    setMsg("Starting…");
    const res = await fetch("/api/regenerate", { method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ workflow }) });
    setMsg(res.ok ? "Started on GitHub Actions. The run appears below within a minute; open it for the live timeline." : `Failed: ${(await res.json()).error}`);
  };

  const newsroom = registry.filter((r) => r.agent === "substack");
  const runsOf = (agent: string) => runs.filter((r) => r.agent === agent && r.kind !== "eval");  // evals live on /evals
  const live = runsOf("substack").find((r) => r.status === "running");
  const shown = live ?? runsOf("substack").find((r) => r.kind === "draft") ?? runsOf("substack")[0];
  const disabled = Object.values(settings).filter((x) => x.agent === "substack" && !x.enabled).map((x) => x.specialist);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-2xl font-semibold">Agents</h1>
        <p className="text-ink-2">Every agent and specialist, how it&apos;s doing, and its controls. Last 30 days.</p>
      </div>
      {!session && <p className="text-sm text-ink-2">Read-only. <Link href="/queue" className="text-accent hover:underline">Sign in</Link> to change settings or edit prompts.</p>}
      {msg && <p className="text-sm text-ink-2">{msg}</p>}

      <AgentHeader name="Market intel" agent="market_intel" runs={runsOf("market_intel")}
        blurb="Daily: collects and scores AI news. Weekdays: one LinkedIn post (skipped when 2 are already waiting), critic loop, one-pager. Configured in code."
        action={session ? () => start("weekly.yml") : undefined} actionLabel="Draft now" />
      <Learning agent="market_intel" canEdit={!!session} />

      <AgentHeader name="Substack newsroom" agent="substack" runs={runsOf("substack")}
        blurb="Wed + Sat: a super agent runs six specialists to write one 800–1,200 word post with images."
        action={session ? () => start("substack.yml") : undefined} actionLabel="Run now" live={live} />
      <Learning agent="substack" canEdit={!!session} />

      <NewsroomDiagram run={shown} disabled={disabled} />

      {newsroom.length === 0 ? (
        <Card><p className="text-ink-2">The newsroom registers its specialists on its first run. Start one with <b>Run now</b>.</p></Card>
      ) : (
        <div className="space-y-4">
          {newsroom.filter((r) => r.specialist === "supervisor").map((r) => (
            <SpecialistCard key={r.specialist} reg={r} setting={settings[`substack/${r.specialist}`]} steps={steps.filter((s) => s.specialist === r.specialist && s.agent === "substack")}
              runs={runsOf("substack")} live={live} canEdit={!!session} onSaved={load} />
          ))}
          <div className="grid gap-4 md:grid-cols-2">
            {newsroom.filter((r) => r.specialist !== "supervisor").map((r) => (
              <SpecialistCard key={r.specialist} reg={r} setting={settings[`substack/${r.specialist}`]} steps={steps.filter((s) => s.specialist === r.specialist && s.agent === "substack")}
                runs={runsOf("substack")} live={live} canEdit={!!session} onSaved={load} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function NewsroomDiagram({ run, disabled }: { run?: Run; disabled: string[] }) {
  const running = run?.status === "running";
  const steps = useRunSteps(run?.id, running);
  return (
    <Card>
      <div className="mb-4 flex flex-wrap items-baseline gap-2">
        <h2 className="font-semibold">Newsroom</h2>
        <span className="text-sm text-ink-2">
          {!run ? "no runs yet" : running ? "live run" : `last run · ${when(run.started_at)}${run.kind === "eval" ? " (eval)" : ""}`}
        </span>
        {run && <Link href={`/runs/${run.id}`} className="ml-auto text-sm text-accent hover:underline">{running ? "watch every step →" : "see every step →"}</Link>}
      </div>
      <AgentGraph steps={steps} runFinished={!!run && !running} disabled={disabled} />
    </Card>
  );
}

function AgentHeader({ name, agent, runs, blurb, action, actionLabel, live }:
  { name: string; agent: string; runs: Run[]; blurb: string; action?: () => void; actionLabel: string; live?: Run }) {
  const finished = runs.filter((r) => r.status !== "running" && r.status !== "skipped");
  const ok = finished.filter((r) => r.status === "succeeded").length;
  const last = runs[0];
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <div className="min-w-0 flex-1">
          <h2 className="text-lg font-semibold">{name} <code className="ml-1 text-xs font-normal text-muted">{agent}</code></h2>
          <p className="text-sm text-ink-2">{blurb}</p>
        </div>
        <Stat label="Runs" value={String(runs.length)} />
        <Stat label="Success" value={finished.length ? `${Math.round((ok / finished.length) * 100)}%` : "–"} />
        <Stat label="Cost" value={usd(runs.reduce((s, r) => s + Number(r.cost_usd), 0))} />
        <div className="text-sm">
          <div className="text-ink-2">Last run</div>
          {last ? <Link href={`/runs/${last.id}`} className="hover:underline">{when(last.started_at)} <Status status={last.status} /></Link> : "–"}
        </div>
        {live && <Link href={`/runs/${live.id}`} className="inline-flex items-center gap-1.5 text-sm text-accent hover:underline"><span className="h-2 w-2 animate-pulse rounded-full bg-accent" />Watch live</Link>}
        {action && <button onClick={action} className="rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-bg">{actionLabel}</button>}
      </div>
    </Card>
  );
}

const Stat = ({ label, value }: { label: string; value: string }) => (
  <div className="text-sm"><div className="text-ink-2">{label}</div><div className="num font-semibold">{value}</div></div>
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
            {running ? <span className="inline-flex items-center gap-1 text-xs text-accent"><span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />working</span>
              : <span className="text-xs text-muted">{enabled ? "idle" : "off"}</span>}
            {reg.required && <span className="rounded border border-line px-1.5 text-[11px] text-muted">required</span>}
          </div>
          <p className="mt-0.5 text-sm text-ink-2">{reg.description}</p>
        </div>
        <label className="flex items-center gap-2 text-sm" title={reg.required ? "Required: can't be switched off" : ""}>
          <input type="checkbox" checked={enabled} disabled={!canEdit || reg.required} onChange={(e) => save({ enabled: e.target.checked })} className="accent-[var(--accent)]" />
          on
        </label>
      </div>
      <div className="mt-3 grid grid-cols-4 gap-2 text-sm">
        <Stat label={reg.specialist === "supervisor" ? "Decisions" : "Turns"} value={String(counted.length)} />
        <Stat label="Avg latency" value={lat.length ? ms(lat.reduce((a, b) => a + b, 0) / lat.length) : "–"} />
        <Stat label="Cost" value={usd(steps.reduce((s, x) => s + Number(x.cost_usd), 0))} />
        <Stat label={reg.specialist === "supervisor" ? "OK" : "In runs"} value={reg.specialist === "supervisor"
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
      {err && <p className="mt-2 text-sm text-bad">{err}</p>}
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
        the run fills them in. CI evals test the repo files, so copy a version you like into <code>agents/substack/prompts/</code>.
      </p>
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={14} aria-label="Prompt"
        className="w-full rounded-lg border border-line bg-bg p-3 font-mono text-xs leading-relaxed outline-none focus:border-accent" />
      <div className="flex flex-wrap gap-2">
        <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="What changed?" aria-label="Version note"
          className="min-w-40 flex-1 rounded-lg border border-line bg-bg px-3 py-1.5 text-sm outline-none focus:border-accent" />
        <button onClick={saveNew} disabled={!text.trim()} className="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">Save as new version</button>
        {activeId && <button onClick={() => onActivate(null)} className="rounded-lg border border-line px-3 py-1.5 text-sm">Use repo file</button>}
      </div>
      {err && <p className="text-sm text-bad">{err}</p>}
      <ul className="divide-y divide-line text-sm">
        {versions.map((v) => (
          <li key={v.id} className="flex flex-wrap items-center gap-2 py-1.5">
            <b className="num">v{v.version}</b>
            <span className="text-ink-2">{v.note ?? ""}</span>
            <span className="text-xs text-muted">{when(v.created_at)}</span>
            {v.id === activeId && <span className="text-xs text-good">active</span>}
            <span className="ml-auto flex gap-2">
              <button onClick={() => setText(v.body)} className="text-accent hover:underline">load</button>
              <button onClick={() => setDiffWith(diffWith?.id === v.id ? null : v)} className="text-accent hover:underline">diff</button>
              {v.id !== activeId && <button onClick={() => onActivate(v.id)} className="text-accent hover:underline">activate</button>}
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
          <div key={n} className={l.t === "+" ? "text-good" : l.t === "-" ? "text-bad" : "text-muted"}>{l.t} {l.s}</div>
        ))}
      </pre>
    </div>
  );
}
