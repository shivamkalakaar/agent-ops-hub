"""RSS/Atom collection. Ported from ~/AI_update_agent/fetch_feeds.py (stdlib only);
dedupe now happens in the DB instead of state/seen.json."""
import html
import re
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

UA = "Mozilla/5.0 (Macintosh) agent-ops-hub/0.1"
MAX_PER_FEED = 15
SUMMARY_CHARS = 500


def local(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(el, *names):
    for c in el:
        if local(c.tag) in names and (c.text or "").strip():
            return c.text.strip()
    return ""


def atom_link(el):
    for c in el:
        if local(c.tag) == "link" and c.get("rel", "alternate") == "alternate" and c.get("href"):
            return c.get("href")
    return child_text(el, "link")


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", html.unescape(s)).strip()
    return s[:SUMMARY_CHARS]


def fetch(feed, cutoff):
    req = urllib.request.Request(feed["url"], headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    entries = [e for e in root.iter() if local(e.tag) in ("item", "entry")]
    items = []
    for e in entries:
        link = atom_link(e) if local(e.tag) == "entry" else child_text(e, "link", "guid")
        published = parse_date(child_text(e, "pubDate", "published", "updated", "date"))
        if not link or not published or published < cutoff:
            continue
        items.append({
            "source": feed["name"],
            "tier": feed["tier"],
            "layer_hint": feed["layer"],
            "title": clean(child_text(e, "title")),
            "url": link.strip(),
            "published": published.astimezone(timezone.utc).isoformat(timespec="minutes"),
            "summary": clean(child_text(e, "description", "summary", "content", "encoded")),
        })
    items.sort(key=lambda i: i["published"], reverse=True)
    return items[:MAX_PER_FEED]


def fetch_all(feeds: list[dict], hours: int = 36) -> tuple[list[dict], list[dict]]:
    """Fetch every feed in parallel. One broken feed never sinks the run; it lands in errors."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    def run(feed):
        try:
            return feed, fetch(feed, cutoff), None
        except Exception as ex:
            return feed, [], f"{type(ex).__name__}: {ex}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, feeds))
    items, errors, seen = [], [], set()
    for feed, got, err in results:
        if err:
            errors.append({"source": feed["name"], "error": err})
        for i in got:
            if i["url"] not in seen:
                seen.add(i["url"])
                items.append(i)
    return items, errors


ARTICLE_WORDS = 1200


def fetch_article(url: str, timeout: int = 20) -> str:
    """Best-effort plain text of an article page (stdlib only). Empty string on any failure.
    The result is untrusted web content: callers pass it to models as data, never as instructions."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if "html" not in r.headers.get("Content-Type", ""):
                return ""
            page = r.read(2_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""
    page = re.sub(r"(?is)<(script|style|noscript|svg|nav|header|footer|aside|form)\b.*?</\1>", " ", page)
    m = re.search(r"(?is)<article\b.*?</article>", page) or re.search(r"(?is)<main\b.*?</main>", page)
    scope = m.group(0) if m else page
    paras = [html.unescape(re.sub(r"<[^>]+>", " ", p)) for p in re.findall(r"(?is)<(?:p|li|h[1-4])\b[^>]*>(.*?)</(?:p|li|h[1-4])>", scope)]
    paras = [re.sub(r"\s+", " ", p).strip() for p in paras]
    text = "\n".join(p for p in paras if len(p.split()) >= 6)
    return " ".join(text.split(" ")[:ARTICLE_WORDS]) if text else ""
