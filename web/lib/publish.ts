// Publishing an approved draft to the owner's LinkedIn profile, shared by "Publish now" (as the signed-in owner)
// and the weekday cron that sends scheduled drafts (with the service key; see app/api/cron/publish).
import type { SupabaseClient } from "@supabase/supabase-js";
import { findMentions, liFetch, littleText, withSeries, type Org } from "@/lib/linkedin";

export type PublishResult = { ok: true; url: string; seriesNo: number; tagged: string[]; tagsFailed: boolean; comment: string }
  | { ok: false; status: number; error: string; url?: string };

export async function publishDraft(sb: SupabaseClient, draftId: string, origin: string): Promise<PublishResult> {
  const { data: d } = await sb.from("drafts").select("*").eq("id", draftId).maybeSingle();
  if (!d) return { ok: false, status: 404, error: "draft not found" };
  if (d.status !== "approved") return { ok: false, status: 409, error: "approve the draft first" };
  if (d.published_at) return { ok: false, status: 409, error: "already published", url: d.linkedin_url };

  const { data: li } = await sb.from("linkedin_auth").select("*").maybeSingle();
  if (!li || new Date(li.expires_at) < new Date()) return { ok: false, status: 412, error: "connect LinkedIn first" };

  const { data: last } = await sb.from("drafts").select("series_no").not("series_no", "is", null)
    .order("series_no", { ascending: false }).limit(1).maybeSingle();
  const seriesNo = (last?.series_no ?? 0) + 1;
  const text = withSeries(d.edited_post_md ?? d.post_md, seriesNo);
  const { data: orgs } = await sb.from("linkedin_orgs").select("*");
  const mentions = findMentions(text, (orgs ?? []) as Org[]);
  const post = (commentary: string) => liFetch("/rest/posts", li.access_token, {
    method: "POST",
    body: JSON.stringify({
      author: li.person_urn, commentary, visibility: "PUBLIC",
      distribution: { feedDistribution: "MAIN_FEED", targetEntities: [], thirdPartyDistributionChannels: [] },
      lifecycleState: "PUBLISHED", isReshareDisabledByAuthor: false,
    }),
  });
  let res = await post(littleText(text, mentions));
  let tagged = mentions.map((m) => m.org.name);
  if (!res.ok && mentions.length && res.status < 500) {  // a bad company id or name: publish untagged instead of failing
    res = await post(littleText(text));
    tagged = [];
  }
  const urn = res.headers.get("x-restli-id");
  if (!res.ok || !urn) return { ok: false, status: 502, error: `LinkedIn ${res.status}: ${(await res.text()).slice(0, 300)}` };

  const url = `https://www.linkedin.com/feed/update/${urn}/`;
  // Save first: the post is live even if the comment below fails.
  await sb.from("drafts").update({ published_at: new Date().toISOString(), linkedin_url: url, series_no: seriesNo }).eq("id", d.id);

  if (!d.onepager_html)  // posts only since 2026-10-30: no one-pager, so no first comment linking to it
    return { ok: true, url, seriesNo, tagged, tagsFailed: mentions.length > 0 && tagged.length === 0, comment: "none" };
  const c = await liFetch(`/rest/socialActions/${encodeURIComponent(urn)}/comments`, li.access_token, {
    method: "POST",
    body: JSON.stringify({ actor: li.person_urn, object: urn, message: { text: `Sources and the full breakdown: ${origin}/p/${d.week}` } }),
  });
  return { ok: true, url, seriesNo, tagged, tagsFailed: mentions.length > 0 && tagged.length === 0,
           comment: c.ok ? "added" : `failed (${c.status}); add it by hand` };
}
