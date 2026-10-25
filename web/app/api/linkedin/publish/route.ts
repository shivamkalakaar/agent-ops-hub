import { bearer, ownerClient } from "@/lib/owner";
import { publishDraft } from "@/lib/publish";

/** Owner-only: publish an APPROVED draft to the owner's LinkedIn profile now, then add the one-pager link as the first comment. */
export async function POST(req: Request) {
  const owner = await ownerClient(bearer(req));
  if (!owner) return Response.json({ error: "forbidden" }, { status: 403 });
  const { draftId } = await req.json();
  const r = await publishDraft(owner.sb, draftId, new URL(req.url).origin);
  if (!r.ok) return Response.json({ error: r.error, url: r.url }, { status: r.status });
  const { ok: _ok, ...body } = r;  // eslint-disable-line @typescript-eslint/no-unused-vars
  return Response.json(body);
}
