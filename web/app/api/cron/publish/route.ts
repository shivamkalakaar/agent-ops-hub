import { createClient } from "@supabase/supabase-js";
import { publishDraft } from "@/lib/publish";

/** Vercel cron, weekdays 15:30 UTC (web/vercel.json): publish approved drafts whose publish_at has passed.
 *  Vercel sends `Authorization: Bearer $CRON_SECRET`; the service key reads the LinkedIn token without a browser session. */
export async function GET(req: Request) {
  if (!process.env.CRON_SECRET || req.headers.get("authorization") !== `Bearer ${process.env.CRON_SECRET}`)
    return Response.json({ error: "forbidden" }, { status: 403 });
  if (!process.env.SUPABASE_SERVICE_KEY) return Response.json({ error: "SUPABASE_SERVICE_KEY isn't set in Vercel" }, { status: 500 });
  const sb = createClient(process.env.NEXT_PUBLIC_SUPABASE_URL!, process.env.SUPABASE_SERVICE_KEY, { auth: { persistSession: false } });
  const { data: due } = await sb.from("drafts").select("id").eq("status", "approved").is("published_at", null)
    .not("publish_at", "is", null).lte("publish_at", new Date().toISOString()).order("publish_at").limit(1);
  // one post per run: two posts on the same morning compete with each other
  const results = [];
  const origin = process.env.SITE_URL ?? new URL(req.url).origin;  // the one-pager link in the first comment
  for (const d of due ?? []) results.push({ id: d.id, ...(await publishDraft(sb, d.id, origin)) });
  return Response.json({ published: results });
}
