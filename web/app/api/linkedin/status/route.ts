import { bearer, ownerClient } from "@/lib/owner";

export async function GET(req: Request) {
  const owner = await ownerClient(bearer(req));
  if (!owner) return Response.json({ error: "forbidden" }, { status: 403 });
  const { data } = await owner.sb.from("linkedin_auth").select("name, expires_at").maybeSingle();
  if (!data) return Response.json({ connected: false });
  const daysLeft = Math.floor((new Date(data.expires_at).getTime() - Date.now()) / 864e5);
  return Response.json({ connected: daysLeft > 0, name: data.name, daysLeft });
}
