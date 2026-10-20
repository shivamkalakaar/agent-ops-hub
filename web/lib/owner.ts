import { createClient, type SupabaseClient } from "@supabase/supabase-js";

/** Supabase client acting as the signed-in owner, so row-level security applies to every query.
 *  Returns null unless the token belongs to OWNER_EMAIL. */
export async function ownerClient(token: string | null | undefined): Promise<{ sb: SupabaseClient; email: string } | null> {
  if (!token) return null;
  const sb = createClient(process.env.NEXT_PUBLIC_SUPABASE_URL!, process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!, {
    global: { headers: { Authorization: `Bearer ${token}` } },
    auth: { persistSession: false },
  });
  const { data } = await sb.auth.getUser(token);
  const email = data.user?.email;
  if (!email || email !== process.env.OWNER_EMAIL) return null;
  return { sb, email };
}

export const bearer = (req: Request) => req.headers.get("authorization")?.replace(/^Bearer /, "");
