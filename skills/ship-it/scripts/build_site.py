#!/usr/bin/env python3
"""
Build a fast, accessible, SEO-ready static website from a small site.json.

The model writes CONTENT (site.json) and picks TOKENS (a design preset plus optional
overrides). This script assembles the pages, the stylesheet, the sitemap and the
Vercel config into a plain folder (default: dist/). No npm, no framework.

  python3 scripts/build_site.py presets                       # the 5 design presets
  python3 scripts/build_site.py blocks hero                   # slots + example for a block
  python3 scripts/build_site.py site.json --out dist          # build (same as: build site.json)
  python3 scripts/build_site.py site.json --preset studio-dark --accent "#FF5A36"
  python3 scripts/build_site.py tokens editorial              # colors, fonts, contrast ratios

Exit codes: 0 built, 1 problems in site.json (each one names the block and slot), 2 usage error.
Use `python` if `python3` is missing. Python 3.8+, standard library only, no network.
"""
from __future__ import annotations

import argparse
import base64
import colorsys
import difflib
import hashlib
import html
import json
import os
import re
import shutil
import struct
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
ASSETS = SKILL / "assets"
VERSION = "2.0.0"
MARK = "built with ship-it build_site.py"
COMMANDS = ("build", "presets", "tokens", "blocks", "schema")
EM_DASH = chr(0x2014)


class SiteError(Exception):
    """A problem the user can fix; printed as one clear message, never a traceback."""


class UsageError(SiteError):
    """Bad command line or missing file: exit code 2."""


# ---------------------------------------------------------------- colors (WCAG maths)

def norm_hex(value) -> str:
    s = str(value).strip()
    if not re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", s):
        raise ValueError("not a hex color like #B0481D")
    if len(s) == 4:
        s = "#" + "".join(ch * 2 for ch in s[1:])
    return s.upper()


def _rgb(h: str):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _to_hex(r, g, b) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(x)))) for x in (r, g, b))


def _lin(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(h: str) -> float:
    r, g, b = _rgb(h)
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a: str, b: str) -> float:
    """WCAG 2 contrast ratio between two hex colors (1 to 21)."""
    la, lb = luminance(a), luminance(b)
    hi, lo = (la, lb) if la >= lb else (lb, la)
    return (hi + 0.05) / (lo + 0.05)


def mix(a: str, b: str, t: float) -> str:
    """t of color a mixed into color b (t=0.12 gives a light tint of a over b)."""
    ra, rb = _rgb(a), _rgb(b)
    return _to_hex(*[x * t + y * (1 - t) for x, y in zip(ra, rb)])


def _shift(h: str, dl: float) -> str:
    r, g, b = [c / 255.0 for c in _rgb(h)]
    hh, ll, ss = colorsys.rgb_to_hls(r, g, b)
    ll = max(0.0, min(1.0, ll + dl))
    r, g, b = colorsys.hls_to_rgb(hh, ll, ss)
    return _to_hex(r * 255, g * 255, b * 255)


def readable(color: str, backgrounds, target: float = 4.5) -> str:
    """Nudge a color's lightness (keeping its hue) until it reads on every background."""
    if min(contrast(color, b) for b in backgrounds) >= target:
        return color
    light = sum(luminance(b) for b in backgrounds) / len(backgrounds) > 0.4
    step, cur = (-0.01 if light else 0.01), color
    for _ in range(100):
        cur = _shift(cur, step)
        if min(contrast(cur, b) for b in backgrounds) >= target:
            return cur
    return "#000000" if light else "#FFFFFF"


def pick_ink(accent: str, candidates) -> str:
    for c in candidates:
        if contrast(c, accent) >= 4.5:
            return c
    return max(candidates, key=lambda c: contrast(c, accent))


def hover_of(accent: str, ink: str) -> str:
    """Hover shade of the accent that keeps the button label readable."""
    direction = -1 if luminance(ink) > luminance(accent) else 1
    for d in (0.08, 0.05, 0.03):
        cand = _shift(accent, direction * d)
        if contrast(ink, cand) >= 4.5:
            return cand
    return accent


PAIRS = [
    ("text", "bg", 4.5), ("text", "surface", 4.5), ("muted", "bg", 4.5), ("muted", "surface", 4.5),
    ("accent_ink", "accent", 4.5), ("accent", "bg", 3.0), ("accent", "surface", 3.0),
    ("danger", "bg", 4.5), ("danger", "surface", 4.5),
    ("text", "accent_soft", 4.5), ("accent_text", "bg", 4.5), ("accent_text", "surface", 4.5),
    ("accent_text", "accent_soft", 4.5),
]
COLOR_KEYS = ["bg", "surface", "text", "muted", "border", "accent", "accent_ink", "danger"]


def finish_palette(c: dict) -> dict:
    """Add the derived colors (tint, readable accent text, hover) to a base palette."""
    c = dict(c)
    c["accent_soft"] = mix(c["accent"], c["bg"], 0.12)
    c["accent_text"] = readable(c["accent"], [c["bg"], c["surface"], c["accent_soft"]], 4.5)
    c["accent_hover"] = hover_of(c["accent"], c["accent_ink"])
    return c


def palette_issues(c: dict, label: str) -> list:
    out = []
    for a, b, need in PAIRS:
        got = contrast(c[a], c[b])
        if got < need:
            hint = ""
            if a == "accent" and b == "bg":
                hint = f" Try {readable(c['accent'], [c['bg'], c['surface']], need)}."
            out.append(f"{label}: {a.replace('_', '-')} {c[a]} on {b} {c[b]} is {got:.2f}:1, needs {need:g}:1.{hint}")
    return out


# ---------------------------------------------------------------- presets and tokens

PRESET_DIR = ASSETS / "presets"
SCALE_KEYS = ["xs", "sm", "base", "lg", "xl", "2xl", "3xl", "display"]
STYLE_DEFAULTS = {
    "heading_weight": "600", "heading_tracking": "-0.01em", "heading_case": "none", "heading_stretch": "100%",
    "lh_heading": "1.12", "lh_body": "1.6", "eyebrow_case": "uppercase", "eyebrow_tracking": "0.12em", "eyebrow_size": "var(--fs-xs)",
    "hero_measure": "20ch",
    "em_weight": "inherit", "btn_radius": "var(--radius-md)", "btn_weight": "600", "btn_case": "none",
    "btn_tracking": "0", "btn_border": "1px", "btn_shadow": "none", "btn_hover_shadow": "none",
    "btn_hover_transform": "none", "card_border_w": "1px", "rule_w": "1px",
    "header_border": "1px solid var(--border)", "measure": "62ch",
}
BUILDER_STYLE = {"decor": "none", "hero_align": "left"}
DECORS = ("none", "glow", "blob", "wash", "grid")
SPACING = [(1, 1), (2, 2), (3, 3), (4, 4), (5, 6), (6, 8), (7, 12), (8, 16), (9, 24)]


def preset_names() -> list:
    return sorted(p.stem for p in PRESET_DIR.glob("*.json"))


def suggest(word: str, options) -> str:
    m = difflib.get_close_matches(str(word), [str(o) for o in options], n=1)
    return f' Did you mean "{m[0]}"?' if m else ""


