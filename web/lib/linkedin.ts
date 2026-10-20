// LinkedIn API helpers (Sign In with LinkedIn using OpenID Connect + Share on LinkedIn).
export const SCOPES = "openid profile w_member_social";

export function redirectUri(req: Request) {
  return process.env.LINKEDIN_REDIRECT_URI ?? `${new URL(req.url).origin}/api/linkedin/callback`;
}

/** Versioned REST API header (YYYYMM). LinkedIn supports each monthly version for about a year,
 *  so default to two months back unless pinned with LINKEDIN_API_VERSION. */
export function apiVersion() {
  if (process.env.LINKEDIN_API_VERSION) return process.env.LINKEDIN_API_VERSION;
  const d = new Date();
  d.setUTCMonth(d.getUTCMonth() - 2);
  return `${d.getUTCFullYear()}${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

/** Posts API "little text" format: escape reserved characters, keep #hashtags as real hashtags. */
export function littleText(text: string) {
  const tags: string[] = [];
  // a hashtag needs a letter; "#2" in "AI Musing #2" stays plain text
  const marked = text.replace(/(^|\s)#([A-Za-z]\w*)/g, (_m, pre: string, tag: string) => `${pre}\u0000${tags.push(tag) - 1}\u0000`);
  const escaped = marked.replace(/[\\|{}@\[\]()<>#*_~]/g, (c) => `\\${c}`);
  return escaped.replace(/\u0000(\d+)\u0000/g, (_m, i: string) => `{hashtag|\\#|${tags[Number(i)]}}`);
}

export const SERIES = process.env.NEXT_PUBLIC_SERIES_NAME ?? "AI Musing";

/** Add the series label as the last line, above a trailing hashtag line if there is one. */
export function withSeries(text: string, n: number) {
  const label = `${SERIES} #${n}`;
  const lines = text.trimEnd().split("\n");
  const last = lines[lines.length - 1] ?? "";
  if (/^(\s*#[A-Za-z]\w*\s*)+$/.test(last)) return [...lines.slice(0, -1), label, "", last].join("\n");
  return `${text.trimEnd()}\n\n${label}`;
}

export async function liFetch(path: string, token: string, init: RequestInit = {}) {
  return fetch(`https://api.linkedin.com${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      "LinkedIn-Version": apiVersion(),
      "X-Restli-Protocol-Version": "2.0.0",
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
}
