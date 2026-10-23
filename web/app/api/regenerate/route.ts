import { createClient } from "@supabase/supabase-js";

// Workflows the dashboard may start, and the inputs each accepts. Anything else is refused.
const WORKFLOWS: Record<string, string[]> = { "weekly.yml": ["revise_draft_id"], "substack.yml": ["seed_draft_id", "revise_draft_id"] };

/** Owner-only: trigger an agent workflow on GitHub Actions (workflow_dispatch).
 *  Body (optional): { workflow?: "weekly.yml" | "substack.yml", seed_draft_id?: string, revise_draft_id?: string }
 *  revise_draft_id on weekly.yml rewrites a LinkedIn draft around the owner's take (saved on the draft first). */
export async function POST(req: Request) {
  const token = req.headers.get("authorization")?.replace(/^Bearer /, "");
  if (!token) return Response.json({ error: "sign in first" }, { status: 401 });
  const sb = createClient(process.env.NEXT_PUBLIC_SUPABASE_URL!, process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!);
  const { data } = await sb.auth.getUser(token);
  if (!data.user || data.user.email !== process.env.OWNER_EMAIL) return Response.json({ error: "forbidden" }, { status: 403 });

  if (!process.env.GITHUB_TOKEN || !process.env.GITHUB_REPO)
    return Response.json({ error: "GITHUB_TOKEN or GITHUB_REPO isn't set in Vercel's environment variables" }, { status: 500 });

  const body = await req.json().catch(() => ({}));
  const workflow = typeof body.workflow === "string" ? body.workflow : "weekly.yml";
  if (!(workflow in WORKFLOWS)) return Response.json({ error: `unknown workflow ${workflow}` }, { status: 400 });
  const inputs: Record<string, string> = {};
  for (const key of WORKFLOWS[workflow]) {
    const v = body[key];
    if (v == null || v === "") continue;
    if (typeof v !== "string" || !/^[0-9a-f]{12}$/.test(v)) return Response.json({ error: `bad ${key}` }, { status: 400 });
    inputs[key] = v;
  }

  const res = await fetch(`https://api.github.com/repos/${process.env.GITHUB_REPO}/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    headers: { Authorization: `Bearer ${process.env.GITHUB_TOKEN}`, Accept: "application/vnd.github+json" },
    body: JSON.stringify({ ref: "main", ...(Object.keys(inputs).length ? { inputs } : {}) }),
  });
  if (!res.ok) return Response.json({ error: `GitHub ${res.status}: ${(await res.text()).slice(0, 200)}` }, { status: 502 });
  return Response.json({ ok: true });
}
