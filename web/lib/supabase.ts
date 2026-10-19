import { createClient } from "@supabase/supabase-js";

const url = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "";
const anon = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "";

export const configured = Boolean(url && anon);

// Anon client: everything public reads go through RLS (see supabase/policies.sql).
export const supabase = createClient(url || "http://localhost", anon || "anon");
