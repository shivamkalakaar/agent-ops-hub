export type Run = {
  id: string; agent: string; kind: string; status: string;
  started_at: string; ended_at: string | null; cost_usd: number;
  trace_url: string | null; git_sha: string | null; error: string | null;
  stats: Record<string, unknown> | null;
};
export type LlmCall = {
  id: number; run_id: string; node: string; tier: string; model: string | null;
  prompt_tokens: number | null; completion_tokens: number | null;
  cost_usd: number; latency_ms: number | null; ok: boolean; created_at: string;
};
export type Draft = {
  id: string; week: string; angle: { angle?: string; hook?: string } | null;
  post_md: string; onepager_html: string;
  sources: { url: string; title: string; source: string; origin?: "rss" | "brief" }[] | null;
  judge_scores: Record<string, number | string | boolean | string[]> | null;
  revisions: number; status: "pending" | "approved" | "rejected";
  edited_post_md: string | null; created_at: string; approved_at: string | null; run_id: string;
  published_at: string | null; linkedin_url: string | null;
};
