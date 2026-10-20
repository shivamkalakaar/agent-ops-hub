import { bearer, ownerClient } from "@/lib/owner";
import { liFetch, littleText } from "@/lib/linkedin";

/** Owner-only: publish an APPROVED draft to the owner's LinkedIn profile, then add the one-pager link as the first comment. */
export async function POST(req: Request) {
  const owner = await ownerClient(bearer(req));
  if (!owner) return Response.json({ error: "forbidden" }, { status: 403 });
  const { draftId } = await req.json();

  const { data: d } = await owner.sb.from("drafts").select("*").eq("id", draftId).maybeSingle();
  if (!d) return Response.json({ error: "draft not found" }, { status: 404 });
  if (d.status !== "approved") return Response.json({ error: "approve the draft first" }, { status: 409 });
  if (d.published_at) return Response.json({ error: "already published", url: d.linkedin_url }, { status: 409 });

  const { data: li } = await owner.sb.from("linkedin_auth").select("*").maybeSingle();
  if (!li || new Date(li.expires_at) < new Date()) return Response.json({ error: "connect LinkedIn first" }, { status: 412 });

  const text: string = d.edited_post_md ?? d.post_md;
  const res = await liFetch("/rest/posts", li.access_token, {
    method: "POST",
    body: JSON.stringify({
      author: li.person_urn, commentary: littleText(text), visibility: "PUBLIC",
      distribution: { feedDistribution: "MAIN_FEED", targetEntities: [], thirdPartyDistributionChannels: [] },
      lifecycleState: "PUBLISHED", isReshareDisabledByAuthor: false,
    }),
  });
  const urn = res.headers.get("x-restli-id");
  if (!res.ok || !urn) return Response.json({ error: `LinkedIn ${res.status}: ${(await res.text()).slice(0, 300)}` }, { status: 502 });

  const url = `https://www.linkedin.com/feed/update/${urn}/`;
  // Save first: the post is live even if the comment below fails.
  await owner.sb.from("drafts").update({ published_at: new Date().toISOString(), linkedin_url: url }).eq("id", d.id);

  const onepager = `${new URL(req.url).origin}/p/${d.week}`;
  const c = await liFetch(`/rest/socialActions/${encodeURIComponent(urn)}/comments`, li.access_token, {
    method: "POST",
    body: JSON.stringify({ actor: li.person_urn, object: urn, message: { text: `Sources and the full breakdown: ${onepager}` } }),
  });
  return Response.json({ url, comment: c.ok ? "added" : `failed (${c.status}); add it by hand` });
}