def load_json(path, what: str):
    try:
        if str(path) == "-":
            text = sys.stdin.read()
        else:
            text = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise UsageError(f"{what} not found: {path}")
    except (OSError, UnicodeDecodeError) as ex:
        raise UsageError(f"cannot read {what} {path}: {ex}")
    if not text.strip():
        raise SiteError(f"{what} is empty: {path}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as ex:
        raise SiteError(f"{what} is not valid JSON (line {ex.lineno}, column {ex.colno}): {ex.msg}")


def load_preset(name: str) -> dict:
    names = preset_names()
    if name not in names:
        raise SiteError(f'unknown preset "{name}".{suggest(name, names)} Available: {", ".join(names)}')
    return load_json(PRESET_DIR / f"{name}.json", f"preset {name}")


def fluid(lo, hi, vmin: float = 20.0, vmax: float = 80.0) -> str:
    """Fluid font size: lo rem at a 320px screen growing to hi rem at 1280px."""
    lo, hi = float(lo), float(hi)
    if hi < lo:
        lo, hi = hi, lo
    if hi - lo < 1e-9:
        return f"{lo:g}rem"
    slope = (hi - lo) / (vmax - vmin)
    return f"clamp({lo:g}rem, {lo - slope * vmin:.4f}rem + {slope * 100:.4f}vw, {hi:g}rem)"


def _merge(base: dict, extra) -> dict:
    out = dict(base)
    if isinstance(extra, dict):
        for k, v in extra.items():
            out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _css_value(v, what: str) -> str:
    s = str(v).strip()
    if not s or re.search(r"[{};<>]|/\*|\*/", s):
        raise SiteError(f"{what}: {s!r} is not a plain CSS value (no braces, semicolons or comments)")
    return s


def build_tokens(preset: dict, overrides=None, accent=None, fonts_mode: str = "google", dark_mode: bool = True) -> dict:
    """Merge preset + site overrides, derive the extra colors, and check every contrast pair."""
    ov = dict(overrides) if isinstance(overrides, dict) else {}
    if not isinstance(overrides, (dict, type(None))):
        raise SiteError("tokens must be an object like {\"colors\": {\"accent\": \"#B0481D\"}}")
    known = {"colors", "dark", "radius", "space_unit", "container", "shadow", "type_scale", "fonts", "style", "accent"}
    for k in ov:
        if k not in known:
            raise SiteError(f'unknown tokens key "{k}".{suggest(k, known)} Allowed: {", ".join(sorted(known))}')
    ov_colors = dict(ov.get("colors") or {})
    if ov.get("accent"):
        ov_colors.setdefault("accent", ov["accent"])
    if accent:
        ov_colors["accent"] = accent
    accent_given = "accent" in ov_colors
    ink_given = "accent_ink" in ov_colors

    def clean(colors: dict, label: str) -> dict:
        out = {}
        for k, v in colors.items():
            key = k.replace("-", "_")
            if key not in COLOR_KEYS + ["accent_2"]:
                raise SiteError(f'{label}: unknown color "{k}".{suggest(key, COLOR_KEYS + ["accent_2"])}')
            try:
                out[key] = norm_hex(v)
            except ValueError:
                raise SiteError(f"{label}.{k}: {v!r} is not a hex color like #B0481D")
        return out

    light = _merge(preset["colors"], clean(ov_colors, "tokens.colors"))
    if accent_given and not ink_given:
        light["accent_ink"] = pick_ink(light["accent"], [preset["colors"]["accent_ink"], light["text"], light["bg"], "#FFFFFF", "#000000"])
    dark = None
    if dark_mode and (preset.get("dark") or ov.get("dark")):
        d_over = clean(dict(ov.get("dark") or {}), "tokens.dark")
        dark = _merge(_merge(light, preset.get("dark") or {}), d_over)
        if accent_given and "accent" not in d_over:
            dark["accent"] = readable(light["accent"], [dark["bg"], dark["surface"]], 4.5)
            if "accent_ink" not in d_over:
                dark["accent_ink"] = pick_ink(dark["accent"], [dark["bg"], "#FFFFFF", "#000000"])
    light, dark = finish_palette(light), (finish_palette(dark) if dark else None)
    issues = palette_issues(light, "colors")
    if dark:
        issues += palette_issues(dark, "dark colors")

    fonts = _merge(preset["fonts"], ov.get("fonts") or {})
    for role in ("heading", "body", "accent"):
        f = fonts.get(role)
        if f is None:
            continue
        if not isinstance(f, dict) or not f.get("family"):
            raise SiteError(f"tokens.fonts.{role} needs {{\"family\", \"google\", \"fallback\"}}")
        if f.get("google") and not re.fullmatch(r"[A-Za-z0-9+ ]+(:[A-Za-z0-9,.;@ ]+)?", str(f["google"])):
            raise SiteError(f"tokens.fonts.{role}.google {f['google']!r} is not a Google Fonts family spec like Fraunces:wght@400..700")
        f.setdefault("fallback", "system-ui, sans-serif")
    radius = _merge(preset["radius"], ov.get("radius") or {})
    scale = _merge(preset["type_scale"], ov.get("type_scale") or {})
    style = dict(STYLE_DEFAULTS)
    style.update(BUILDER_STYLE)
    style.update(preset.get("style") or {})
    style.update(ov.get("style") or {})
    for k in style:
        if k not in STYLE_DEFAULTS and k not in BUILDER_STYLE:
            raise SiteError(f'unknown style key "{k}".{suggest(k, list(STYLE_DEFAULTS) + list(BUILDER_STYLE))}')
    if style["decor"] not in DECORS:
        raise SiteError(f'style.decor "{style["decor"]}" must be one of: {", ".join(DECORS)}')
    if style["hero_align"] not in ("left", "center"):
        raise SiteError("style.hero_align must be left or center")
    for k, v in style.items():
        _css_value(v, f"style.{k}")
    for k in radius:
        _css_value(radius[k], f"radius.{k}")
    space = _css_value(ov.get("space_unit", preset.get("space_unit", "0.25rem")), "space_unit")
    if not re.fullmatch(r"\d*\.?\d+rem", space):
        raise SiteError(f"space_unit {space!r} must be a rem value like 0.25rem")
    return {
        "name": preset["name"], "label": preset.get("label", preset["name"]), "colors": light, "dark": dark,
        "fonts": fonts, "fonts_mode": fonts_mode, "radius": radius, "space_unit": space,
        "container": _css_value(ov.get("container", preset.get("container", "70rem")), "container"),
        "shadow": _css_value(ov.get("shadow", preset.get("shadow", "none")), "shadow"),
        "scale": {k: (fluid(*v) if isinstance(v, (list, tuple)) and len(v) == 2 else _css_value(v, f"type_scale.{k}")) for k, v in scale.items()},
        "style": style, "issues": issues,
    }


def font_stack(font: dict, mode: str) -> str:
    if mode == "google" and font.get("google") and font.get("family"):
        return f'"{font["family"]}", {font["fallback"]}'
    return font["fallback"]


def google_fonts_url(tokens: dict):
    if tokens["fonts_mode"] != "google":
        return None
    specs = []
    for role in ("heading", "body", "accent"):
        f = tokens["fonts"].get(role)
        if f and f.get("google") and f["google"] not in specs:
            specs.append(f["google"])
    if not specs:
        return None
    return "https://fonts.googleapis.com/css2?" + "&".join("family=" + s.replace(" ", "+") for s in specs) + "&display=swap"


def _color_vars(c: dict) -> str:
    names = COLOR_KEYS + ["accent_text", "accent_hover", "accent_soft"] + (["accent_2"] if c.get("accent_2") else [])
    return ";".join(f"--{k.replace('_', '-')}:{c[k]}" for k in names)


def css_variables(tokens: dict) -> str:
    """The :root block. Names are a contract with base.css and scripts/check_site.py."""
    t, st = tokens, tokens["style"]
    lines = [
        _color_vars(t["colors"]),
        f"--radius-sm:{t['radius']['sm']};--radius-md:{t['radius']['md']};--radius-lg:{t['radius']['lg']}",
        f"--font-heading:{font_stack(t['fonts']['heading'], t['fonts_mode'])};--font-body:{font_stack(t['fonts']['body'], t['fonts_mode'])}",
    ]
    if t["fonts"].get("accent"):
        lines.append(f"--font-accent:{font_stack(t['fonts']['accent'], t['fonts_mode'])}")
    lines.append(";".join(f"--fs-{k}:{v}" for k, v in t["scale"].items()))
    lines.append(";".join(f"--{k.replace('_', '-')}:{st[k]}" for k in STYLE_DEFAULTS))
    lines.append(f"--u:{t['space_unit']};" + ";".join(f"--sp-{n}:calc(var(--u) * {m})" for n, m in SPACING))
    lines.append(f"--container:{t['container']};--shadow:{t['shadow']}")
    lines.append("--gutter:clamp(1rem, 4vw, 2rem);--section-y:clamp(calc(var(--u) * 14), 9vw, calc(var(--u) * 28));--gap:clamp(1rem, 2.5vw, 2rem)")
    scheme = "light dark" if t["dark"] else ("dark" if luminance(t["colors"]["bg"]) < 0.2 else "light")
    lines.append(f"color-scheme:{scheme}")
    body = ";\n  ".join(lines)
    out = f":root{{\n  {body};\n}}\n"
    if t["dark"]:
        out += f"@media (prefers-color-scheme: dark){{\n  :root{{{_color_vars(t['dark'])}}}\n}}\n"
    return out


# ---------------------------------------------------------------- tiny template language
#   {{var}} escaped   {{{var}}} raw   {{#each list}}..{{/each}}   {{#if x}}..{{else}}..{{/if}}
#   {{#if !x}}  {{#if x=value}}  {{! comment }}   inside each: this, @index, @number, @number2, @first, @last

class SafeStr(str):
    """Text that is already HTML-safe; {{var}} will not escape it again."""


class TemplateError(SiteError):
    pass


TAG_RE = re.compile(r"\{\{\{\s*(.+?)\s*\}\}\}|\{\{\s*(.+?)\s*\}\}", re.S)


def parse_template(src: str, name: str = "template"):
    root, stack, cur, pos = [], [], None, 0
    cur = root
    for m in TAG_RE.finditer(src):
        if m.start() > pos:
            cur.append(("text", src[pos:m.start()]))
        pos = m.end()
        line = src.count("\n", 0, m.start()) + 1
        if m.group(1) is not None:
            cur.append(("var", m.group(1).strip(), True))
            continue
        e = m.group(2).strip()
        if e.startswith("!"):
            continue
        if e.startswith("#each ") or e.startswith("#if "):
            kind = "each" if e.startswith("#each ") else "if"
            node = (kind, e.split(None, 1)[1].strip(), [], [])
            cur.append(node)
            stack.append((node, cur))
            cur = node[2]
        elif e == "else":
            if not stack or stack[-1][0][0] != "if":
                raise TemplateError(f"{name} line {line}: {{{{else}}}} outside {{{{#if}}}}")
            cur = stack[-1][0][3]
        elif e in ("/each", "/if"):
            if not stack or stack[-1][0][0] != e[1:]:
                raise TemplateError(f"{name} line {line}: unexpected {{{{{e}}}}}")
            _, cur = stack.pop()
        elif e.startswith("#") or e.startswith("/") or e.startswith(">"):
            raise TemplateError(f"{name} line {line}: unknown tag {{{{{e}}}}}")
        else:
            cur.append(("var", e, False))
    if stack:
        raise TemplateError(f"{name}: {{{{#{stack[-1][0][0]} {stack[-1][0][1]}}}}} is never closed")
    if pos < len(src):
        cur.append(("text", src[pos:]))
    return root


def _lookup(scopes, path: str):
    parts = path.split(".")
    val = None
    for sc in reversed(scopes):
        if isinstance(sc, dict) and parts[0] in sc:
            val = sc[parts[0]]
            break
    else:
        return None
    for p in parts[1:]:
        if isinstance(val, dict):
            val = val.get(p)
        else:
            return None
    return val


def _cond(scopes, expr: str) -> bool:
    neg = expr.startswith("!")
    if neg:
        expr = expr[1:].strip()
    if "=" in expr:
        left, right = expr.split("=", 1)
        res = str(_lookup(scopes, left.strip())) == right.strip().strip("\"'")
    else:
        res = bool(_lookup(scopes, expr))
    return (not res) if neg else res


def _fmt(v, raw: bool, path: str) -> str:
    if v is None or v is False:
        return ""
    if isinstance(v, (dict, list, tuple)):
        raise TemplateError(f"{{{{{path}}}}} is a {type(v).__name__}; print one of its fields instead")
    if isinstance(v, bool):
        return "true"
    s = v if isinstance(v, str) else str(v)
    return s if raw or isinstance(v, SafeStr) else html.escape(s, quote=True)


def _render(nodes, scopes, out) -> None:
    for n in nodes:
        kind = n[0]
        if kind == "text":
            out.append(n[1])
        elif kind == "var":
            out.append(_fmt(_lookup(scopes, n[1]), n[2], n[1]))
        elif kind == "each":
            seq = _lookup(scopes, n[1]) or []
            if not isinstance(seq, (list, tuple)):
                raise TemplateError(f"{{{{#each {n[1]}}}}}: {n[1]} is not a list")
            total = len(seq)
            for i, item in enumerate(seq):
                meta = {"this": item, "@index": i, "@number": i + 1, "@number2": "%02d" % (i + 1),
                        "@first": i == 0, "@last": i == total - 1}
                scopes.append(item if isinstance(item, dict) else {})
                scopes.append(meta)
                _render(n[2], scopes, out)
                scopes.pop()
                scopes.pop()
        else:
            _render(n[2] if _cond(scopes, n[1]) else n[3], scopes, out)


def render_template(nodes, context: dict) -> SafeStr:
    out: list = []
    _render(nodes, [context], out)
    return SafeStr("".join(out))


def template_vars(nodes, prefix=()):
    """Every variable path a template prints or tests (used by the tests to catch typos)."""
    found = []
    for n in nodes:
        if n[0] == "var":
            found.append(n[1])
        elif n[0] == "each":
            found.append(n[1])
            found += ["%s>%s" % (n[1], v) for v in template_vars(n[2])]
        elif n[0] == "if":
            found.append(n[1].lstrip("!").split("=")[0].strip())
            found += template_vars(n[2]) + template_vars(n[3])
    return found


_TEMPLATES: dict = {}


def get_template(name: str):
    if name not in _TEMPLATES:
        path = ASSETS / "blocks" / f"{name}.html"
        try:
            _TEMPLATES[name] = parse_template(path.read_text(encoding="utf-8"), f"blocks/{name}.html")
        except FileNotFoundError:
            raise SiteError(f"template missing: assets/blocks/{name}.html")
    return _TEMPLATES[name]


# ---------------------------------------------------------------- light markup for copy

def _inline(text: str, link, allow_links: bool = True) -> str:
    """**bold**, *italic*, `code`, [label](url), newline. HTML in the text is always escaped."""
    s = html.escape(text.replace("\x00", ""), quote=False)
    keep: list = []

    def stash(markup: str) -> str:
        keep.append(markup)
        return "\x00%d\x00" % (len(keep) - 1)

    s = re.sub(r"`([^`\n]+)`", lambda m: stash("<code>%s</code>" % m.group(1)), s)
    if allow_links:
        def do_link(m):
            href = link(html.unescape(m.group(2)))
            return stash('<a href="%s">%s</a>' % (html.escape(href, quote=True), _inline(html.unescape(m.group(1)), link, False)))
        s = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", do_link, s)
    s = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])", r"<em>\1</em>", s)
    s = s.replace("\n", "<br>")
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], s)


