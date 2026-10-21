import MarkdownIt from "markdown-it";

// Same parser and options as agents/substack/render.py (markdown-it-py): CommonMark, raw HTML escaped,
// so an edited post copies to Substack exactly as the agent would have rendered it.
const md = new MarkdownIt("commonmark", { html: false, linkify: false, typographer: false });

export const renderMarkdown = (text: string) => md.render(text);

/** A standalone preview page with Substack-like typography, for the sandboxed iframe. */
export function previewPage(title: string, subtitle: string, bodyHtml: string) {
  const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
  return `<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;background:#fff;color:#1a1a1a;font:19px/1.65 Georgia,serif}main{max-width:680px;margin:0 auto;padding:40px 16px 64px}
h1{font:700 34px/1.2 -apple-system,sans-serif;margin:0 0 8px}.sub{color:#666;font-size:20px;margin:0 0 28px}
h2{font:700 23px/1.3 -apple-system,sans-serif;margin:36px 0 10px}img{max-width:100%;height:auto;border-radius:4px}
a{color:#c2410c}p:has(> em:only-child){color:#777;font-size:15px;text-align:center}
</style></head><body><main><h1>${esc(title)}</h1><p class="sub">${esc(subtitle)}</p>${bodyHtml}</main></body></html>`;
}

export const words = (text: string) => text.replace(/!\[[^\]]*\]\([^)]*\)/g, "").trim().split(/\s+/).filter(Boolean).length;
