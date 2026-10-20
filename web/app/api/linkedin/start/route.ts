import { cookies } from "next/headers";
import { bearer, ownerClient } from "@/lib/owner";
import { SCOPES, redirectUri } from "@/lib/linkedin";

/** Owner-only: begin LinkedIn OAuth. The owner's Supabase token rides in a short-lived httpOnly cookie so the
 *  callback can save the LinkedIn token under row-level security, with no service key on the server. */
export async function POST(req: Request) {
  const token = bearer(req);
  if (!(await ownerClient(token))) return Response.json({ error: "forbidden" }, { status: 403 });
  const state = crypto.randomUUID();
  const jar = await cookies();
  const opts = { httpOnly: true, secure: true, sameSite: "lax" as const, path: "/api/linkedin", maxAge: 600 };
  jar.set("li_state", state, opts);
  jar.set("li_sb", token!, opts);
  const url = new URL("https://www.linkedin.com/oauth/v2/authorization");
  url.search = new URLSearchParams({
    response_type: "code", client_id: process.env.LINKEDIN_CLIENT_ID!, redirect_uri: redirectUri(req), state, scope: SCOPES,
  }).toString();
  return Response.json({ url: url.toString() });
}