def inline_html(text: str, link) -> SafeStr:
    return SafeStr(_inline(text.strip(), link))


def rich_html(text: str, link) -> SafeStr:
    """Paragraphs (blank line), '- ' bullet lists, '1. ' lists, '### ' subheads, '> ' quotes."""
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if all(re.match(r"\s*[-*]\s+", ln) for ln in lines):
            items = "".join("<li>%s</li>" % _inline(re.sub(r"^\s*[-*]\s+", "", ln), link) for ln in lines)
            out.append("<ul>%s</ul>" % items)
        elif all(re.match(r"\s*\d+[.)]\s+", ln) for ln in lines):
            items = "".join("<li>%s</li>" % _inline(re.sub(r"^\s*\d+[.)]\s+", "", ln), link) for ln in lines)
            out.append("<ol>%s</ol>" % items)
        elif lines[0].startswith("### "):
            out.append("<h3>%s</h3>" % _inline(lines[0][4:], link))
            if len(lines) > 1:
                out.append("<p>%s</p>" % _inline("\n".join(lines[1:]), link))
        elif all(ln.startswith(">") for ln in lines):
            out.append("<blockquote><p>%s</p></blockquote>" % _inline("\n".join(ln.lstrip(">").strip() for ln in lines), link))
        else:
            out.append("<p>%s</p>" % _inline("\n".join(lines), link))
    return SafeStr("".join(out))


