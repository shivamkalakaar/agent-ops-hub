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

export type Org = { name: string; org_id: string; aliases: string[] };
export type Mention = { at: number; len: number; org: Org };

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** First mention of each known company in the text (longest spelling wins), capped at 5 tags per post. */
export function findMentions(text: string, orgs: Org[]): Mention[] {
  const found: Mention[] = [];
  for (const org of orgs) {
    const names = [org.name, ...(org.aliases ?? [])].filter(Boolean).sort((a, b) => b.length - a.length);
    for (const n of names) {
      const m = new RegExp(`(?<![\\w@])${escapeRe(n)}(?!\\w)`).exec(text);
      if (m) { found.push({ at: m.index, len: n.length, org }); break; }
    }
  }
  const sorted = found.sort((a, b) => a.at - b.at);
  return sorted.filter((m, i) => i === 0 || m.at >= sorted[i - 1].at + sorted[i - 1].len).slice(0, 5);
}

/** Posts API "little text" format: escape reserved characters, keep #hashtags as real hashtags,
 *  and turn known company names into @mentions of their pages. */
export function littleText(text: string, mentions: Mention[] = []) {
  for (const m of [...mentions].sort((a, b) => b.at - a.at))
    text = `${text.slice(0, m.at)}\u0001${mentions.indexOf(m)}\u0001${text.slice(m.at + m.len)}`;
  const tags: string[] = [];
  // a hashtag needs a letter; "#2" in "AI Musing #2" stays plain text
  const marked = text.replace(/(^|\s)#([A-Za-z]\w*)/g, (_m, pre: string, tag: string) => `${pre}\u0000${tags.push(tag) - 1}\u0000`);
  const escaped = marked.replace(/[\\|{}@\[\]()<>#*_~]/g, (c) => `\\${c}`);
  return escaped
    .replace(/\u0000(\d+)\u0000/g, (_m, i: string) => `{hashtag|\\#|${tags[Number(i)]}}`)
    .replace(/\u0001(\d+)\u0001/g, (_m, i: string) => {
      const o = mentions[Number(i)].org;
      return `@[${o.name.replace(/[\\\[\]()]/g, (c) => `\\${c}`)}](urn:li:organization:${o.org_id})`;
    });
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
