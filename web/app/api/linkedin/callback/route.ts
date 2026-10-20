import { cookies } from "next/headers";
import { ownerClient } from "@/lib/owner";
import { redirectUri } from "@/lib/linkedin";

export async function GET(req: Request) {
  const u = new URL(req.url);
  const back = (msg: string) => Response.redirect(`${u.origin}/queue?linkedin=${encodeURIComponent(msg)}`, 302);
  const jar = await cookies();
  const state = jar.get("li_state")?.value;
  const sbToken = jar.get("li_sb")?.value;
  jar.delete({ name: "li_state", path: "/api/linkedin" });
  jar.delete({ name: "li_sb", path: "/api/linkedin" });

  if (u.searchParams.get("error")) return back(u.searchParams.get("error_description") ?? "LinkedIn sign-in was cancelled");
  if (!state || state !== u.searchParams.get("state")) return back("Sign-in expired, try Connect again");
  const owner = await ownerClient(sbToken);
  if (!owner) return back("Only the owner can connect LinkedIn");

  const tok = await fetch("https://www.linkedin.com/oauth/v2/accessToken", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code", code: u.searchParams.get("code") ?? "", redirect_uri: redirectUri(req),
      client_id: process.env.LINKEDIN_CLIENT_ID!, client_secret: process.env.LINKEDIN_CLIENT_SECRET!,
    }),
  }).then((r) => r.json());
  if (!tok.access_token) return back(`LinkedIn token error: ${tok.error_description ?? tok.error ?? "unknown"}`);

  const me = await fetch("https://api.linkedin.com/v2/userinfo", { headers: { Authorization: `Bearer ${tok.access_token}` } }).then((r) => r.json());
  if (!me.sub) return back("Couldn't read your LinkedIn profile");

  const { error } = await owner.sb.from("linkedin_auth").upsert({
    owner_email: owner.email, person_urn: `urn:li:person:${me.sub}`, name: me.name ?? null, access_token: tok.access_token,
    expires_at: new Date(Date.now() + (tok.expires_in ?? 5184000) * 1000).toISOString(), updated_at: new Date().toISOString(),
  });
  return back(error ? `Couldn't save the connection: ${error.message}` : "connected");
}