def plain(text) -> str:
    """Text with the light markup and tags removed (for titles and meta descriptions)."""
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", str(text))
    s = re.sub(r"[*`]+", "", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


# ---------------------------------------------------------------- image sizes (for width/height attributes)

def image_size(path: Path):
    """Best-effort (width, height) of PNG, JPEG, GIF, WebP or SVG files; None when unknown."""
    try:
        head = path.read_bytes()[:65536] if path.suffix.lower() != ".svg" else path.read_bytes()[:4096]
    except OSError:
        return None
    try:
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", head[16:24])
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", head[6:10])
        if head[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(head):
                if head[i] != 0xFF:
                    i += 1
                    continue
                marker = head[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg = struct.unpack(">H", head[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", head[i + 5:i + 9])
                    return w, h
                i += 2 + seg
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            kind = head[12:16]
            if kind == b"VP8X":
                return 1 + int.from_bytes(head[24:27], "little"), 1 + int.from_bytes(head[27:30], "little")
            if kind == b"VP8L":
                bits = int.from_bytes(head[21:25], "little")
                return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", head[26:30])
                return w & 0x3FFF, h & 0x3FFF
        if path.suffix.lower() == ".svg":
            txt = head.decode("utf-8", "ignore")
            m = re.search(r'viewBox\s*=\s*"\s*[-\d.]+[ ,]+[-\d.]+[ ,]+([\d.]+)[ ,]+([\d.]+)', txt)
            if m:
                return int(float(m.group(1))), int(float(m.group(2)))
    except (struct.error, ValueError, IndexError):
        return None
    return None


# ---------------------------------------------------------------- assets: spec, icons

_SPEC: dict = {}
_ICONS: dict = {}


def spec() -> dict:
    if not _SPEC:
        _SPEC.update(load_json(ASSETS / "blocks.json", "assets/blocks.json"))
    return _SPEC


def icons() -> dict:
    if not _ICONS:
        try:
            src = (ASSETS / "icons.svg").read_text(encoding="utf-8")
        except OSError:
            raise SiteError("assets/icons.svg is missing")
        for m in re.finditer(r'<symbol id="([\w-]+)"([^>]*)>(.*?)</symbol>', src, re.S):
            _ICONS[m.group(1)] = (m.group(2), m.group(3))
    return _ICONS


def sprite_svg(names) -> str:
    syms = "".join('<symbol id="i-%s"%s>%s</symbol>' % (n, icons()[n][0], icons()[n][1]) for n in sorted(names) if n in icons())
    return '<svg class="sprite" width="0" height="0" aria-hidden="true" focusable="false">%s</svg>' % syms if syms else ""


def icon_svg(name: str) -> SafeStr:
    return SafeStr('<svg class="icon" aria-hidden="true"><use href="#i-%s"/></svg>' % name)


UI_DEFAULT = {
    "skip": "Skip to content", "menu": "Menu", "main_nav": "Main", "popular": "Most popular",
    "send": "Send message", "email_us": "Email us", "call_us": "Call us", "name": "Name", "email": "Email",
    "message": "Message", "phone": "Phone", "address": "Address", "hours": "Hours",
    "nf_title": "Page not found", "nf_text": "That page does not exist or has moved.", "nf_button": "Back to the home page",
}
BUSINESS_TYPES = {
    "Organization", "LocalBusiness", "ProfessionalService", "LegalService", "AccountingService", "FinancialService",
    "Restaurant", "CafeOrCoffeeShop", "Bakery", "BarOrPub", "Store", "HealthAndBeautyBusiness", "Dentist", "Physician",
    "MedicalBusiness", "RealEstateAgent", "HomeAndConstructionBusiness", "AutomotiveBusiness", "LodgingBusiness",
    "SportsActivityLocation", "EntertainmentBusiness", "TravelAgency", "EducationalOrganization", "NGO",
    "Corporation", "OnlineBusiness", "Photograph",
}
RESERVED_FILES = {"index.html", "styles.css", "404.html"}
TODO_RE = re.compile(r"\b(TODO|TBD|FIXME)\b|\[(?:your|insert|add)\b[^\]]*\]|\bYour (?:Company|Business|Name)\b|\bXXX+\b", re.I)


# ---------------------------------------------------------------- model and validation

class Ctx:
    """Collects every problem so one run reports them all."""

    def __init__(self):
        self.errors: list = []
        self.warnings: list = []
        self.todos: list = []
        self.notes: list = []
        self.dashes: list = []
        self.lorem: list = []

    def err(self, where: str, msg: str) -> None:
        item = f"{where}: {msg}"
        if item not in self.errors:
            self.errors.append(item)

    def warn(self, where: str, msg: str) -> None:
        item = f"{where}: {msg}"
        if item not in self.warnings:
            self.warnings.append(item)

    def scan(self, where: str, text: str) -> None:
        if EM_DASH in text and where not in self.dashes:
            self.dashes.append(where)
        if re.search(r"lorem ipsum|dolor sit amet", text, re.I):
            self.lorem.append(where)
        m = TODO_RE.search(text)
        if m:
            self.todos.append(f"{where}: {m.group(0)}")


class Page:
    def __init__(self, path: str, index: int):
        self.path, self.index = path, index
        self.rel = "" if path == "/" else path.strip("/") + "/"
        self.depth = 0 if path == "/" else path.strip("/").count("/") + 1
        self.file = "index.html" if path == "/" else self.rel + "index.html"
        self.blocks: list = []
        self.ids: set = set()
        self.title = ""
        self.description = ""
        self.nav_label = None
        self.explicit_title = None
        self.explicit_description = None
        self.last_tone = None
        self.absolute = False
        self.nav: dict = {}
        self.footer: dict = {}

    @property
    def label(self) -> str:
        return f'page "{self.path}"'


class Model:
    def __init__(self, ctx: Ctx, base_dir: Path):
        self.ctx, self.base_dir = ctx, base_dir
        self.pages: list = []
        self.by_path: dict = {}
        self.public: dict = {}
        self.hashes: dict = {}
        self.pending: list = []
        self.warned_remote = False
        self.tokens: dict = {}
        self.ui: dict = dict(UI_DEFAULT)
        self.name = ""
        self.lang = "en"
        self.url = ""
        self.raw: dict = {}
        self.uses_script = False
        self.forms: set = set()
        self.year = time.localtime().tm_year
        self.business: dict = {}
        self.og_image = None
        self.custom_css = ""
        self.public_dir = None
        self.preset = "editorial"
        self.not_found = None

    def file_hash(self, rel: str) -> str:
        if rel not in self.hashes:
            self.hashes[rel] = hashlib.sha1(self.public[rel].read_bytes()).hexdigest()[:8]
        return self.hashes[rel]


def norm_path(path: str):
    """'/about' -> '/about/'. Returns None for values that are not a page path."""
    p = "/" + str(path).strip().strip("/")
    if p == "/":
        return "/"
    if "." in p.rsplit("/", 1)[-1] or not re.fullmatch(r"(?:/[a-z0-9][a-z0-9_-]*)+", p):
        return None
    return p + "/"


class Links:
    """Turns authored links into page-relative URLs (so dist/ works at any depth and from disk)."""

    def __init__(self, model: Model, page: Page, absolute: bool = False):
        self.m, self.page, self.absolute = model, page, absolute
        self.prefix = "/" if absolute else "../" * page.depth

    def page_url(self, target: Page) -> str:
        return (self.prefix + target.rel) or "./"

    def asset(self, rel: str) -> str:
        return self.prefix + rel

    def resolve(self, raw, where: str, home_anchor: bool = False) -> str:
        m, ctx = self.m, self.m.ctx
        h = str(raw).strip()
        low = h.lower()
        if not h or h == "#":
            ctx.err(where, "empty link: use a page like /about, an #id on the page, mailto: or tel:")
            return "#"
        if low.startswith(("javascript:", "data:", "vbscript:")):
            ctx.err(where, f'unsafe link "{h[:30]}"')
            return "#"
        if low.startswith("mailto:"):
            if not re.fullmatch(r"mailto:[^@\s]+@[^@\s]+\.[^@\s?]+(\?.*)?", h, re.I):
                ctx.err(where, f'"{h}" is not a valid mailto: link')
            return h
        if low.startswith("tel:"):
            if not re.fullmatch(r"tel:\+?[\d\s().-]{5,}", h):
                ctx.err(where, f'"{h}" is not a valid tel: link (digits and + only)')
            return h
        if low.startswith(("http://", "https://")):
            if low.startswith("http://") and "localhost" not in low:
                ctx.warn(where, f"{h} is not https; use https:// if the site supports it")
            if re.search(r"example\.(com|org|net)|yourdomain|your-domain|domain\.com", low):
                ctx.warn(where, f"{h} looks like a placeholder address")
            return h
        if h.startswith("//"):
            ctx.err(where, f'"{h}" starts with //; write the full https:// address')
            return "#"
        if h.startswith("#"):
            target = m.by_path["/"] if home_anchor and "/" in m.by_path else self.page
            m.pending.append((target, h[1:], where))
            return "#" + h[1:] if target is self.page else self.page_url(target) + h
        path, _, frag = h.partition("#")
        key = norm_path(path)
        if key is not None and key in m.by_path:
            tgt = m.by_path[key]
            url = self.page_url(tgt)
            if frag:
                m.pending.append((tgt, frag, where))
                url += "#" + frag
            return url
        rel = path.lstrip("/")
        if rel in m.public:
            return self.asset(rel)
        pages = ", ".join(p.path for p in m.pages)
        ctx.err(where, f'link "{h}" goes nowhere: no page has that path (pages: {pages}) and public/ has no such file')
        return "#"


def as_text(m: Model, v, where: str):
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        m.ctx.err(where, f"must be text, not {type(v).__name__}")
        return None
    return str(v).replace("\x00", "").strip()


def soft_max(m: Model, sp: dict, s: str, where: str) -> None:
    lim = sp.get("max")
    if lim and len(s) > lim:
        m.ctx.warn(where, f"{len(s)} characters; aim for {lim} or fewer")


def norm_image(m: Model, v, where: str, links: Links, eager: bool = False):
    ctx = m.ctx
    if not isinstance(v, dict):
        ctx.err(where, 'must be an object like {"src": "img/team.jpg", "alt": "Three people at a desk"}')
        return None
    for k in v:
        if k not in ("src", "alt", "width", "height"):
            ctx.err(where, f'unknown key "{k}" (allowed: src, alt, width, height)')
    src, alt = v.get("src"), v.get("alt")
    if not isinstance(src, str) or not src.strip():
        ctx.err(where, 'needs "src" (a file inside public/ or an https:// address)')
        return None
    if not isinstance(alt, str):
        ctx.err(where, 'needs "alt" text describing the picture (use "" only if it is purely decorative)')
        return None
    src, alt = src.strip(), alt.strip()
    if re.search(r"\.(jpe?g|png|webp|gif|svg)$|^img_?\d+", alt, re.I):
        ctx.warn(where, f'alt text "{alt}" looks like a file name; describe what the picture shows')
    ctx.scan(where + ".alt", alt)
    size = None
    if src.lower().startswith("https://"):
        url = src
        if not m.warned_remote:
            m.warned_remote = True
            ctx.warn(where, "image is loaded from another website; copy it into public/ so it cannot disappear or slow the page")
    elif src.lower().startswith(("http://", "//", "data:")):
        ctx.err(where, f'image "{src[:40]}" must be a file inside public/ or an https:// address')
        return None
    else:
        rel = src.lstrip("/")
        if rel not in m.public:
            ctx.err(where, f'image "{src}" not found in public/ (put the file there; files: {", ".join(sorted(m.public)[:6]) or "none yet"})')
            return None
        url = links.asset(rel) + "?v=" + m.file_hash(rel)
        size = image_size(m.public[rel])
    w = v.get("width") if isinstance(v.get("width"), int) else (size[0] if size else None)
    h = v.get("height") if isinstance(v.get("height"), int) else (size[1] if size else None)
    attrs = (' width="%d" height="%d"' % (w, h)) if w and h else ""
    attrs += ' fetchpriority="high"' if eager else ' loading="lazy" decoding="async"'
    return {"src": url, "alt": alt, "attrs": SafeStr(attrs)}


def norm_value(m: Model, sp: dict, v, where: str, links, eager: bool = False):
    ctx = m.ctx
    t = sp.get("type", "text")
    if t in ("text", "inline"):
        s = as_text(m, v, where)
        if s is None:
            return None
        soft_max(m, sp, s, where)
        ctx.scan(where, s)
        return inline_html(s, lambda h: links.resolve(h, where)) if t == "inline" else s
    if t == "rich":
        parts = v if isinstance(v, list) else [v]
        texts = [as_text(m, p, where) for p in parts]
        if any(x is None for x in texts):
            return None
        s = "\n\n".join(texts)
        ctx.scan(where, s)
        return rich_html(s, lambda h: links.resolve(h, where))
    if t == "link":
        if not isinstance(v, dict) or set(v) - {"label", "href"}:
            ctx.err(where, 'must be {"label": "Book a call", "href": "#contact"}')
            return None
        label = as_text(m, v.get("label"), where + ".label") if v.get("label") not in (None, "") else None
        if not label or not v.get("href"):
            ctx.err(where, 'needs both "label" and "href"')
            return None
        if len(label) > 34:
            ctx.warn(where, f'button text is {len(label)} characters; short labels convert better')
        ctx.scan(where, label)
        return {"label": label, "href": links.resolve(v["href"], where)}
    if t == "image":
        return norm_image(m, v, where, links, eager)
    if t == "enum":
        for cand in sp["values"]:
            if str(cand) == str(v).strip() and not isinstance(v, bool):
                return cand
        ctx.err(where, f'"{v}" is not allowed; use one of: {", ".join(str(x) for x in sp["values"])}')
        return None
    if t == "bool":
        if not isinstance(v, bool):
            ctx.err(where, "must be true or false")
            return None
        return v
    if t == "number":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            ctx.err(where, "must be a number")
            return None
        return v
    if t == "icon":
        if v not in icons():
            ctx.err(where, f'unknown icon "{v}".{suggest(v, icons())} Icons: {", ".join(sorted(icons()))}')
            return None
        return v
    if t == "id":
        if not isinstance(v, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", v):
            ctx.err(where, f'id "{v}" must be lowercase letters, digits and hyphens, starting with a letter')
            return None
        return v
    if t == "email":
        s = as_text(m, v, where)
        if s is None or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s):
            ctx.err(where, f'"{v}" is not an email address')
            return None
        return s
    if t == "url":
        s = as_text(m, v, where)
        if s is None or not re.fullmatch(r"https://[^\s/]+\S*|http://localhost\S*", s):
            ctx.err(where, f'"{v}" must be a full https:// address')
            return None
        return s
    if t == "list":
        if not isinstance(v, list):
            ctx.err(where, "must be a list")
            return None
        lo, hi = sp.get("min_items", 0), sp.get("max_items", 10 ** 6)
        if len(v) < lo or len(v) > hi:
            ctx.err(where, f"has {len(v)} items; needs {lo} to {hi}" if lo else f"has {len(v)} items; the most allowed is {hi}")
        item = sp.get("of", "text")
        out = []
        for i, el in enumerate(v):
            w = f"{where}[{i + 1}]"
            if isinstance(item, dict):
                out.append(norm_obj(m, item, el, w, links, eager))
            else:
                out.append(norm_value(m, {"type": item}, el, w, links, eager))
        return out
    if t == "object":
        return norm_obj(m, sp["of"], v, where, links, eager)
    if t == "blocks":
        if not isinstance(v, list) or not v:
            ctx.err(where, "must be a non-empty list of blocks, each with a \"type\"")
            return None
        return v
    return v


def norm_obj(m: Model, slots: dict, raw, where: str, links, eager: bool = False) -> dict:
    ctx = m.ctx
    out: dict = {}
    if not isinstance(raw, dict):
        ctx.err(where, f"must be an object, not {type(raw).__name__}")
        return out
    for k in raw:
        if k not in slots:
            ctx.err(where, f'unknown slot "{k}".{suggest(k, slots)} Slots: {", ".join(slots)}')
    for name, sp in slots.items():
        v = raw.get(name)
        if v is None or v == "" or v == []:
            if sp.get("required"):
                ctx.err(where, f'missing required slot "{name}"')
            elif "default" in sp:
                out[name] = sp["default"]
            continue
        got = norm_value(m, sp, v, f"{where}.{name}", links, eager)
        if got is not None:
            out[name] = got
    return out


def _grid_cols(n: int) -> int:
    return {1: 1, 2: 2, 3: 3, 4: 2, 5: 3, 6: 3, 7: 3, 8: 4, 9: 3}.get(n, 3)


def fin_hero(m, page, b, where):
    if b.get("cta_secondary") and not b.get("cta"):
        m.ctx.err(where, "cta_secondary needs a cta as well")
    b["center"] = (b.get("align") or m.tokens["style"]["hero_align"]) == "center"
    b["compact"] = bool(b.get("compact"))
    if not b.get("cta") and not b["compact"]:
        m.ctx.warn(where, "no cta: a landing page needs one clear action")


def fin_features(m, page, b, where):
    b["rows"] = b.get("variant", "cards") == "rows"
    b["variant"] = b.get("variant", "cards")
    b["cols"] = 1 if b["rows"] else (int(b["columns"]) if b.get("columns") else _grid_cols(len(b["items"])))


def fin_split(m, page, b, where):
    b["has_media"] = bool(b.get("image") or b.get("panel"))
    if b.get("image") and b.get("panel"):
        m.ctx.warn(where, "both image and panel given; the image is shown and the panel is dropped")
        b.pop("panel")


def fin_steps(m, page, b, where):
    b["cols"] = {2: 2, 3: 3, 4: 4, 5: 3, 6: 3}.get(len(b["items"]), 3)


def fin_testimonials(m, page, b, where):
    n = len(b["items"])
    b["solo"] = n == 1
    b["cols"] = 1 if n == 1 else {2: 2, 3: 3, 4: 2, 5: 3, 6: 3}.get(n, 3)
    for it in b["items"]:
        it["initial"] = (it.get("name") or "?")[:1].upper()


def fin_pricing(m, page, b, where):
    b["count"] = len(b["plans"])
    if sum(1 for p in b["plans"] if p.get("featured")) > 1:
        m.ctx.warn(where, "more than one featured plan; highlight only one")
    for p in b["plans"]:
        if p.get("featured") and not p.get("badge"):
            p["badge"] = m.ui["popular"]


def fin_faq(m, page, b, where):
    for i, it in enumerate(b["items"]):
        it["open"] = bool(b.get("open_first")) and i == 0


def fin_stats(m, page, b, where):
    b["count"] = len(b["items"])


def fin_cta(m, page, b, where):
    if b.get("cta_secondary") and not b.get("cta"):
        m.ctx.err(where, "cta_secondary needs a cta as well")
    b["center"] = (b.get("align") or m.tokens["style"]["hero_align"]) == "center"


def fin_text(m, page, b, where):
    b["center"] = b.get("align") == "center"
    b["wide"] = b.get("width") == "wide"


def fin_contact(m, page, b, where):
    ctx = m.ctx
    phone = b.get("phone")
    if phone:
        digits = re.sub(r"[^\d+]", "", phone)
        if len(digits.lstrip("+")) < 6:
            ctx.err(where + ".phone", f'"{phone}" does not look like a phone number')
        b["tel"] = "tel:" + digits
    fa = b.get("form_action")
    if fa:
        if re.search(r"your[-_ ]?(form|id|endpoint)|YOUR_|xxxx|placeholder|\{\{", fa, re.I):
            ctx.err(where + ".form_action", "still looks like a placeholder; paste the real endpoint from the form service")
        host = urlsplit(fa)
        m.forms.add("%s://%s" % (host.scheme, host.netloc))
        ctx.warn(where + ".form_action", f"form entries are sent to {host.netloc}: the owner needs an account there, and one test submission before launch")
    else:
        ctx.notes.append(f"{where}: no form_action, so visitors get an email button. A static site cannot store form entries; add a form service (e.g. Formspree) and its https:// endpoint as form_action if a form is wanted.")
    if b.get("email"):
        subj = b.get("subject")
        b["action_href"] = "mailto:" + b["email"] + ("?subject=" + quote(subj) if subj else "")
    elif phone:
        b["action_href"] = b["tel"]
    b["button"] = b.get("button") or m.ui["send" if fa else ("email_us" if b.get("email") else "call_us")]
    if b.get("address"):
        lines = [html.escape(x.strip()) for x in b["address"].replace("\\n", "\n").split("\n") if x.strip()]
        b["address"] = SafeStr("<br>".join(lines))


FINISH = {
    "hero": fin_hero, "features": fin_features, "split": fin_split, "steps": fin_steps,
    "testimonials": fin_testimonials, "pricing": fin_pricing, "faq": fin_faq, "stats": fin_stats,
    "cta": fin_cta, "text": fin_text, "contact": fin_contact,
}


def norm_block(m: Model, page: Page, raw, i: int, links: Links):
    ctx, catalog = m.ctx, spec()["blocks"]
    where0 = f"{page.label} block {i + 1}"
    if not isinstance(raw, dict):
        ctx.err(where0, 'must be an object like {"type": "hero", ...}')
        return None
    t = raw.get("type")
    usable = [k for k, v in catalog.items() if not v.get("auto")]
    if t not in usable:
        ctx.err(where0, f'unknown block type "{t}".{suggest(t, usable)} Blocks: {", ".join(usable)}')
        return None
    where = f"{where0} ({t})"
    bs = catalog[t]
    slots = dict(spec()["common"])
    slots.update(bs["slots"])
    before = len(ctx.errors)
    b = norm_obj(m, slots, {k: v for k, v in raw.items() if k != "type"}, where, links, eager=(t == "hero"))
    if "tone" not in raw:
        b["tone"] = bs.get("tone", "auto")
    need = bs.get("needs_one_of")
    if need and not any(b.get(k) for k in need):
        ctx.err(where, f'needs at least one of: {", ".join(need)}')
    b["type"], b["_where"] = t, where
    if t in FINISH and len(ctx.errors) == before:
        FINISH[t](m, page, b, where)
    return b


def build_page_blocks(m: Model, page: Page, raw_blocks) -> None:
    links = Links(m, page)
    taken = {"main", "site-nav", "top"}
    for i, rb in enumerate(raw_blocks):
        b = norm_block(m, page, rb, i, links)
        if b is None:
            continue
        want, explicit = b.get("id") or b["type"], bool(b.get("id"))
        if want in taken:
            if explicit:
                m.ctx.err(b["_where"], f'id "{want}" is already used on this page')
            n = 2
            while f"{want}-{n}" in taken:
                n += 1
            want = f"{want}-{n}"
        b["id"] = want
        taken.add(want)
        page.ids.add(want)
        page.blocks.append(b)
    heroes = [b for b in page.blocks if b["type"] == "hero"]
    if page.blocks and page.blocks[0]["type"] != "hero":
        m.ctx.err(page.label, "the first block must be a hero: it carries the page's only h1 (use compact: true for a slim page header)")
    if len(heroes) > 1:
        m.ctx.err(page.label, "only one hero block per page (one h1 per page)")
    last = None
    for b in page.blocks:
        t = b["tone"]
        if t == "auto":
            t = "surface" if last in (None, "default") else "default"
        b["tone"] = last = t
    page.last_tone = last


def scan_public(m: Model, pub_dir: Path) -> None:
    for p in sorted(pub_dir.rglob("*")):
        rel = p.relative_to(pub_dir)
        if any(part.startswith(".") for part in rel.parts) or not p.is_file():
            continue
        key = rel.as_posix()
        if key in RESERVED_FILES:
            m.ctx.err("public", f"{key} would overwrite a file the builder writes; rename it")
            continue
        m.public[key] = p


def build_model(raw, base_dir: Path, opts: dict, ctx: Ctx) -> Model:
    if not isinstance(raw, dict):
        raise SiteError('site.json must be a JSON object like {"name": "...", "blocks": [...]}')
    m = Model(ctx, base_dir)
    m.raw = raw
    sslots = spec()["site"]["slots"]
    for k in raw:
        if k not in sslots:
            ctx.err("site.json", f'unknown key "{k}".{suggest(k, sslots)} Keys: {", ".join(sslots)}')
    nm = raw.get("name")
    if not isinstance(nm, str) or not nm.strip():
        raise SiteError('site.json needs "name" (the business or site name)')
    m.name = nm.strip()
    ctx.scan("name", m.name)
    m.lang = str(raw.get("lang") or "en").strip()
    if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*", m.lang):
        ctx.err("lang", f'"{m.lang}" is not a language code like en, fr or nl-BE')
    fonts_mode = opts.get("fonts") or raw.get("fonts") or "google"
    if fonts_mode not in ("google", "system"):
        raise SiteError('fonts must be "google" or "system"')
    m.preset = opts.get("preset") or raw.get("preset") or "editorial"
    m.tokens = build_tokens(load_preset(m.preset), raw.get("tokens"), opts.get("accent"), fonts_mode, raw.get("dark_mode", True) is not False)
    for issue in m.tokens["issues"]:
        ctx.err("tokens", issue)
    ui_raw = raw.get("ui")
    if ui_raw is not None:
        if not isinstance(ui_raw, dict):
            ctx.err("ui", 'must be an object like {"skip": "Aller au contenu"}')
        else:
            for k, v in ui_raw.items():
                if k not in UI_DEFAULT:
                    ctx.err("ui", f'unknown key "{k}".{suggest(k, UI_DEFAULT)} Keys: {", ".join(UI_DEFAULT)}')
                elif isinstance(v, str) and v.strip():
                    m.ui[k] = v.strip()
    if not m.lang.lower().startswith("en") and not ui_raw:
        ctx.warn("site", f'lang is "{m.lang}" but ui is empty, so the skip link, menu label, form labels and 404 page stay in English. Add "ui" translations (run: build_site.py blocks site)')
    if raw.get("url"):
        u = norm_value(m, sslots["url"], raw["url"], "url", None)
        if u:
            parts = urlsplit(u)
            if parts.path not in ("", "/") or parts.query or parts.fragment:
                ctx.err("url", "must be the site address only, like https://halden.co.uk (no path)")
            m.url = "%s://%s" % (parts.scheme, parts.netloc)
    if raw.get("year") is not None:
        m.year = norm_value(m, sslots["year"], raw["year"], "year", None) or time.localtime().tm_year
    else:
        m.year = time.localtime().tm_year
    biz = raw.get("business")
    m.business = norm_obj(m, sslots["business"]["of"], biz, "business", None) if biz is not None else {}
    if m.business.get("type") and m.business["type"] not in BUSINESS_TYPES:
        ctx.warn("business.type", f'"{m.business["type"]}" is not in the built-in list; using LocalBusiness or Organization instead')
        m.business["type"] = "LocalBusiness" if m.business.get("address") else "Organization"
    pub_opt = opts.get("public")
    pub_dir = Path(pub_opt) if pub_opt else None
    if not pub_dir and raw.get("public"):
        pub_dir = base_dir / str(raw["public"])
        pub_opt = str(raw["public"])
    if not pub_dir:
        pub_dir = base_dir / "public"
    if pub_dir.is_dir():
        m.public_dir = pub_dir
        scan_public(m, pub_dir)
    elif pub_opt:
        ctx.err("public", f"folder not found: {pub_dir}")
    cc = raw.get("custom_css")
    if cc:
        cs = as_text(m, cc, "custom_css") or ""
        if "{" in cs:
            m.custom_css = cs
        else:
            cp = base_dir / cs
            try:
                m.custom_css = cp.read_text(encoding="utf-8-sig")
            except OSError:
                ctx.err("custom_css", f"file not found: {cp} (give a path to a .css file or the CSS text itself)")
    raw_pages, raw_blocks = raw.get("pages"), raw.get("blocks")
    if raw_pages is not None and raw_blocks is not None:
        ctx.err("site.json", 'use either "pages" or "blocks", not both')
    if raw_pages is None:
        if raw_blocks is None:
            raise SiteError('site.json needs "blocks" (a one page site) or "pages"')
        raw_pages = [{"path": "/", "blocks": raw_blocks}]
    if not isinstance(raw_pages, list) or not raw_pages:
        raise SiteError('"pages" must be a non-empty list of pages')
    prepared = []
    for i, rp in enumerate(raw_pages):
        where = f"pages[{i + 1}]"
        po = norm_obj(m, sslots["pages"]["of"], rp, where, None)
        p = norm_path(po["path"]) if po.get("path") else None
        if p is None:
            if po.get("path"):
                ctx.err(where, f'path "{po["path"]}" must look like / or /about (lowercase letters, digits, hyphens)')
            continue
        if p in m.by_path or p == "/404/":
            ctx.err(where, f'path "{p}" is used twice or reserved')
            continue
        page = Page(p, i)
        page.explicit_title, page.explicit_description, page.nav_label = po.get("title"), po.get("description"), po.get("nav_label")
        m.pages.append(page)
        m.by_path[p] = page
        prepared.append((page, po.get("blocks")))
    if "/" not in m.by_path:
        ctx.err("pages", 'no page has path "/" (the home page)')
    for key in m.public:
        first = key.split("/")[0]
        if any(pg.path != "/" and pg.path.strip("/").split("/")[0] == first for pg in m.pages):
            ctx.err("public", f"{key} collides with the page /{first}/; rename the file or folder")
    for page, rb in prepared:
        if rb:
            build_page_blocks(m, page, rb)
    og = raw.get("og_image")
    m.og_image = None
    if og:
        og = as_text(m, og, "og_image")
        if og and og.lower().startswith("https://"):
            m.og_image = og
        elif og:
            rel = og.lstrip("/")
            if rel not in m.public:
                ctx.err("og_image", f'"{og}" not found in public/')
            elif not m.url:
                ctx.warn("og_image", "share image ignored until url is set (share cards need an absolute address)")
            else:
                m.og_image = "%s/%s" % (m.url, rel)
    return m


def is_current(m: Model, page: Page, href: str) -> bool:
    if "#" in href or href.startswith(("mailto:", "tel:", "http")):
        return False
    return norm_path(href) == page.path


def resolve_site_link(m: Model, raw, where: str, links: Links):
    if not isinstance(raw, dict) or not raw.get("label") or not raw.get("href"):
        m.ctx.err(where, 'must be {"label": "...", "href": "..."}')
        return None
    return {"label": str(raw["label"]).strip(), "href": links.resolve(raw["href"], where, home_anchor=True)}


def finalize_page(m: Model, page: Page) -> None:
    """Everything that depends on which page we are on: menu, footer, logo, title, description."""
    links, raw, sslots = Links(m, page, page.absolute), m.raw, spec()["site"]["slots"]
    items: list = []
    if raw.get("nav") is not None:
        nav = norm_value(m, sslots["nav"], raw["nav"], "nav", links) or []
        for i, it in enumerate(nav):
            href = links.resolve(it["href"], f"nav[{i + 1}]", home_anchor=True)
            items.append({"label": it["label"], "href": href, "current": is_current(m, page, it["href"])})
    else:
        home = m.by_path.get("/")
        if home is not None and home.nav_label:
            items.append({"label": home.nav_label, "href": links.page_url(home), "current": page is home})
        if home is not None:
            for b in home.blocks:
                if b.get("nav_label"):
                    href = ("#" + b["id"]) if page is home else links.page_url(home) + "#" + b["id"]
                    items.append({"label": b["nav_label"], "href": href, "current": False})
        for p in m.pages:
            if p.path != "/" and p.nav_label:
                items.append({"label": p.nav_label, "href": links.page_url(p), "current": p is page})
    if len(items) > 7:
        m.ctx.warn("nav", f"{len(items)} menu items; 7 or fewer stays readable on a phone")
    cta = resolve_site_link(m, raw["nav_cta"], "nav_cta", links) if raw.get("nav_cta") else None
    logo = norm_image(m, raw["logo"], "logo", links, eager=True) if raw.get("logo") else None
    fo = norm_obj(m, sslots["footer"]["of"], raw.get("footer") or {}, "footer", links) if raw.get("footer") is not None else {}
    cols = []
    for ci, c in enumerate(fo.get("columns", [])):
        cols.append({"title": c["title"], "links": [
            {"label": ln["label"], "href": links.resolve(ln["href"], f"footer.columns[{ci + 1}].links[{li + 1}]", True)}
            for li, ln in enumerate(c.get("links", []))]})
    social = [{"label": s["label"], "href": links.resolve(s["href"], f"footer.social[{si + 1}]", True)} for si, s in enumerate(fo.get("social", []))]
    contact = []
    biz = m.business
    if biz.get("email"):
        contact.append({"label": biz["email"], "href": "mailto:" + biz["email"]})
    if biz.get("phone"):
        contact.append({"label": biz["phone"], "href": "tel:" + re.sub(r"[^\d+]", "", biz["phone"])})
    page.nav = {"items": items, "cta": cta, "logo": logo, "has_menu": bool(items or cta),
                "home": links.page_url(m.by_path["/"]) if "/" in m.by_path else "./"}
    page.footer = {"text": fo.get("text"), "legal": fo.get("legal"), "columns": cols, "social": social, "contact": contact}
    hero = next((b for b in page.blocks if b["type"] == "hero"), None)
    headline = plain(hero.get("headline", "")) if hero else m.name
    t = page.explicit_title
    if t:
        page.title = t if (m.name.lower() in t.lower() or len(t) + len(m.name) + 3 > 60) else f"{t} | {m.name}"
    elif page.path == "/":
        tag = raw.get("tagline")
        page.title = f"{m.name}: {tag}" if tag else m.name
        if not tag:
            m.ctx.warn("home page", "title is only the business name; add a tagline (or a page title) that says what you do")
    else:
        page.title = f"{headline} | {m.name}"
    if len(page.title) > 65:
        m.ctx.warn(page.label, f"title is {len(page.title)} characters; search engines cut it near 60")
    d = page.explicit_description or (raw.get("description") if page.path == "/" else None)
    if not d and hero and hero.get("subhead"):
        d = plain(hero["subhead"])
        if len(d) > 158:
            d = d[:155].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    page.description = d or raw.get("description") or ""
    if len(page.description) < 50 and page.file != "404.html":
        m.ctx.warn(page.label, "meta description is missing or very short (aim for 70 to 160 characters); set description")


def check_anchors(m: Model) -> None:
    for tgt, frag, where in m.pending:
        if frag in tgt.ids or frag in ("main", "top"):
            continue
        m.ctx.err(where, f'link to #{frag} but {tgt.label} has no section with that id.{suggest(frag, tgt.ids)} Ids there: {", ".join(sorted(tgt.ids)) or "none"}')


# ---------------------------------------------------------------- pages, head, files

ARROW = icon_svg("arrow-right")
CHECK = icon_svg("check")
GENERATOR = "ship-it build_site.py " + VERSION
NAV_SCRIPT = (
    "document.documentElement.classList.add('js');document.addEventListener('DOMContentLoaded',function(){"
    "var b=document.querySelector('[data-nav-toggle]'),n=document.getElementById('site-nav');if(!b||!n)return;"
    "function s(o){b.setAttribute('aria-expanded',o?'true':'false');n.classList.toggle('is-open',o)}"
    "b.addEventListener('click',function(){s(b.getAttribute('aria-expanded')!=='true')});"
    "n.addEventListener('click',function(e){if(e.target.closest('a'))s(false)});"
    "document.addEventListener('keydown',function(e){if(e.key==='Escape'&&b.getAttribute('aria-expanded')==='true'){s(false);b.focus()}})})"
)
NAV_HASH = "sha256-" + base64.b64encode(hashlib.sha256(NAV_SCRIPT.encode("utf-8")).digest()).decode()


def validate_csp(m: Model):
    opt = m.raw.get("csp", True)
    if opt is True or opt is False:
        return opt
    if not isinstance(opt, dict):
        m.ctx.err("csp", 'must be false or an object like {"frame-src": ["https://www.youtube.com"]}')
        return True
    for k, v in opt.items():
        if not re.fullmatch(r"[a-z-]+", k) or not isinstance(v, list) or not all(isinstance(x, str) and not re.search(r"[;,\s]", x) for x in v):
            m.ctx.err("csp", f'{k}: must be a list of sources without spaces, commas or semicolons')
    return opt


def csp_header(m: Model):
    opt = validate_csp(m)
    if opt is False:
        return None
    google = m.tokens["fonts_mode"] == "google" and google_fonts_url(m.tokens)
    d = {
        "default-src": ["'self'"],
        "script-src": ["'self'"] + (["'%s'" % NAV_HASH] if m.uses_script else []),
        "style-src": ["'self'"] + (["https://fonts.googleapis.com"] if google else []),
        "font-src": ["'self'"] + (["https://fonts.gstatic.com"] if google else []),
        "img-src": ["'self'", "data:", "https:"],
        "connect-src": ["'self'"],
        "form-action": ["'self'"] + sorted(m.forms),
        "base-uri": ["'self'"], "frame-ancestors": ["'none'"], "object-src": ["'none'"],
    }
    if isinstance(opt, dict):
        for k, v in opt.items():
            d.setdefault(k, ["'self'"] if k.endswith("-src") else [])
            d[k] += [x for x in v if x not in d[k]]
    return "; ".join("%s %s" % (k, " ".join(v)) for k, v in d.items())


def json_ld(m: Model):
    biz = m.business
    if not biz and not m.url:
        return None
    org: dict = {"@type": biz.get("type", "Organization"), "name": m.name}
    if m.url:
        org["@id"] = m.url + "/#org"
        org["url"] = m.url + "/"
    if m.raw.get("description"):
        org["description"] = m.raw["description"]
    for src, dst in (("email", "email"), ("phone", "telephone"), ("price_range", "priceRange"), ("opening_hours", "openingHours"), ("same_as", "sameAs")):
        if biz.get(src):
            org[dst] = biz[src]
    a = biz.get("address")
    if a:
        org["address"] = {"@type": "PostalAddress", "streetAddress": a.get("street"), "addressLocality": a.get("city"),
                          "addressRegion": a.get("region"), "postalCode": a.get("postal"), "addressCountry": a.get("country")}
        org["address"] = {k: v for k, v in org["address"].items() if v}
    graph = [org]
    if m.url:
        graph.append({"@type": "WebSite", "@id": m.url + "/#website", "url": m.url + "/", "name": m.name,
                      "inLanguage": m.lang, "publisher": {"@id": m.url + "/#org"}})
    return {"@context": "https://schema.org", "@graph": graph}


def head_html(m: Model, page: Page, links: Links, css_hash: str, noindex: bool) -> str:
    e = html.escape
    c = m.tokens["colors"]
    h = ['<meta charset="utf-8">', '<meta name="viewport" content="width=device-width, initial-scale=1">',
         "<title>%s</title>" % e(page.title, quote=False)]
    if page.description:
        h.append('<meta name="description" content="%s">' % e(page.description))
    if m.tokens["dark"]:
        h.append('<meta name="theme-color" content="%s" media="(prefers-color-scheme: light)">' % c["bg"])
        h.append('<meta name="theme-color" content="%s" media="(prefers-color-scheme: dark)">' % m.tokens["dark"]["bg"])
    else:
        h.append('<meta name="theme-color" content="%s">' % c["bg"])
    h.append('<meta name="generator" content="%s">' % GENERATOR)
    is404 = page.file == "404.html"
    if noindex or is404:
        h.append('<meta name="robots" content="noindex">')
    elif m.url:
        h.append('<link rel="canonical" href="%s">' % e(m.url + page.path))
    h.append('<link rel="icon" href="%s" type="image/svg+xml">' % links.asset("favicon.svg"))
    gurl = google_fonts_url(m.tokens)
    if gurl:
        h.append('<link rel="preconnect" href="https://fonts.googleapis.com">')
        h.append('<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>')
        h.append('<link rel="stylesheet" href="%s">' % e(gurl))
    h.append('<link rel="stylesheet" href="%s?v=%s">' % (links.asset("styles.css"), css_hash))
    if not is404:
        og = [("og:type", "website"), ("og:site_name", m.name), ("og:title", page.title), ("og:description", page.description)]
        if "-" in m.lang:
            og.append(("og:locale", m.lang.replace("-", "_")))
        if m.url:
            og.append(("og:url", m.url + page.path))
        if m.og_image:
            og.append(("og:image", m.og_image))
        for k, v in og:
            if v:
                h.append('<meta property="%s" content="%s">' % (k, e(v)))
        tw = [("twitter:card", "summary_large_image" if m.og_image else "summary"), ("twitter:title", page.title), ("twitter:description", page.description)]
        if m.og_image:
            tw.append(("twitter:image", m.og_image))
        for k, v in tw:
            if v:
                h.append('<meta name="%s" content="%s">' % (k, e(v)))
        ld = json_ld(m) if page.path == "/" else None
        if ld:
            h.append('<script type="application/ld+json">%s</script>' % json.dumps(ld, ensure_ascii=False).replace("</", "<\\/"))
    if page.nav["has_menu"]:
        h.append("<script>%s</script>" % NAV_SCRIPT)
    return "\n".join(h)


def render_page(m: Model, page: Page, css_hash: str, noindex: bool = False, absolute: bool = False) -> str:
    links = Links(m, page, absolute)
    common = {"arrow": ARROW, "check": CHECK, "ui": m.ui, "site": {"name": m.name}}
    blocks = "\n".join(str(render_template(get_template(b["type"]), dict(b, **common))) for b in page.blocks)
    nav = render_template(get_template("nav"), dict(page.nav, sticky=bool(m.raw.get("nav_sticky")), name=m.name, ui=m.ui))
    foot = render_template(get_template("footer"), dict(page.footer, name=m.name, year=m.year, plain=(page.last_tone == "surface")))
    body = '<a class="skip-link" href="#main">%s</a>\n%s\n<main id="main" tabindex="-1">\n%s\n</main>\n%s' % (
        html.escape(m.ui["skip"]), nav, blocks, foot)
    used = set(re.findall(r'href="#i-([a-z0-9-]+)"', body))
    head = head_html(m, page, links, css_hash, noindex)
    return "<!doctype html>\n<html lang=\"%s\">\n<head>\n%s\n</head>\n<body class=\"decor-%s\">\n%s\n%s\n</body>\n</html>\n" % (
        html.escape(m.lang), head, m.tokens["style"]["decor"], sprite_svg(used), body)


def make_404(m: Model) -> Page:
    p = Page("/404/", -1)
    p.rel, p.file, p.depth, p.absolute = "404.html", "404.html", 0, True
    links = Links(m, p, True)
    nf = norm_obj(m, spec()["site"]["slots"]["not_found"]["of"], m.raw.get("not_found") or {}, "not_found", links)
    title = nf.get("title") or m.ui["nf_title"]
    text = nf.get("text") or inline_html(m.ui["nf_text"], lambda h: h)
    hero = {"type": "hero", "id": "hero", "tone": "default", "_where": "404", "headline": SafeStr(html.escape(title, quote=False)),
            "subhead": text, "cta": {"label": nf.get("button") or m.ui["nf_button"], "href": "/"}, "center": m.tokens["style"]["hero_align"] == "center", "compact": True}
    p.blocks, p.ids, p.last_tone = [hero], {"hero"}, "default"
    p.explicit_title = "%s | %s" % (title, m.name)
    return p


def favicon_svg(m: Model) -> str:
    c = m.tokens["colors"]
    letter = html.escape((re.sub(r"\W", "", m.name)[:1] or "S").upper(), quote=False)
    md = re.match(r"[\d.]+", m.tokens["radius"]["md"])
    rx = min(32, int(round(float(md.group(0)) * 1.8))) if md else 0
    font = html.escape(m.tokens["fonts"]["heading"]["fallback"])
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="%d" fill="%s"/>'
            '<text x="32" y="32" dy=".35em" text-anchor="middle" font-family="%s" font-size="38" font-weight="700" fill="%s">%s</text></svg>\n'
            % (rx, c["accent"], font, c["accent_ink"], letter))


def vercel_json(m: Model) -> str:
    immutable = [{"key": "Cache-Control", "value": "public, max-age=31536000, immutable"}]
    hdr = [
        {"key": "X-Content-Type-Options", "value": "nosniff"},
        {"key": "Referrer-Policy", "value": "strict-origin-when-cross-origin"},
        {"key": "X-Frame-Options", "value": "DENY"},
        {"key": "Permissions-Policy", "value": "camera=(), microphone=(), geolocation=(), payment=()"},
    ]
    csp = csp_header(m)
    if csp:
        hdr.append({"key": "Content-Security-Policy", "value": csp})
    rules = [{"source": "/(.*)", "headers": hdr}, {"source": "/styles.css", "headers": immutable},
             {"source": "/favicon.svg", "headers": [{"key": "Cache-Control", "value": "public, max-age=86400"}]}]
    for d in sorted({k.split("/")[0] for k in m.public if "/" in k}):
        if re.fullmatch(r"[A-Za-z0-9_-]+", d):
            rules.append({"source": "/%s/(.*)" % d, "headers": immutable})
    return json.dumps({"$schema": "https://openapi.vercel.sh/vercel.json", "cleanUrls": True, "trailingSlash": True, "headers": rules}, indent=2) + "\n"


def build_files(m: Model) -> dict:
    """Return {relative path: text} for everything the builder generates."""
    try:
        base = (ASSETS / "base.css").read_text(encoding="utf-8")
    except OSError:
        raise SiteError("assets/base.css is missing")
    css = "/* %s. Change site.json and rebuild instead of editing this file. */\n%s%s" % (GENERATOR, css_variables(m.tokens), base)
    if m.custom_css:
        css += "\n/* custom.css from site.json */\n" + m.custom_css.strip() + "\n"
    css_hash = hashlib.sha1(css.encode("utf-8")).hexdigest()[:8]
    noindex = bool(m.raw.get("noindex"))
    files = {"styles.css": css}
    for p in m.pages:
        files[p.file] = render_page(m, p, css_hash, noindex)
    nf = m.not_found
    files["404.html"] = render_page(m, nf, css_hash, True, absolute=True)
    if "favicon.svg" not in m.public:
        files["favicon.svg"] = favicon_svg(m)
    if "robots.txt" not in m.public:
        if noindex:
            files["robots.txt"] = "User-agent: *\nDisallow: /\n"
        else:
            files["robots.txt"] = "User-agent: *\nAllow: /\n" + ("\nSitemap: %s/sitemap.xml\n" % m.url if m.url else "")
    if m.url and not noindex and "sitemap.xml" not in m.public:
        urls = "".join("  <url><loc>%s</loc></url>\n" % html.escape(m.url + p.path, quote=False) for p in m.pages)
        files["sitemap.xml"] = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n%s</urlset>\n' % urls
    if "vercel.json" not in m.public:
        files["vercel.json"] = vercel_json(m)
    return files


def guard_out(out: Path, site_path, pub_dir) -> Path:
    out = out.resolve()
    if out in (Path(out.anchor), Path.home().resolve(), Path.cwd().resolve()):
        raise SiteError(f"refusing to build into {out}; use a dedicated folder such as dist")
    if site_path not in (None, "-"):
        sp = Path(site_path).resolve()
        if sp == out or out in sp.parents:
            raise SiteError("--out would contain site.json; choose a separate folder such as dist")
    if pub_dir is not None:
        pd = Path(pub_dir).resolve()
        if pd == out or out in pd.parents or pd in out.parents:
            raise SiteError("--out and the public folder must be separate folders")
    if out.exists():
        if not out.is_dir():
            raise SiteError(f"{out} is a file, not a folder")
        if any(out.iterdir()):
            try:
                mine = "ship-it build_site.py" in (out / "index.html").read_text(encoding="utf-8", errors="ignore")[:4000]
            except OSError:
                mine = False
            if not mine:
                raise SiteError(f"{out} already holds files that build_site.py did not make; use an empty --out or empty it first")
    return out


def write_site(m: Model, files: dict, out: Path) -> list:
    if out.exists():
        for child in out.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for rel, text in sorted(files.items()):
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        written.append(rel)
    for rel, src in sorted(m.public.items()):
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(src), str(dest))
        written.append(rel)
    return sorted(written)


def prepare(raw, base_dir: Path, opts: dict):
    """Validate everything and return (model, ctx). Nothing is written."""
    ctx = Ctx()
    m = build_model(raw, base_dir, opts, ctx)
    m.not_found = make_404(m)
    for p in m.pages + [m.not_found]:
        finalize_page(m, p)
    m.uses_script = any(p.nav["has_menu"] for p in m.pages + [m.not_found])
    validate_csp(m)
    check_anchors(m)
    if len(ctx.dashes):
        ctx.warn("copy", "em dashes found in %d place(s) (%s%s); they read as machine-written. Use a comma, colon or full stop." % (
            len(ctx.dashes), ", ".join(ctx.dashes[:3]), ", ..." if len(ctx.dashes) > 3 else ""))
    if ctx.lorem:
        ctx.err("copy", "placeholder text (lorem ipsum) in: " + ", ".join(ctx.lorem[:4]))
    return m, ctx


# ---------------------------------------------------------------- reference output: presets, tokens, blocks, schema

def slot_lines(slots: dict, indent: str = "  ", prefix: str = "") -> list:
    rows = []
    for name, sp in slots.items():
        t = sp.get("type", "text")
        star = "*" if sp.get("required") else ""
        extra = []
        if sp.get("max"):
            extra.append("max %d chars" % sp["max"])
        if t == "enum":
            extra.append("|".join(str(x) for x in sp["values"]))
        if t == "list":
            lo, hi = sp.get("min_items"), sp.get("max_items")
            extra.append("%s items" % (("%s to %s" % (lo, hi)) if lo and hi else ("up to %s" % hi if hi else "any number of")))
        if "default" in sp:
            extra.append("default %s" % json.dumps(sp["default"]))
        head = "%s%s%s" % (prefix, name, star)
        rows.append("%s%-22s %-8s %s%s" % (indent, head, t, ("(%s) " % ", ".join(extra)) if extra else "", sp.get("desc", "")))
        inner = sp.get("of")
        if isinstance(inner, dict):
            rows += slot_lines(inner, indent, prefix + name + ".")
    return rows


def cmd_presets(a) -> int:
    rows = []
    for n in preset_names():
        p = load_preset(n)
        rows.append({"name": n, "label": p.get("label", n), "best_for": p.get("best_for", ""), "mood": p.get("mood", ""),
                     "accent": p["colors"]["accent"], "dark_variant": bool(p.get("dark")), "fonts": {r: p["fonts"][r]["family"] for r in p["fonts"]}})
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        print("%-15s %s" % (r["name"], r["best_for"]))
        print("%-15s %s Accent %s%s." % ("", r["mood"], r["accent"], "; follows dark mode" if r["dark_variant"] else ""))
    print("\nUse one with: python3 scripts/build_site.py site.json --preset NAME   (or \"preset\" in site.json)")
    print("See its numbers with: python3 scripts/build_site.py tokens NAME")
    return 0


def cmd_tokens(a) -> int:
    t = build_tokens(load_preset(a.preset), None, a.accent)
    if a.json:
        print(json.dumps({k: v for k, v in t.items() if k != "fonts_mode"}, indent=2, ensure_ascii=False))
        return 1 if t["issues"] else 0
    p = load_preset(a.preset)
    print("%s: %s" % (t["label"], p.get("best_for", "")))
    print("fonts   heading %s, body %s%s" % (t["fonts"]["heading"]["family"], t["fonts"]["body"]["family"],
                                            (", accent " + t["fonts"]["accent"]["family"]) if t["fonts"].get("accent") else ""))
    for label, c in (("colors", t["colors"]), ("dark", t["dark"])):
        if not c:
            continue
        print("%s" % label)
        for k in COLOR_KEYS + ["accent_text", "accent_hover", "accent_soft"] + (["accent_2"] if c.get("accent_2") else []):
            print("  %-13s %s" % (k, c[k]))
        checks = ["%s/%s %.1f" % (x.replace("accent_", "a-"), y, contrast(c[x], c[y])) for x, y, _ in PAIRS[:7]]
        print("  contrast      " + ", ".join(checks))
    print("radius  sm %s, md %s, lg %s   container %s   space unit %s" % (t["radius"]["sm"], t["radius"]["md"], t["radius"]["lg"], t["container"], t["space_unit"]))
    print("style   " + ", ".join("%s=%s" % (k, v) for k, v in t["style"].items() if k in p.get("style", {})))
    for i in t["issues"]:
        print("problem " + i)
    print('\nOverride in site.json, for example:\n  "tokens": {"colors": {"accent": "#B0481D"}, "radius": {"md": "8px"}, "style": {"heading_weight": "700"}}')
    return 1 if t["issues"] else 0


def cmd_blocks(a) -> int:
    sp = spec()
    if a.name == "site":
        entry = {"desc": sp["site"]["desc"], "slots": sp["site"]["slots"]}
    else:
        entry = sp["blocks"].get(a.name) if a.name else None
    if a.name and entry is None:
        raise UsageError('unknown block "%s".%s Blocks: %s, site' % (a.name, suggest(a.name, sp["blocks"]), ", ".join(sp["blocks"])))
    if a.json:
        print(json.dumps(entry if a.name else {k: v.get("desc", "") for k, v in sp["blocks"].items()}, indent=2))
        return 0
    if not a.name:
        for k, v in sp["blocks"].items():
            print("%-13s %s" % (k, v["desc"]))
        print("%-13s %s" % ("site", sp["site"]["desc"]))
        print("\nDetails and an example: python3 scripts/build_site.py blocks NAME")
        return 0
    print("%s: %s" % (a.name, entry["desc"]))
    if entry.get("slots"):
        print("\nslots (* = required):")
        print("\n".join(slot_lines(entry["slots"])))
    if a.name != "site" and not entry.get("auto"):
        print("\nevery block also takes: " + ", ".join("%s (%s)" % (k, v["desc"].split(".")[0].lower()) for k, v in sp["common"].items()))
    if entry.get("example"):
        print("\nexample:\n  " + json.dumps(entry["example"], ensure_ascii=False))
    if a.name == "site":
        print("\nui keys (text the builder adds): " + ", ".join(UI_DEFAULT))
        print("presets: " + ", ".join(preset_names()))
    return 0


def slot_schema(sp: dict) -> dict:
    t = sp.get("type", "text")
    s: dict
    if t in ("text", "inline"):
        s = {"type": "string"}
    elif t == "rich":
        s = {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}
    elif t == "id":
        s = {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"}
    elif t == "email":
        s = {"type": "string", "format": "email"}
    elif t == "url":
        s = {"type": "string", "pattern": "^https://"}
    elif t == "link":
        s = {"type": "object", "required": ["label", "href"], "additionalProperties": False, "properties": {"label": {"type": "string"}, "href": {"type": "string"}}}
    elif t == "image":
        s = {"type": "object", "required": ["src", "alt"], "additionalProperties": False,
             "properties": {"src": {"type": "string"}, "alt": {"type": "string"}, "width": {"type": "integer"}, "height": {"type": "integer"}}}
    elif t == "enum":
        s = {"enum": sp["values"]}
    elif t == "icon":
        s = {"enum": sorted(icons())}
    elif t == "bool":
        s = {"type": "boolean"}
    elif t == "number":
        s = {"type": "number"}
    elif t == "list":
        inner = sp.get("of", "text")
        s = {"type": "array", "items": obj_schema(inner) if isinstance(inner, dict) else slot_schema({"type": inner})}
        if sp.get("min_items"):
            s["minItems"] = sp["min_items"]
        if sp.get("max_items"):
            s["maxItems"] = sp["max_items"]
    elif t == "object":
        s = obj_schema(sp["of"])
    elif t == "ui":
        s = {"type": "object", "additionalProperties": False, "properties": {k: {"type": "string"} for k in UI_DEFAULT}}
    elif t == "csp":
        s = {"oneOf": [{"type": "boolean"}, {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}}]}
    elif t == "blocks":
        s = {"type": "array", "minItems": 1, "items": {"oneOf": [{"$ref": "#/$defs/" + k} for k, v in spec()["blocks"].items() if not v.get("auto")]}}
    else:
        s = {"type": "object"}
    if sp.get("desc"):
        s["description"] = sp["desc"]
    return s


def obj_schema(slots: dict) -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": {k: slot_schema(v) for k, v in slots.items()},
            "required": [k for k, v in slots.items() if v.get("required")]}


def build_schema() -> dict:
    sp = spec()
    defs = {}
    for k, b in sp["blocks"].items():
        if b.get("auto"):
            continue
        slots = dict(sp["common"])
        slots.update(b["slots"])
        d = obj_schema(slots)
        d["properties"] = dict({"type": {"const": k}}, **d["properties"])
        d["required"] = ["type"] + d["required"]
        d["description"] = b["desc"]
        defs[k] = d
    top = obj_schema(sp["site"]["slots"])
    top.update({"$schema": "http://json-schema.org/draft-07/schema#", "$id": "site.schema.json", "title": "ship-it site.json",
                "description": "Input for scripts/build_site.py. Generated by: python3 scripts/build_site.py schema. Needs blocks (one page) or pages.",
                "anyOf": [{"required": ["pages"]}, {"required": ["blocks"]}], "$defs": defs})
    return top


def cmd_schema(a) -> int:
    print(json.dumps(build_schema(), indent=2, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- build command

def cmd_build(a) -> int:
    raw = load_json(a.site, "site file")
    base = Path.cwd() if a.site == "-" else Path(a.site).resolve().parent
    opts = {"preset": a.preset, "accent": a.accent, "fonts": a.fonts, "public": a.public}
    m, ctx = prepare(raw, base, opts)
    errors = list(ctx.errors)
    if a.strict and ctx.warnings:
        errors += ["(--strict) " + w for w in ctx.warnings]
    if errors:
        if a.json:
            print(json.dumps({"ok": False, "errors": errors, "warnings": ctx.warnings}, indent=2, ensure_ascii=False))
        else:
            print("%s has %d problem%s. Nothing was built.\n" % ("site.json" if a.site == "-" else a.site, len(errors), "" if len(errors) == 1 else "s"), file=sys.stderr)
            for i, e in enumerate(errors, 1):
                print("  %d. %s" % (i, e), file=sys.stderr)
            print("\nFix them in site.json and run the same command again.", file=sys.stderr)
        return 1
    files = build_files(m)
    out = guard_out(Path(a.out), a.site, getattr(m, "public_dir", None))
    written = write_site(m, files, out)
    total = sum((out / w).stat().st_size for w in written)
    google = m.tokens["fonts_mode"] == "google" and bool(google_fonts_url(m.tokens))
    result = {"ok": True, "out": str(out), "preset": m.preset, "fonts": m.tokens["fonts_mode"],
              "pages": [{"path": p.path, "file": p.file, "title": p.title} for p in m.pages],
              "files": written, "bytes": total, "warnings": ctx.warnings, "notes": ctx.notes, "todo": ctx.todos,
              "external_requests": ["fonts.googleapis.com", "fonts.gstatic.com"] if google else []}
    if a.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print("built %d page%s (preset %s, fonts %s) into %s" % (len(m.pages), "" if len(m.pages) == 1 else "s", m.preset, m.tokens["fonts_mode"], a.out))
    for w in written:
        print("  %-24s %6.1f KB" % (w, (out / w).stat().st_size / 1024.0))
    print("total %.0f KB. External requests: %s. Scripts: %s." % (
        total / 1024.0, "Google Fonts (use --fonts system to remove)" if google else "none",
        "one small inline script for the phone menu" if m.uses_script else "none"))
    for label, items in (("notes", ctx.notes), ("warnings", ctx.warnings), ("still open (TODO markers)", ctx.todos)):
        if items:
            print("\n%s (%d):" % (label, len(items)))
            for it in items:
                print("  - " + it)
    print("\nnext: python3 scripts/check_site.py %s   (preview: python3 -m http.server 8000 --directory %s)" % (a.out, a.out))
    return 0


def make_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="build_site.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", metavar="command")
    b = sub.add_parser("build", help="build SITE.json into a static folder (the default command)", description="Build SITE.json (use - for stdin) into --out.")
    b.add_argument("site", help="path to site.json, or - to read it from stdin")
    b.add_argument("--out", default="dist", help="output folder (default: dist). Emptied first, but only if empty or made by this script")
    b.add_argument("--preset", help="design preset, overrides site.json (see: presets)")
    b.add_argument("--accent", help="accent color as #hex; ink, hover and text shades are recalculated for contrast")
    b.add_argument("--fonts", choices=("google", "system"), help="google (default): load the preset fonts; system: no external requests")
    b.add_argument("--public", help="folder of images and files to copy as-is (default: public/ next to site.json)")
    b.add_argument("--strict", action="store_true", help="treat warnings as errors")
    b.add_argument("--json", action="store_true", help="machine-readable result")
    b.set_defaults(fn=cmd_build)
    p = sub.add_parser("presets", help="list the design presets", description="List the design presets with what each is best for.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_presets)
    t = sub.add_parser("tokens", help="show a preset's colors, fonts and contrast ratios", description="Print the design tokens of a preset.")
    t.add_argument("preset")
    t.add_argument("--accent", help="preview the preset with another accent color")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_tokens)
    k = sub.add_parser("blocks", help="list blocks, or show one block's slots and an example", description="Show what content each block takes. Use 'site' for the top level of site.json.")
    k.add_argument("name", nargs="?", help="block name, or site")
    k.add_argument("--json", action="store_true")
    k.set_defaults(fn=cmd_blocks)
    s = sub.add_parser("schema", help="print the JSON Schema of site.json", description="Print assets/site.schema.json, generated from assets/blocks.json.")
    s.set_defaults(fn=cmd_schema)
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and (argv[0] == "-" or not argv[0].startswith("-")):
        argv.insert(0, "build")
    ap = make_parser()
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return a.fn(a)
    except UsageError as ex:
        print("error: %s" % ex, file=sys.stderr)
        return 2
    except SiteError as ex:
        print("error: %s" % ex, file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
