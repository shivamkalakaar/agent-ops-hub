import { createClient } from "@supabase/supabase-js";

/** Owner-only: trigger the weekly draft workflow on GitHub Actions (workflow_dispatch). */
export async function POST(req: Request) {
  const token = req.headers.get("authorization")?.replace(/^Bearer /, "");
  if (!token) return Response.json({ error: "sign in first" }, { status: 401 });
  const sb = createClient(process.env.NEXT_PUBLIC_SUPABASE_URL!, process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!);
  const { data } = await sb.auth.getUser(token);
  if (!data.user || data.user.email !== process.env.OWNER_EMAIL) return Response.json({ error: "forbidden" }, { status: 403 });

  if (!process.env.GITHUB_TOKEN || !process.env.GITHUB_REPO)
    return Response.json({ error: "GITHUB_TOKEN or GITHUB_REPO isn't set in Vercel's environment variables" }, { status: 500 });

  const res = await fetch(`https://api.github.com/repos/${process.env.GITHUB_REPO}/actions/workflows/weekly.yml/dispatches`, {
    method: "POST",
    headers: { Authorization: `Bearer ${process.env.GITHUB_TOKEN}`, Accept: "application/vnd.github+json" },
    body: JSON.stringify({ ref: "main" }),
  });
  if (!res.ok) return Response.json({ error: `GitHub ${res.status}: ${(await res.text()).slice(0, 200)}` }, { status: 502 });
  return Response.json({ ok: true });
}
