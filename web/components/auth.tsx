"use client";
import { useEffect, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase } from "@/lib/supabase";
import { Card } from "@/components/ui";

/** The signed-in Supabase session (owner checks happen server-side and in row-level security). */
export function useSession() {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => { setSession(data.session); setReady(true); });
    const { data } = supabase.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => data.subscription.unsubscribe();
  }, []);
  return { session, ready };
}

export function SignIn({ title = "Approval queue", next = "/queue" }: { title?: string; next?: string }) {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const send = async () => {
    const { error } = await supabase.auth.signInWithOtp({ email, options: { emailRedirectTo: `${window.location.origin}${next}` } });
    if (!error) setSent(true);
  };
  return (
    <Card className="mx-auto max-w-md">
      <h1 className="text-xl font-semibold">{title}</h1>
      <p className="mt-1 text-sm text-ink-2">Owner only. Drafts are visible and editable only to the approved account (enforced by row-level security).</p>
      {sent ? <p className="mt-4 text-sm">Check your email for a sign-in link.</p> : (
        <div className="mt-4 flex gap-2">
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com"
            className="flex-1 rounded-lg border border-line bg-bg px-3 py-2 text-sm outline-none focus:border-ink" />
          <button onClick={send} className="rounded-lg bg-ink px-4 py-2 text-sm font-medium text-bg">Send link</button>
        </div>
      )}
    </Card>
  );
}
