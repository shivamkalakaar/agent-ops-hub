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
  published_at: string | null; linkedin_url: string | null; series_no: number | null;
  feedback: { reasons?: string[]; note?: string; performance?: "well" | "average" | "poorly" } | null; my_take: string | null;
  image?: { url: string; alt: string; kind: "chart" | "card" } | null;
};

// --- Substack newsroom (agent #2) ---------------------------------------------------------
export type AgentStep = {
  id: number; run_id: string; seq: number; agent: string; specialist: string; kind: "decision" | "work";
  reason: string | null; summary: string | null; output: Record<string, unknown> | null;
  cost_usd: number; latency_ms: number | null; status: "running" | "ok" | "failed" | "skipped" | "blocked" | "degraded";
  started_at: string; ended_at: string | null;
};
export type AgentRegistry = {
  agent: string; specialist: string; position: number; role: string; description: string | null;
  default_model: string | null; allowed_models: string[] | null; required: boolean; default_max_calls: number | null;
};
export type AgentSetting = {
  agent: string; specialist: string; enabled: boolean; model: string | null; max_calls: number | null;
  prompt_version_id: string | null; updated_at: string;
};
export type PromptVersion = {
  id: string; agent: string; specialist: string; version: number; body: string; note: string | null; created_at: string;
};
export type SubstackImage = {
  kind: "cover" | "inline" | "chart"; url: string; alt: string; caption?: string; after_section?: string | null;
  check?: { ok: boolean; score: number; issues: string[] };
};
export type SubstackDraft = {
  id: string; slot: string; seed_draft_id: string | null; title: string; subtitle: string | null;
  body_md: string; body_html: string; images: SubstackImage[] | null;
  sources: { url: string; title: string; source: string; origin?: "rss" | "brief" }[] | null;
  judge_scores: Record<string, number | string | boolean | string[] | null> | null;
  fact_report: { claims?: { claim: string; verdict: string; fix?: string }[]; invented?: string[]; blocking?: number; supported?: number } | null;
  final_report: { passed?: boolean; blockers?: string[]; polish?: string[] } | null;
  revisions: number; status: "pending" | "approved" | "rejected" | "published";
  edited_title: string | null; edited_subtitle: string | null; edited_body_md: string | null;
  published_url: string | null; created_at: string; approved_at: string | null; run_id: string;
  feedback: { reasons?: string[]; note?: string } | null; my_take: string | null;
};
