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
  const marked = text.replace(/(^|\s)#(\w+)/g, (_m, pre: string, tag: string) => `${pre}\u0000${tags.push(tag) - 1}\u0000`);
  const escaped = marked.replace(/[\\|{}@\[\]()<>#*_~]/g, (c) => `\\${c}`);
  return escaped.replace(/\u0000(\d+)\u0000/g, (_m, i: string) => `{hashtag|\\#|${tags[Number(i)]}}`);
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
