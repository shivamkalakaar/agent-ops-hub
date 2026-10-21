"use client";
import { useEffect, useState } from "react";
import { supabase } from "@/lib/supabase";
import type { AgentStep } from "@/lib/types";

/** A run's agent_steps, kept live with Supabase Realtime (plus polling every 4 s while running,
 *  in case Realtime isn't enabled on the table). */
export function useRunSteps(runId: string | null | undefined, running: boolean) {
  const [steps, setSteps] = useState<AgentStep[]>([]);
  useEffect(() => {
    if (!runId) { setSteps([]); return; }
    let alive = true;
    const load = () => supabase.from("agent_steps").select("*").eq("run_id", runId).order("seq")
      .then(({ data }) => alive && setSteps((data ?? []) as AgentStep[]));
    load();
    const upsert = (row: AgentStep) => setSteps((prev) =>
      [...prev.filter((s) => s.id !== row.id), row].sort((a, b) => a.seq - b.seq || a.id - b.id));
    const channel = supabase.channel(`steps-${runId}`)
      .on("postgres_changes", { event: "*", schema: "public", table: "agent_steps", filter: `run_id=eq.${runId}` },
        (p) => p.new && upsert(p.new as AgentStep))
      .subscribe();
    const poll = running ? setInterval(load, 4000) : undefined;
    return () => { alive = false; supabase.removeChannel(channel); if (poll) clearInterval(poll); };
  }, [runId, running]);
  return steps;
}
