import { notFound } from "next/navigation";
import { supabase, configured } from "@/lib/supabase";
import { NotConfigured } from "@/components/ui";

export const revalidate = 3600;

/** Public one-pager for an approved post. Rendered in a sandboxed iframe (no scripts) as a second line of defense;
 *  the HTML itself is built server-side from escaped model output (agents/market_intel/render.py). */
export default async function OnePager({ params }: { params: Promise<{ week: string }> }) {
  if (!configured) return <NotConfigured />;
  const { week } = await params;
  const { data } = await supabase.from("drafts").select("onepager_html").eq("week", week).eq("status", "approved")
    .order("approved_at", { ascending: false }).limit(1).maybeSingle();
  if (!data) notFound();
  return <iframe title={`One-pager ${week}`} srcDoc={data.onepager_html} sandbox="" className="h-[85vh] w-full rounded-xl border border-line" />;
}
