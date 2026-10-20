#!/usr/bin/env python3
"""Check a built static site the way a stranger would, and scan it for leaked secrets.

  python3 scripts/check_site.py dist
  python3 scripts/check_site.py dist --json > report.json
  python3 scripts/check_site.py dist --root . --strict

Reads only: never fixes anything, never touches the network. It checks color contrast of
the design tokens, hard-coded colors and fonts, every HTML page (title, description, lang,
headings, alt text, links, labels, placeholders, share tags), accessibility signals, page
weight, the outside servers the site talks to, and secrets (API keys, tokens, .env files).
With --root it also scans the project's git-tracked or not-ignored files and .env files.
Use `-` as DIR to check one HTML page read from stdin.

Exit codes: 0 no errors, 1 errors found (or any warning with --strict), 2 usage error.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import posixpath
import re
import struct
import sys
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

E, W, I = "error", "warning", "info"
_RANK = {E: 0, W: 1, I: 2}

# rule id -> (default severity, default one-line fix)
RULES = {
    "contrast.text-bg": (E, "Darken --text or lighten --bg until the ratio is at least 4.5:1."),
    "contrast.text-surface": (E, "Darken --text or lighten --surface until the ratio is at least 4.5:1."),
    "contrast.muted-bg": (E, "Darken --muted or lighten --bg until the ratio is at least 4.5:1."),
    "contrast.muted-surface": (W, "Muted text on cards needs 4.5:1: darken --muted or lighten --surface."),
    "contrast.accent-ink": (E, "Change --accent-ink or --accent so button text reaches 4.5:1."),
    "contrast.accent-bg": (E, "Darken --accent or lighten --bg (buttons need 3:1, accent-colored text 4.5:1)."),
    "contrast.danger-bg": (W, "Darken --danger so error messages reach 4.5:1 on --bg."),
    "contrast.parse": (W, "Use a plain color (#hex, rgb(), hsl(), oklch()) so contrast can be checked."),
    "tokens.missing": (W, "Define them in :root of styles.css so contrast can be checked."),
    "tokens.extra": (I, "Extra variables are fine; keep colors and fonts in :root."),
    "tokens.hardcoded-color": (W, "Replace the literal color with a var(--...) token from :root."),
    "tokens.hardcoded-font": (W, "Use var(--font-body) or var(--font-heading) instead of naming a font here."),
    "css.focus-visible": (W, "Add :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }."),
    "css.reduced-motion": (W, "Put animations in @media (prefers-reduced-motion: no-preference), or switch them off for reduce."),
    "files.index": (E, "Put the home page at index.html in the folder you deploy."),
    "files.missing": (W, "Add the file (the builder writes it for you)."),
    "files.junk": (W, "Delete it from the deploy folder; it should not be public."),
    "files.vercel-json": (E, "Fix the JSON syntax in vercel.json; a deploy fails on invalid JSON."),
    "seo.robots": (W, "Remove 'Disallow: /' from robots.txt before going public."),
    "seo.noindex": (W, "Remove noindex from the robots meta tag unless the page must stay hidden."),
    "seo.sitemap": (W, "List real pages in sitemap.xml as <loc> URLs."),
    "html.parse": (W, "Fix the broken markup so the page can be read."),
    "html.doctype": (W, "Start the file with <!doctype html>."),
    "html.charset": (W, 'Add <meta charset="utf-8"> at the top of <head>.'),
    "html.lang": (E, 'Add lang to the opening tag, for example <html lang="en">.'),
    "html.viewport": (E, 'Add <meta name="viewport" content="width=device-width, initial-scale=1">.'),
    "html.zoom-blocked": (W, "Remove user-scalable=no and maximum-scale so people can pinch to zoom."),
    "html.title": (E, "Give every page a specific <title> of 10 to 60 characters."),
    "html.title-dup": (W, "Give each page its own title."),
    "html.description": (W, 'Add <meta name="description" content="..."> of 50 to 160 characters.'),
    "html.canonical": (W, 'Add <link rel="canonical" href="https://your-domain/page"> with the real address.'),
    "html.og": (W, "Add og:title, og:description and og:image (absolute URL) meta tags."),
    "html.og-image-abs": (W, "Use a full https:// URL for og:image; most crawlers ignore relative ones."),
    "html.twitter": (W, 'Add <meta name="twitter:card" content="summary_large_image">.'),
    "html.favicon": (W, 'Add <link rel="icon" href="/favicon.svg" type="image/svg+xml">.'),
    "html.h1": (E, "Use exactly one <h1> that names the page."),
    "html.heading-order": (W, "Do not skip heading levels (h2 then h3, never h2 then h4)."),
    "html.img-alt": (E, 'Add alt text; use alt="" only for purely decorative images.'),
    "html.img-alt-quality": (W, "Describe what the image shows instead of the file name or 'image'."),
    "html.link-empty": (E, "Give the link a real address, or make it a <button> if it runs an action."),
    "html.link-name": (E, "Add visible text or aria-label so a screen reader can say where it goes."),
    "html.link-text": (W, "Say where it goes: 'Read the pricing guide', not 'click here'."),
    "html.form-label": (E, "Add a <label for=...> to every field; placeholder text is not a label."),
    "html.form-label-placeholder": (W, "Add a visible <label>; placeholder text disappears while typing."),
    "html.form-action": (W, "Point the form at a real service, or replace it with a link or mailto:."),
    "html.dup-id": (W, "Make every id unique on the page."),
    "html.landmarks": (W, "Wrap the content in <main>; add <nav> and <footer> where they apply."),
    "a11y.skip-link": (W, 'Add <a class="skip" href="#main">Skip to content</a> as the first link.'),
    "links.broken": (E, "Fix the path or add the file; paths are case-sensitive once deployed."),
    "links.anchor": (E, "Add the matching id to the target, or fix the # link."),
    "links.localhost": (E, "Replace the local address with the real one or a relative path."),
    "net.insecure": (E, "Load it over https://."),
    "net.csp": (W, "Allow it in the Content-Security-Policy in vercel.json, or remove it; the live site blocks it."),
    "net.script": (W, "Remove third-party scripts unless the owner asked for them."),
    "net.tracker": (W, "Remove the tracker unless the owner asked for analytics."),
    "net.external": (I, "These are the outside servers a visitor's browser contacts."),
    "content.lorem": (E, "Replace the lorem ipsum with real copy."),
    "content.placeholder": (E, "Replace the placeholder with real text, or remove the element."),
    "content.example-domain": (W, "Put the real address in place of the example domain."),
    "weight.total": (I, "Compress images and drop unused files if the page feels slow."),
    "weight.image": (W, "Resize or compress it (WebP or AVIF, under 300 KB)."),
    "weight.file": (W, "Minify it, or drop the library if it is not used."),
    "secrets.key": (E, "Remove it, rotate the key at the provider, load it from an environment variable."),
    "secrets.file": (E, "Delete it from the site, keep it in a git-ignored .env, rotate any key inside."),
    "secrets.env-ignored": (I, "Fine: git ignores it. Never paste its contents into chat."),
    "secrets.public-key": (W, "Fine only if the key is restricted to your domain; otherwise rotate it."),
}

REQUIRED_COLORS = ("bg", "surface", "text", "muted", "border", "accent", "accent-ink", "danger")
REQUIRED_OTHER = ("radius-sm", "radius-md", "radius-lg", "font-heading", "font-body")
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".ico", ".svg", ".tiff")
BINARY_EXT = frozenset(
    ".png .jpg .jpeg .gif .webp .avif .bmp .ico .tiff .woff .woff2 .ttf .otf .eot .mp4 .webm .mov .mp3 .wav "
    ".ogg .m4a .zip .gz .tgz .bz2 .xz .7z .rar .pdf .bin .exe .dll .so .dylib .class .jar .psd .ai .sketch "
    ".fig .pyc .sqlite .sqlite3 .db .wasm .heic .mkv .avi".split())
PER_FILE_CAP = 200
MAX_FILES = 20000
MAX_TEXT = 24 * 1024 * 1024
IMG_LIMIT = 300 * 1024
BIG_TEXT_LIMIT = 100 * 1024
TOTAL_LIMIT = 3 * 1024 * 1024
SKIP_DIRS = frozenset((".git", "node_modules", ".vercel", "__pycache__"))
HEAVY_DIRS = frozenset((".git", "node_modules", ".next", ".vercel", ".venv", "venv", "__pycache__", ".cache", ".turbo"))

_NAMED_SRC = (
    "aliceblue:f0f8ff antiquewhite:faebd7 aqua:00ffff aquamarine:7fffd4 azure:f0ffff beige:f5f5dc "
    "bisque:ffe4c4 black:000000 blanchedalmond:ffebcd blue:0000ff blueviolet:8a2be2 brown:a52a2a "
    "burlywood:deb887 cadetblue:5f9ea0 chartreuse:7fff00 chocolate:d2691e coral:ff7f50 "
    "cornflowerblue:6495ed cornsilk:fff8dc crimson:dc143c cyan:00ffff darkblue:00008b darkcyan:008b8b "
    "darkgoldenrod:b8860b darkgray:a9a9a9 darkgreen:006400 darkgrey:a9a9a9 darkkhaki:bdb76b "
    "darkmagenta:8b008b darkolivegreen:556b2f darkorange:ff8c00 darkorchid:9932cc darkred:8b0000 "
    "darksalmon:e9967a darkseagreen:8fbc8f darkslateblue:483d8b darkslategray:2f4f4f "
    "darkslategrey:2f4f4f darkturquoise:00ced1 darkviolet:9400d3 deeppink:ff1493 deepskyblue:00bfff "
    "dimgray:696969 dimgrey:696969 dodgerblue:1e90ff firebrick:b22222 floralwhite:fffaf0 "
    "forestgreen:228b22 fuchsia:ff00ff gainsboro:dcdcdc ghostwhite:f8f8ff gold:ffd700 "
    "goldenrod:daa520 gray:808080 green:008000 greenyellow:adff2f grey:808080 honeydew:f0fff0 "
    "hotpink:ff69b4 indianred:cd5c5c indigo:4b0082 ivory:fffff0 khaki:f0e68c lavender:e6e6fa "
    "lavenderblush:fff0f5 lawngreen:7cfc00 lemonchiffon:fffacd lightblue:add8e6 lightcoral:f08080 "
    "lightcyan:e0ffff lightgoldenrodyellow:fafad2 lightgray:d3d3d3 lightgreen:90ee90 lightgrey:d3d3d3 "
    "lightpink:ffb6c1 lightsalmon:ffa07a lightseagreen:20b2aa lightskyblue:87cefa "
    "lightslategray:778899 lightslategrey:778899 lightsteelblue:b0c4de lightyellow:ffffe0 lime:00ff00 "
    "limegreen:32cd32 linen:faf0e6 magenta:ff00ff maroon:800000 mediumaquamarine:66cdaa "
    "mediumblue:0000cd mediumorchid:ba55d3 mediumpurple:9370db mediumseagreen:3cb371 "
    "mediumslateblue:7b68ee mediumspringgreen:00fa9a mediumturquoise:48d1cc mediumvioletred:c71585 "
    "midnightblue:191970 mintcream:f5fffa mistyrose:ffe4e1 moccasin:ffe4b5 navajowhite:ffdead "
    "navy:000080 oldlace:fdf5e6 olive:808000 olivedrab:6b8e23 orange:ffa500 orangered:ff4500 "
    "orchid:da70d6 palegoldenrod:eee8aa palegreen:98fb98 paleturquoise:afeeee palevioletred:db7093 "
    "papayawhip:ffefd5 peachpuff:ffdab9 peru:cd853f pink:ffc0cb plum:dda0dd powderblue:b0e0e6 "
    "purple:800080 rebeccapurple:663399 red:ff0000 rosybrown:bc8f8f royalblue:4169e1 "
    "saddlebrown:8b4513 salmon:fa8072 sandybrown:f4a460 seagreen:2e8b57 seashell:fff5ee sienna:a0522d "
    "silver:c0c0c0 skyblue:87ceeb slateblue:6a5acd slategray:708090 slategrey:708090 snow:fffafa "
    "springgreen:00ff7f steelblue:4682b4 tan:d2b48c teal:008080 thistle:d8bfd8 tomato:ff6347 "
    "turquoise:40e0d0 violet:ee82ee wheat:f5deb3 white:ffffff whitesmoke:f5f5f5 yellow:ffff00 "
    "yellowgreen:9acd32"
)
NAMED = dict(p.split(":") for p in _NAMED_SRC.split())


# ---- Report ----

class Report:
    def __init__(self):
        self.findings = []
        self.ran = set()
        self.na = set()
        self.stats = {}
        self.external = {}
        self._seen = set()
        self._count = Counter()
        self._over = Counter()

    def check(self, *rules):
        self.ran.update(rules)

    def not_applicable(self, *rules):
        self.na.update(rules)

    def add(self, rule, message, file=None, line=None, fix=None, sev=None, **extra):
        self.ran.add(rule)
        key = (rule, file, line, message, extra.get("scope"))
        if key in self._seen:
            return
        self._seen.add(key)
        self._count[(rule, file, extra.get("scope"))] += 1
        if self._count[(rule, file, extra.get("scope"))] > PER_FILE_CAP:
            self._over[(rule, file, extra.get("scope"), sev)] += 1
            return
        dsev, dfix = RULES[rule]
        f = {"severity": sev or dsev, "rule": rule, "file": file, "line": line, "message": message,
             "fix": fix or dfix}
        f.update(extra)
        self.findings.append(f)

    def finish(self):
        for (rule, file, scope, sev), n in sorted(self._over.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
            dsev, dfix = RULES[rule]
            extra = {"scope": scope} if scope else {}
            self.findings.append(dict({"severity": sev or dsev, "rule": rule, "file": file, "line": None, "fix": dfix,
                                       "message": "...and %d more of these in this file (not listed)" % n}, **extra))
        self.findings.sort(key=lambda f: (_RANK[f["severity"]], f["rule"], f.get("scope") or "", f["file"] or "",
                                          f["line"] or 0, f["message"]))
        failed = {f["rule"] for f in self.findings if f["severity"] in (E, W)}
        self.failed = sorted(failed)
        self.passed = sorted(self.ran - failed)
        self.n_na = len(self.na - self.ran)
        self.errors = sum(1 for f in self.findings if f["severity"] == E)
        self.warnings = sum(1 for f in self.findings if f["severity"] == W)
        self.infos = sum(1 for f in self.findings if f["severity"] == I)
        return self

    def ok(self, strict=False):
        return self.errors == 0 and (not strict or self.warnings == 0)

    def to_dict(self, label, strict=False):
        return {
            "tool": "check_site", "dir": label, "ok": self.ok(strict), "strict": bool(strict),
            "summary": {"errors": self.errors, "warnings": self.warnings, "info": self.infos,
                        "checks_passed": len(self.passed), "checks_failed": len(self.failed),
                        "checks_not_applicable": self.n_na, "pages": self.stats.get("pages", 0),
                        "files": self.stats.get("files", 0), "bytes": self.stats.get("bytes", 0)},
            "findings": self.findings,
            "external": [{"host": h, "kinds": sorted(v["kinds"]), "count": sum(v["kinds"].values()),
                          "files": sorted(f for f in v["files"] if f)[:5]} for h, v in sorted(self.external.items())],
            "passed": self.passed,
        }


def fmt_size(n):
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.0f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1048576.0)


# ---- Colors and contrast ----

def _clamp(x, lo=0.0, hi=255.0):
    return lo if x < lo else hi if x > hi else x


def _tok(t, scale):
    """'50%' -> 0.5*scale ; '128' -> 128.0"""
    t = t.strip().lower()
    if t == "none":
        return 0.0
    if t.endswith("%"):
        return float(t[:-1]) / 100.0 * scale
    return float(t)


def _hue(t):
    t = t.strip().lower()
    for unit, mul in (("deg", 1.0), ("grad", 0.9), ("rad", 180.0 / math.pi), ("turn", 360.0)):
        if t.endswith(unit):
            return float(t[:-len(unit)]) * mul
    return float(t)


def _hsl_to_rgb(h, s, l):
    h = (h % 360.0) / 360.0
    if s == 0:
        return (l * 255.0,) * 3
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q

    def f(t):
        t %= 1.0
        if t < 1 / 6.0:
            return p + (q - p) * 6 * t
        if t < 0.5:
            return q
        if t < 2 / 3.0:
            return p + (q - p) * (2 / 3.0 - t) * 6
        return p

    return tuple(f(h + d) * 255.0 for d in (1 / 3.0, 0.0, -1 / 3.0))


def _oklab_to_rgb(L, a, b):
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    lin = (4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
           -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
           -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_)
    out = []
    for c in lin:
        c = _clamp(c, 0.0, 1.0)
        out.append((12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055) * 255.0)
    return tuple(out)


def parse_color(value):
    """CSS color string -> (r, g, b, a) with r,g,b in 0..255 and a in 0..1, or None if unreadable."""
    try:
        v = re.sub(r"\s*!important\s*$", "", value.strip().lower())
        if v in NAMED:
            v = "#" + NAMED[v]
        if v == "transparent":
            return (0.0, 0.0, 0.0, 0.0)
        if v.startswith("#"):
            h = v[1:]
            if not re.fullmatch(r"[0-9a-f]{3,8}", h) or len(h) in (5, 7):
                return None
            if len(h) in (3, 4):
                h = "".join(c * 2 for c in h)
            if len(h) == 6:
                h += "ff"
            return (float(int(h[0:2], 16)), float(int(h[2:4], 16)), float(int(h[4:6], 16)), int(h[6:8], 16) / 255.0)
        m = re.fullmatch(r"(rgba?|hsla?|oklch|oklab)\((.*)\)", v)
        if not m:
            return None
        fn, body = m.group(1), m.group(2)
        parts = [p for p in re.split(r"[\s,/]+", body.strip()) if p]
        if len(parts) < 3:
            return None
        alpha = _clamp(_tok(parts[3], 1.0), 0.0, 1.0) if len(parts) > 3 else 1.0
        if fn.startswith("rgb"):
            r, g, b = (_clamp(_tok(p, 255.0)) for p in parts[:3])
        elif fn.startswith("hsl"):
            s = _tok(parts[1], 1.0) if parts[1].endswith("%") else float(parts[1]) / 100.0
            l = _tok(parts[2], 1.0) if parts[2].endswith("%") else float(parts[2]) / 100.0
            r, g, b = (_clamp(c) for c in _hsl_to_rgb(_hue(parts[0]), _clamp(s, 0, 1), _clamp(l, 0, 1)))
        elif fn == "oklch":
            L, C, h = _tok(parts[0], 1.0), _tok(parts[1], 0.4), _hue(parts[2])
            r, g, b = _oklab_to_rgb(L, C * math.cos(math.radians(h)), C * math.sin(math.radians(h)))
        else:
            r, g, b = _oklab_to_rgb(_tok(parts[0], 1.0), _tok(parts[1], 0.4), _tok(parts[2], 0.4))
        return (r, g, b, alpha)
    except (ValueError, OverflowError, ZeroDivisionError):
        return None


def flatten(color, over):
    """Composite an rgba color over an opaque rgb color."""
    a = color[3]
    return tuple(color[i] * a + over[i] * (1 - a) for i in range(3))


def _lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    return 0.2126 * _lin(rgb[0]) + 0.7152 * _lin(rgb[1]) + 0.0722 * _lin(rgb[2])


def contrast_ratio(a, b):
    """WCAG contrast ratio of two colors (hex/rgb strings or rgb tuples)."""
    def rgb(x):
        if isinstance(x, str):
            c = parse_color(x)
            if c is None:
                raise ValueError("unreadable color: %r" % x)
            return c[:3]
        return tuple(x[:3])
    la, lb = luminance(rgb(a)), luminance(rgb(b))
    hi, lo = (la, lb) if la >= lb else (lb, la)
    return (hi + 0.05) / (lo + 0.05)


def ratio_str(r):
    return "%.2f" % (math.floor(r * 100 + 1e-9) / 100.0)


PAIRS = (
    ("contrast.text-bg", "--text", "--bg", 4.5),
    ("contrast.text-surface", "--text", "--surface", 4.5),
    ("contrast.muted-bg", "--muted", "--bg", 4.5),
    ("contrast.muted-surface", "--muted", "--surface", 4.5),
    ("contrast.accent-ink", "--accent-ink", "--accent", 4.5),
    ("contrast.accent-bg", "--accent", "--bg", 3.0),
    ("contrast.danger-bg", "--danger", "--bg", 4.5),
)


def resolve_token(pal, name, depth=0):
    ent = pal.get(name)
    if not ent:
        return None
    v = ent[0].strip()
    m = re.fullmatch(r"var\(\s*(--[\w-]+)\s*(?:,\s*(.+))?\)", v, re.I | re.S)
    if m and depth < 6:
        r = resolve_token(pal, m.group(1), depth + 1)
        return r if r is not None else (m.group(2).strip() if m.group(2) else None)
    return v


def check_contrast(rep, css):
    if not css.light:
        rep.not_applicable(*[p[0] for p in PAIRS])
        return
    rep.check(*[p[0] for p in PAIRS])
    palettes = [("", dict(css.light), None)]
    for label, overlay in sorted(css.dark.items()):
        pal = dict(css.light)
        pal.update(overlay)
        palettes.append(("dark (%s): " % label, pal, overlay))
    for mode, pal, overlay in palettes:
        cache = {}

        def color(name):
            if name not in cache:
                raw = resolve_token(pal, name)
                if raw is None:
                    cache[name] = (None, None)
                else:
                    c = parse_color(raw)
                    cache[name] = (c, raw)
                    if c is None:
                        ent = pal[name]
                        rep.add("contrast.parse", "%s%s value '%s' is not a plain color, so its contrast was not checked"
                                % (mode, name, raw[:40]), ent[1], ent[2])
            return cache[name]

        bgc, _ = color("--bg")
        canvas = flatten(bgc, (255, 255, 255)) if bgc else None
        for rule, fgn, bgn, need in PAIRS:
            if overlay is not None and fgn not in overlay and bgn not in overlay:
                continue
            fgc, fraw = color(fgn)
            bc, braw = color(bgn)
            if fgc is None or bc is None or canvas is None:
                continue
            bg = flatten(bc, canvas)
            fg = flatten(fgc, bg)
            r = contrast_ratio(fg, bg)
            ent = pal[fgn]
            if r < need:
                rep.add(rule, "%s%s %s on %s %s is %s:1 (needs %s:1)" % (mode, fgn, fraw, bgn, braw, ratio_str(r), need),
                        ent[1], ent[2])
            elif rule == "contrast.accent-bg" and r < 4.5:
                rep.add(rule, "%s%s %s on %s %s is %s:1: fine for buttons and borders, too low for accent-colored text (needs 4.5:1)"
                        % (mode, fgn, fraw, bgn, braw, ratio_str(r)), ent[1], ent[2], sev=W)


# ---- CSS scanning ----

_CSS_TOK = re.compile(
    r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|"""
    r"""((?i:url)\(\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^)]*)\s*\))|"""
    r"""([{};])|((?:[^{};"'uU]|[uU](?!(?i:rl)\())+|[uU])""", re.S)
_URL_IN = re.compile(r"""url\(\s*(['"]?)(.*?)\1\s*\)""", re.I | re.S)
_HEX_LIT = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b")
_FUNC_LIT = re.compile(r"(?<![\w-])(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(", re.I)
_COLOR_PROP = re.compile(r"^(?:color|background(?:-color|-image)?|border(?:-[a-z]+)*|outline(?:-color)?|fill|stroke|"
                         r"box-shadow|text-shadow|text-decoration(?:-color)?|caret-color|accent-color|column-rule(?:-color)?|"
                         r"scrollbar-color|text-emphasis-color|-webkit-text-fill-color)$")
_GENERIC_FONTS = frozenset(("serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui", "ui-serif", "ui-sans-serif",
                            "ui-monospace", "ui-rounded", "emoji", "math", "fangsong", "inherit", "initial", "unset", "revert",
                            "revert-layer"))
_FONT_WORDS = frozenset(("normal", "bold", "bolder", "lighter", "italic", "oblique", "small-caps", "xx-small", "x-small",
                         "small", "medium", "large", "x-large", "xx-large", "xxx-large", "smaller", "larger", "condensed",
                         "expanded", "caption", "icon", "menu", "message-box", "small-caption", "status-bar"))


def strip_css_comments(text):
    return re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)


def css_walk(text):
    """Yield ('open', prelude, line), ('close', None, line), ('decl', (name, value), line), ('stmt', text, line)."""
    text = strip_css_comments(text)
    buf = []
    line = 1
    buf_line = 1
    for m in _CSS_TOK.finditer(text):
        tok = m.group(0)
        if m.group(3):
            s = "".join(buf).strip()
            if tok == "{":
                yield ("open", s, buf_line)
            else:
                if s:
                    if s.startswith("@") or ":" not in s:
                        yield ("stmt", s, buf_line)
                    else:
                        name, _, value = s.partition(":")
                        value = re.sub(r"\s*!important\s*$", "", value.strip(), flags=re.I)
                        yield ("decl", (name.strip().lower(), value), buf_line)
                if tok == "}":
                    yield ("close", None, line)
            buf = []
        else:
            if not buf:
                stripped = tok.lstrip()
                if stripped:
                    buf_line = line + tok[:len(tok) - len(stripped)].count("\n")
            buf.append(tok)
        line += tok.count("\n")


_ROOT_START = (":root", "html", "[data-theme", "[data-mode", "[data-color-scheme", ".dark", ".light", ".theme-")


def _is_token_selector(sel):
    for part in sel.lower().split(","):
        last = re.split(r"\s*[>+~]\s*|\s+", part.strip())[-1]
        if last.startswith(_ROOT_START):
            return True
    return False


class CssInfo:
    def __init__(self):
        self.light = {}
        self.dark = {}
        self.has_css = False
        self.focus_visible = False
        self.motion = False
        self.reduced_motion = False
        self.urls = []          # (file, base_dir, line, url, label)
        self.first_file = None
        self.over = Counter()   # (rule, file) -> suppressed findings
        self.counts = Counter()


def _literal_colors(name, value):
    v = _URL_IN.sub(" ", re.sub(r'"[^"]*"|\'[^\']*\'', " ", value))
    out = []
    for m in _HEX_LIT.finditer(v):
        out.append(m.group(0))
    for m in _FUNC_LIT.finditer(v):
        depth, j = 1, m.end()
        while j < len(v) and depth:
            depth += (v[j] == "(") - (v[j] == ")")
            j += 1
        inner = v[m.end():j - 1]
        if "var(" not in inner:
            out.append(v[m.start():j])
    if _COLOR_PROP.match(name) or name.startswith("--"):
        for w in re.split(r"[\s,()/]+", v.lower()):
            if w in NAMED:
                out.append(w)
    return out


def _font_hardcoded(name, value):
    v = value.strip()
    if "var(" in v:
        return None
    if name == "font-family":
        fams = [f.strip().strip("'\"").lower() for f in v.split(",") if f.strip()]
        bad = [f for f in fams if f not in _GENERIC_FONTS]
        return v if bad else None
    if name == "font":
        for t in re.split(r"[\s,/]+", v.lower().replace('"', " ").replace("'", " ")):
            if (not t or re.fullmatch(r"[\d.]+(?:px|rem|em|%|pt|vw|vh|ch|ex)?", t) or t in _FONT_WORDS or t in _GENERIC_FONTS
                    or t.startswith(("calc(", "clamp(", "min(", "max("))):
                continue
            return v
    return None


def analyze_css(text, file, base_dir, css, rep, line0=0, cap=25):
    """Walk one stylesheet, updating `css` and adding token-discipline findings to `rep`."""
    css.has_css = True
    css.first_file = css.first_file or file
    rep.check("tokens.hardcoded-color", "tokens.hardcoded-font")
    stack = []

    def flag(rule, msg, ln):
        css.counts[(rule, file)] += 1
        if css.counts[(rule, file)] <= cap:
            rep.add(rule, msg, file, ln + line0)
        else:
            css.over[(rule, file)] += 1

    for kind, a, ln in css_walk(text):
        if kind == "open":
            stack.append(a)
            low = a.lower()
            if low.startswith(("@keyframes", "@-webkit-keyframes")):
                css.motion = True
            if ":focus-visible" in low:
                css.focus_visible = True
            if "prefers-reduced-motion" in low:
                css.reduced_motion = True
            continue
        if kind == "close":
            if stack:
                stack.pop()
            continue
        if kind == "stmt":
            m = re.match(r"@import\s+(?:url\(\s*)?(['\"]?)([^'\")\s]+)\1", a, re.I)
            if m and re.search(r"[/.:]", m.group(2)):   # bare names such as "tailwindcss" are bundler imports
                css.urls.append((file, base_dir, ln + line0, m.group(2), "stylesheet"))
            continue
        name, value = a
        inner = stack[-1].lower() if stack else ""
        for um in _URL_IN.finditer(value):
            u = um.group(2).strip()
            label = "font" if re.search(r"\.(?:woff2?|ttf|otf|eot)(?:[?#]|$)", u, re.I) else "image"
            css.urls.append((file, base_dir, ln + line0, u, label))
        if inner.startswith("@font-face") or inner.startswith("@property"):
            continue
        if name.startswith("--") and _is_token_selector(inner) and not inner.startswith("@"):
            sel_only = re.sub(r":not\([^)]*\)", "", inner)
            media_dark = any(s.lower().startswith("@media") and "prefers-color-scheme" in s.lower() and "dark" in s.lower()
                             for s in stack)
            if not media_dark and any(s.lower().startswith(("@media", "@supports", "@container")) and "prefers-color-scheme" not in s.lower()
                                      for s in stack):
                continue          # tokens that only apply under some other condition (width, contrast, ...) are not the base palette
            if media_dark or "dark" in sel_only:
                label = "prefers-color-scheme: dark" if media_dark else stack[-1].strip()
                css.dark.setdefault(label, {})[name] = (value, file, ln + line0)
            else:
                css.light[name] = (value, file, ln + line0)
            continue
        if ":focus" in inner and ":focus-within" not in inner and name in ("outline", "box-shadow", "outline-color", "border-color") \
                and value.strip().lower() not in ("none", "0", "0 0 #0000"):
            css.focus_visible = True
        if name in ("animation", "animation-name") and value.strip().lower() not in ("none", ""):
            css.motion = True
        elif name == "scroll-behavior" and value.strip().lower() == "smooth":
            css.motion = True
        elif name.startswith("transition") and "transform" in value.lower():
            css.motion = True
        where = (stack[-1].strip()[:40] if stack else "") or "this rule"
        lits = _literal_colors(name, value)
        if lits:
            flag("tokens.hardcoded-color", "%s: %s is hard-coded in %s" % (name, lits[0], where), ln)
        hf = _font_hardcoded(name, value)
        if hf:
            flag("tokens.hardcoded-font", "%s: %s is hard-coded in %s" % (name, hf[:50], where), ln)


def check_tokens(rep, css):
    if not css.has_css:
        rep.not_applicable("tokens.missing", "tokens.hardcoded-color", "tokens.hardcoded-font")
        return
    rep.check("tokens.missing")
    if not css.light and not css.dark:
        rep.add("tokens.missing", "no design tokens found (:root variables such as --bg and --text), so color contrast was not checked",
                css.first_file, None, sev=W)
        return
    missing = ["--" + n for n in REQUIRED_COLORS + REQUIRED_OTHER if "--" + n not in css.light]
    if missing:
        rep.add("tokens.missing", "missing variable(s): " + ", ".join(missing), css.first_file, None)
    known = set("--" + n for n in REQUIRED_COLORS + REQUIRED_OTHER)
    extra = sorted(k for k in css.light if k not in known)
    if extra:
        rep.add("tokens.extra", "%d extra variable(s): %s%s" % (len(extra), ", ".join(extra[:8]), " ..." if len(extra) > 8 else ""),
                css.first_file, None)


# ---- HTML model ----

_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_HEADS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_TRACKED = frozenset(("a", "button", "label", "title", "script", "style", "svg")) | frozenset(_HEADS)
_ROLES = {"main": "main", "navigation": "nav", "contentinfo": "footer"}
_MEDIA_REFS = {"video": ("src", "poster"), "audio": ("src", "poster"), "track": ("src",), "embed": ("src",), "object": ("data",),
               "iframe": ("src",), "source": ("src",)}


class Page(HTMLParser):
    def __init__(self, text):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.doctype = False
        self.lang = None
        self.charset = False
        self.title = None
        self.meta = {}
        self.link_rels = []
        self.canonical = None
        self.headings, self.imgs, self.anchors, self.buttons = [], [], [], []
        self.fields, self.forms = [], []
        self.label_for = set()
        self.ids = {}
        self.names = set()
        self.refs = []            # (kind, label, url, line)
        self.strings = []         # (line, text)
        self.urls_text = []       # (line, url-ish text)
        self.style_blocks, self.style_attrs, self.scripts = [], [], []
        self.handlers = []        # (line, attribute name) for inline on...= handlers
        self.tags = Counter()
        self.has_script = False
        self.pre_main_anchors = 0
        self.broken = False
        self._main_seen = False
        self._stack, self._collect = [], []
        self._svg = self._tpl = self._label = 0
        try:
            self.feed(text)
            self.close()
        except Exception:  # malformed markup must never crash the check
            self.broken = True
        self._finish()

    def _finish(self):
        while self._stack:
            tag, rec = self._stack.pop()
            self._close(tag, rec)

    # ---- events
    def handle_decl(self, decl):
        if decl.strip().lower().startswith("doctype"):
            self.doctype = "html" in decl.lower()

    def handle_starttag(self, tag, attrs):
        ln = self.getpos()[0]
        if self._tpl:
            if tag == "template":
                self._tpl += 1
            return
        if tag == "template":
            self._tpl = 1
            return
        a = {k: (v if v is not None else "") for k, v in attrs}
        self.tags[tag] += 1
        role = a.get("role", "").strip().lower()
        if role in _ROLES:
            self.tags[_ROLES[role]] += 1
        if a.get("id"):
            self.ids.setdefault(a["id"], []).append(ln)
        if "style" in a:
            self.style_attrs.append((ln, a["style"]))
        for k in a:
            if len(k) > 2 and k.startswith("on") and k[2:].isalpha():
                self.handlers.append((ln, k))
        for key in ("aria-label", "placeholder", "alt", "title"):
            if a.get(key, "").strip():
                self.strings.append((ln, a[key]))
        if tag == "main":
            self._main_seen = True
        for attr in _MEDIA_REFS.get(tag, ()):
            if a.get(attr):
                self.refs.append(("res", "iframe" if tag == "iframe" else "media", a[attr], ln))
        handler = getattr(self, "_t_" + tag, None)
        rec = handler(a, ln) if handler else None
        if tag in _HEADS:
            rec = {"level": _HEADS[tag], "text": [], "line": ln, "attrs": a, "alt": []}
            self.headings.append(rec)
            self._collect.append(rec)
        if tag in _TRACKED:
            self._stack.append((tag, rec if rec is not None else {}))

    def handle_endtag(self, tag):
        if self._tpl:
            if tag == "template":
                self._tpl -= 1
            return
        if tag not in _TRACKED:
            return
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                closing = self._stack[i:]
                del self._stack[i:]
                for t, rec in reversed(closing):
                    self._close(t, rec)
                return

    def handle_data(self, data):
        if self._tpl:
            return
        ln = self.getpos()[0]
        top, rec = self._stack[-1] if self._stack else ("", None)
        if top in ("script", "style", "title") and rec is not None and "chunks" in rec:
            rec["chunks"].append(data)
            return
        if data.strip():
            self.strings.append((ln, data))
        for c in self._collect:
            c["text"].append(data)

    def _close(self, tag, rec):
        if tag == "style":
            self.style_blocks.append((rec.get("line", 1), "".join(rec.get("chunks", []))))
        elif tag == "script":
            self.scripts.append((rec.get("line", 1), "".join(rec.get("chunks", [])), rec.get("type", ""), rec.get("src", "")))
        elif tag == "title":
            if "chunks" in rec and self.title is None:
                self.title = " ".join("".join(rec["chunks"]).split())
                self.strings.append((rec.get("line", 1), self.title))
        elif tag == "svg":
            self._svg -= 1
        elif tag == "label":
            self._label -= 1
        elif tag in ("a", "button") or tag in _HEADS:
            self._collect = [c for c in self._collect if c is not rec]

    # ---- per-tag handlers (return a record for tracked tags)
    def _t_html(self, a, ln):
        self.lang = a.get("lang")

    def _t_svg(self, a, ln):
        self._svg += 1

    def _t_title(self, a, ln):
        if not self._svg:
            return {"chunks": [], "line": ln}

    def _t_style(self, a, ln):
        return {"chunks": [], "line": ln}

    def _t_script(self, a, ln):
        self.has_script = True
        if a.get("src"):
            self.refs.append(("res", "script", a["src"], ln))
        return {"chunks": [], "line": ln, "type": a.get("type", "").lower(), "src": a.get("src", "")}

    def _t_label(self, a, ln):
        self._label += 1
        if a.get("for"):
            self.label_for.add(a["for"])
        return {"line": ln}

    def _t_a(self, a, ln):
        rec = {"attrs": a, "href": a.get("href"), "line": ln, "text": [], "alt": []}
        self.anchors.append(rec)
        if not self._main_seen:
            self.pre_main_anchors += 1
        if a.get("name"):
            self.names.add(a["name"])
        if a.get("href") is not None:
            self.refs.append(("nav", "link", a["href"], ln))
            self.urls_text.append((ln, a["href"]))
        self._collect.append(rec)
        return rec

    def _t_button(self, a, ln):
        rec = {"attrs": a, "line": ln, "text": [], "alt": []}
        self.buttons.append(rec)
        self._collect.append(rec)
        return rec

    def _t_img(self, a, ln):
        self.imgs.append({"attrs": a, "line": ln})
        for c in self._collect:
            c["alt"].append(a.get("alt", ""))
        if a.get("src"):
            self.refs.append(("res", "image", a["src"], ln))
            self.urls_text.append((ln, a["src"]))
        if a.get("srcset") and "data:" not in a["srcset"]:
            for u in re.findall(r"([^\s,]+)(?:\s+[\d.]+[wx])?\s*(?:,|$)", a["srcset"]):
                self.refs.append(("res", "image", u, ln))

    def _t_source(self, a, ln):
        if a.get("srcset") and "data:" not in a["srcset"]:
            for u in re.findall(r"([^\s,]+)(?:\s+[\d.]+[wx])?\s*(?:,|$)", a["srcset"]):
                self.refs.append(("res", "image", u, ln))

    def _t_link(self, a, ln):
        rels = a.get("rel", "").lower().split()
        href = a.get("href")
        self.link_rels.append((rels, href, ln))
        if href is None:
            return
        if "canonical" in rels and self.canonical is None:
            self.canonical = href
            self.urls_text.append((ln, href))
        as_ = a.get("as", "").lower()
        if "stylesheet" in rels:
            self.refs.append(("res", "stylesheet", href, ln))
        elif "icon" in rels or "apple-touch-icon" in rels or "mask-icon" in rels:
            self.refs.append(("res", "icon", href, ln))
        elif "preload" in rels or "modulepreload" in rels or "prefetch" in rels:
            self.refs.append(("res", "font" if as_ == "font" else (as_ or "preload"), href, ln))
        elif "manifest" in rels:
            self.refs.append(("res", "manifest", href, ln))
        elif "preconnect" in rels or "dns-prefetch" in rels:
            self.refs.append(("pre", "preconnect", href, ln))

    def _t_meta(self, a, ln):
        if "charset" in a:
            self.charset = True
        if a.get("http-equiv", "").lower() == "content-type" and "charset" in a.get("content", "").lower():
            self.charset = True
        key = (a.get("name") or a.get("property") or "").strip().lower()
        content = a.get("content", "")
        if key:
            self.meta.setdefault(key, content)
            if key in ("og:image", "og:image:url", "twitter:image"):
                self.refs.append(("meta", "image", content, ln))
            if key in ("description", "og:title", "og:description", "twitter:title", "twitter:description") and content.strip():
                self.strings.append((ln, content))
        if "://" in content:
            self.urls_text.append((ln, content))

    def _t_form(self, a, ln):
        self.forms.append({"action": a.get("action"), "attrs": a, "line": ln})
        if a.get("action"):
            self.refs.append(("form", "form", a["action"], ln))
            self.urls_text.append((ln, a["action"]))

    def _t_input(self, a, ln):
        t = a.get("type", "text").strip().lower()
        if t in ("submit", "reset", "button"):
            default = "Submit" if t == "submit" else "Reset" if t == "reset" else ""
            self.buttons.append({"attrs": a, "line": ln, "text": [a.get("value", "") or default], "alt": []})
        elif t == "image":
            if a.get("src"):
                self.refs.append(("res", "image", a["src"], ln))
            self.buttons.append({"attrs": a, "line": ln, "text": [], "alt": [a.get("alt", "")]})
        elif t != "hidden":
            self.fields.append({"type": t, "attrs": a, "line": ln, "wrapped": self._label > 0})

    def _t_select(self, a, ln):
        self.fields.append({"type": "select", "attrs": a, "line": ln, "wrapped": self._label > 0})

    _t_textarea = _t_select


def accessible_name(rec):
    a = rec["attrs"]
    if a.get("aria-labelledby", "").strip():
        return "(labelled)"
    n = a.get("aria-label", "").strip()
    if n:
        return n
    n = " ".join("".join(rec["text"]).split())
    if n:
        return n
    for alt in rec["alt"]:
        if alt.strip():
            return alt.strip()
    return a.get("title", "").strip()


# ---- Site index ----

_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]")


def _join(*parts):
    return "/".join(p for p in parts if p)


class Site:
    def __init__(self, root, virtual=None):
        self.root = root
        self.files = {}
        self.dirs = set()
        self.junk = []
        self.truncated = False
        self.virtual = virtual
        self.clean_urls = False
        self.route_sources = set()
        self.vercel_error = None
        self.csp = None
        if virtual is not None:
            for k, v in virtual.items():
                self.files[k] = len(v.encode("utf-8"))
            return
        for cur, dirnames, filenames in os.walk(root):
            dirnames.sort()
            filenames.sort()
            rel_cur = os.path.relpath(cur, root).replace(os.sep, "/")
            rel_cur = "" if rel_cur == "." else rel_cur
            keep = []
            for d in dirnames:
                if d in SKIP_DIRS:
                    if d in ("node_modules", ".git", "__pycache__"):
                        self.junk.append((_join(rel_cur, d) + "/", "folder"))
                    continue
                keep.append(d)
                self.dirs.add(_join(rel_cur, d))
            dirnames[:] = keep
            for f in filenames:
                try:
                    size = os.path.getsize(os.path.join(cur, f))
                except OSError:
                    continue
                self.files[_join(rel_cur, f)] = size
                if len(self.files) >= MAX_FILES:
                    self.truncated = True
                    return

    def path(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    def read(self, rel):
        if self.virtual is not None:
            return self.virtual.get(rel, "")
        try:
            with open(self.path(rel), "rb") as fh:
                data = fh.read(MAX_TEXT)
        except OSError:
            return ""
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
        return data.decode("utf-8", "replace").replace("\r\n", "\n")

    def load_vercel(self):
        if "vercel.json" not in self.files:
            return
        try:
            cfg = json.loads(self.read("vercel.json") or "{}")
        except ValueError as ex:
            self.vercel_error = str(ex)[:80]
            return
        if not isinstance(cfg, dict):
            return
        self.clean_urls = bool(cfg.get("cleanUrls"))
        for entry in cfg.get("headers") or []:
            if not isinstance(entry, dict) or entry.get("source") not in ("/(.*)", "/:path*", "/*", "/"):
                continue
            for h in entry.get("headers") or []:
                if isinstance(h, dict) and str(h.get("key", "")).lower() == "content-security-policy" and isinstance(h.get("value"), str):
                    self.csp = {}
                    for part in h["value"].split(";"):
                        toks = part.split()
                        if toks:
                            self.csp.setdefault(toks[0].lower(), toks[1:])
                    break
            if self.csp is not None:
                break
        for key in ("redirects", "rewrites"):
            for item in cfg.get(key) or []:
                src = item.get("source") if isinstance(item, dict) else None
                if isinstance(src, str) and not re.search(r"[:*(\[]", src):
                    self.route_sources.add("/" + src.strip("/"))

    def resolve(self, base_dir, raw):
        """Classify a URL. Returns (status, detail, fragment)."""
        u = raw.strip()
        if not u:
            return ("empty", None, "")
        if u.startswith("#"):
            return ("anchor", None, unquote(u[1:]))
        if re.match(r"^[a-zA-Z]:[\\/]", u):
            return ("localfile", u, "")
        m = _SCHEME.match(u)
        if m:
            return ("external", u, "") if m.group(1).lower() in ("http", "https") else ("skip", u, "")
        if u.startswith("//"):
            return ("external", "https:" + u, "")
        try:
            parts = urlsplit(u)
        except ValueError:
            return ("skip", u, "")
        path, frag = unquote(parts.path), unquote(parts.fragment)
        if path == "":
            return ("anchor", None, frag)
        keep_slash = path.endswith("/")
        p = posixpath.normpath(path if path.startswith("/") else posixpath.join(base_dir or ".", path))
        if p == ".." or p.startswith("../"):
            return ("escape", None, frag)
        p = p.lstrip("/")
        p = "" if p == "." else p
        if p and ("/" + p) in self.route_sources:
            return ("ok", p, frag)
        cands = []
        if p:
            cands.append(p)
        if keep_slash or p == "" or p in self.dirs:
            cands.append(_join(p, "index.html"))
        if p and not posixpath.splitext(p)[1] and self.clean_urls:
            cands.append(p + ".html")
        for c in cands:
            if c in self.files:
                return ("ok", c, frag)
        low = {k.lower(): k for k in self.files}
        for c in cands:
            if c.lower() in low:
                return ("case", low[c.lower()], frag)
        return ("missing", p or "/", frag)


# ---- Per-page and per-site checks ----

GENERIC_LINK_TEXT = frozenset(("click here", "click", "here", "tap here", "this", "this link", "link", "more", "read more",
                               "learn more", "see more", "go", "click this", "right here"))
GENERIC_ALT = frozenset(("image", "img", "photo", "picture", "graphic", "icon", "logo", "banner", "placeholder", "untitled",
                         "thumbnail"))
FILE_ALT = re.compile(r"\.(?:png|jpe?g|gif|webp|avif|svg|bmp)\b|^(?:img|image|dsc|screenshot)[_\- ]?\d+", re.I)
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
LOREM = re.compile(r"\blorem ipsum\b|\bdolor sit amet\b|\bconsectetur adipiscing\b", re.I)
PLACEHOLDERS = (
    (re.compile(r"\b(?:TODO|TBD|FIXME)\b"), "TODO/TBD marker"),
    (re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S), "unfilled template tag"),
    (re.compile("\\[(?:\\.\\.\\.|\u2026)\\]"), "[...] marker"),
    (re.compile(r"\[(?:your|insert|add|enter|company|business|brand|name|text|title|headline|description|link|image|todo|tbd)\b[^\]]{0,40}\]",
                re.I), "bracket placeholder"),
    (re.compile(r"\b(?:your (?:company|business|brand|website|product|name)(?: name)? here|company name here|your company name|"
                r"insert (?:your )?(?:text|headline|title|description|content|logo)(?: here)?|"
                r"(?:text|headline|title|description|content|copy) goes here|placeholder text|sample text)\b", re.I),
     "placeholder wording"),
)
EXAMPLE_DOMAIN = re.compile(r"(?<![\w-])(?:[\w-]+\.)*(?:example\.(?:com|org|net)|yourdomain\.\w+|your-domain\.\w+|"
                            r"yourwebsite\.\w+|yoursite\.\w+|domain\.com)(?![\w-])", re.I)
TRACKERS = ("google-analytics.com", "googletagmanager.com", "analytics.google.com", "doubleclick.net", "googlesyndication.com",
            "facebook.net", "hotjar.com", "clarity.ms", "segment.com", "segment.io", "mixpanel.com", "mxpnl.com", "plausible.io",
            "usefathom.com", "posthog.com", "amplitude.com", "heap.io", "heapanalytics.com", "fullstory.com", "hs-scripts.com",
            "hs-analytics.net", "snap.licdn.com", "ads.linkedin.com", "analytics.tiktok.com", "ads-twitter.com", "matomo",
            "umami.is", "cloudflareinsights.com", "vercel-insights.com", "vercel-scripts.com", "adsbygoogle", "pagead2")
JUNK_NAMES = frozenset((".ds_store", "thumbs.db", "desktop.ini"))
JUNK_EXT = frozenset(".psd .ai .sketch .fig .xcf .zip .tar .gz .tgz .rar .7z .bak .orig .swp .log .sql .sqlite .sqlite3 .db .map".split())


def _loc_note(cx, label, host):
    e = cx.rep.external.setdefault(host, {"kinds": Counter(), "files": set()})
    e["kinds"][label] += 1
    e["files"].add(cx.cur_file)


def _host(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


class Ctx:
    def __init__(self, site, rep):
        self.site, self.rep = site, rep
        self.pages = {}
        self.css = CssInfo()
        self.cur_file = None
        self.multi_page = False
        self.self_hosts = set()


def process_ref(cx, file, base_dir, kind, label, url, ln, page=None):
    """Existence, hosts and safety of one referenced URL (from HTML or CSS)."""
    R, site = cx.rep, cx.site
    R.check("links.broken", "links.anchor", "links.localhost", "net.insecure")
    st, detail, frag = site.resolve(base_dir, url)
    cx.cur_file = file
    if st in ("empty", "skip"):
        return
    if st == "localfile":
        R.add("links.localhost", "'%s' points to a file on this computer" % url[:60], file, ln)
        return
    if st == "external":
        host = _host(detail)
        if host in _LOCAL_HOSTS or host.endswith(".local"):
            R.add("links.localhost", "'%s' points to this computer, so visitors cannot open it" % url[:60], file, ln)
            return
        if kind in ("nav", "meta"):
            return
        _loc_note(cx, label, host)
        if detail.lower().startswith("http://") and kind in ("res", "pre", "form"):
            R.add("net.insecure", "%s '%s' loads over plain http" % (label, url[:70]), file, ln)
        if label == "script":
            if any(t in host for t in TRACKERS):
                R.add("net.tracker", "tracking or analytics script from %s" % host, file, ln)
            else:
                R.add("net.script", "third-party script from %s" % host, file, ln)
        elif any(t in host for t in TRACKERS) and kind in ("res", "form"):
            R.add("net.tracker", "%s from tracking host %s" % (label, host), file, ln)
        return
    if site.virtual is not None and st in ("escape", "missing", "case", "ok"):
        return
    if st == "escape":
        R.add("links.broken", "'%s' climbs out of the site folder" % url[:70], file, ln)
    elif st == "missing":
        R.add("links.broken", "'%s' does not exist in the site folder (looked for %s)" % (url[:70], detail), file, ln)
    elif st == "case":
        R.add("links.broken", "'%s' differs in letter case from the real file '%s'; this works on a Mac but breaks after deploy"
              % (url[:70], detail), file, ln)
    elif st == "anchor" and page is not None:
        if frag and frag.lower() != "top" and frag not in page.ids and frag not in page.names:
            R.add("links.anchor", "'#%s' does not match any id on this page" % frag[:50], file, ln)
    elif st == "ok" and frag and page is not None and kind == "nav":
        target = cx.pages.get(detail)
        if target is not None and frag.lower() != "top" and frag not in target.ids and frag not in target.names:
            R.add("links.anchor", "'%s' points to #%s, which does not exist on %s" % (url[:60], frag[:40], detail), file, ln)


def check_page(cx, rel, page):
    R = cx.rep
    base = posixpath.dirname(rel)
    is404 = posixpath.basename(rel).lower() == "404.html"
    R.check("html.doctype", "html.charset", "html.lang", "html.viewport", "html.title", "html.h1", "html.heading-order",
            "html.img-alt", "html.img-alt-quality", "html.link-empty", "html.link-name", "html.link-text", "html.form-label",
            "html.form-label-placeholder", "html.form-action", "html.dup-id", "html.landmarks", "html.parse",
            "content.lorem", "content.placeholder", "content.example-domain", "html.zoom-blocked", "seo.noindex")
    if page.broken:
        R.add("html.parse", "the HTML could not be read completely; results for this page may be partial", rel, None)
    if not page.doctype:
        R.add("html.doctype", "no <!doctype html>; browsers fall back to quirks mode", rel, 1)
    if not page.charset:
        R.add("html.charset", "no <meta charset>", rel, 1)
    if not page.lang:
        R.add("html.lang", "the <html> tag has no lang attribute", rel, 1)
    elif not LANG_RE.match(page.lang.strip()):
        R.add("html.lang", "lang='%s' is not a valid language code" % page.lang[:20], rel, 1)
    vp = page.meta.get("viewport")
    if vp is None:
        R.add("html.viewport", "no viewport meta tag; phones will show a shrunken desktop page", rel, 1)
    else:
        v = vp.replace(" ", "").lower()
        if "width=device-width" not in v:
            R.add("html.viewport", "viewport lacks width=device-width", rel, 1)
        mx = re.search(r"maximum-scale=([\d.]+)", v)
        if "user-scalable=no" in v or "user-scalable=0" in v or (mx and float(mx.group(1)) < 2):
            R.add("html.zoom-blocked", "the viewport tag blocks pinch zoom", rel, 1)
    t = page.title
    if not t:
        R.add("html.title", "missing or empty <title>", rel, 1)
    elif len(t) < 10 or len(t) > 60:
        R.add("html.title", "title is %d characters (aim for 10 to 60): '%s'" % (len(t), t[:50]), rel, 1, sev=W)
    # headings
    ones = [h for h in page.headings if h["level"] == 1]
    if not ones:
        R.add("html.h1", "no <h1> on this page", rel, 1)
    elif len(ones) > 1:
        R.add("html.h1", "%d <h1> elements; use one" % len(ones), rel, ones[1]["line"], sev=W)
    prev = 0
    for h in page.headings:
        if prev and h["level"] - prev > 1:
            R.add("html.heading-order", "h%d jumps to h%d" % (prev, h["level"]), rel, h["line"])
        prev = h["level"]
    # images
    for im in page.imgs:
        a = im["attrs"]
        if "alt" not in a:
            if a.get("role", "").lower() in ("presentation", "none") or a.get("aria-hidden", "").lower() == "true":
                continue
            R.add("html.img-alt", "image '%s' has no alt attribute" % a.get("src", "")[:60], rel, im["line"])
        else:
            alt = a["alt"].strip().lower()
            if alt and (FILE_ALT.search(alt) or alt in GENERIC_ALT):
                R.add("html.img-alt-quality", "alt text '%s' says nothing useful" % a["alt"][:40], rel, im["line"])
    # links and buttons
    for an in page.anchors:
        href = an["href"]
        if href is None:
            if an["attrs"].get("id") or an["attrs"].get("name"):
                continue
            R.add("html.link-empty", "<a> without href (a link that goes nowhere)", rel, an["line"])
        else:
            h = href.strip().lower()
            if h in ("", "#") or h.startswith("javascript:") or re.fullmatch(r"(?:mailto|tel):\s*", h):
                R.add("html.link-empty", "link with empty destination '%s'" % href[:30], rel, an["line"])
        name = accessible_name(an)
        if not name:
            R.add("html.link-name", "link to '%s' has no text or aria-label" % (href or "")[:50], rel, an["line"])
        elif name.lower().strip(" .!:") in GENERIC_LINK_TEXT:
            R.add("html.link-text", "link text '%s' does not say where it goes" % name[:30], rel, an["line"])
    for b in page.buttons:
        name = accessible_name(b)
        if not name:
            R.add("html.link-name", "button has no text or aria-label", rel, b["line"])
        elif name.lower().strip(" .!:") in GENERIC_LINK_TEXT:
            R.add("html.link-text", "button text '%s' does not say what it does" % name[:30], rel, b["line"])
    # forms
    for f in page.fields:
        a = f["attrs"]
        fid = a.get("id", "")
        if f["wrapped"] or (fid and fid in page.label_for) or a.get("aria-label", "").strip() \
                or a.get("aria-labelledby", "").strip() or a.get("title", "").strip():
            continue
        if a.get("placeholder", "").strip():
            R.add("html.form-label-placeholder", "field '%s' has only a placeholder" % (a.get("name") or a.get("placeholder"))[:30],
                  rel, f["line"])
        else:
            R.add("html.form-label", "%s field '%s' has no label" % (f["type"], (a.get("name") or fid or "?")[:30]), rel, f["line"])
    for fm in page.forms:
        act = (fm["action"] or "").strip()
        if act in ("", "#") and "onsubmit" not in fm["attrs"] and not page.has_script:
            R.add("html.form-action", "form has no action, so submitting it sends nothing anywhere", rel, fm["line"])
    dups = sorted(i for i, lns in page.ids.items() if len(lns) > 1)
    if dups:
        R.add("html.dup-id", "duplicate id(s): %s" % ", ".join(dups[:6]), rel, page.ids[dups[0]][1])
    # landmarks and skip link
    m = page.tags["main"]
    if m == 0:
        R.add("html.landmarks", "no <main> element", rel, 1)
    elif m > 1:
        R.add("html.landmarks", "%d <main> elements; use one" % m, rel, 1)
    if not is404:
        if page.tags["nav"] == 0 and cx.multi_page:
            R.add("html.landmarks", "no <nav> element on a multi-page site", rel, 1)
        if page.tags["footer"] == 0:
            R.add("html.landmarks", "no <footer> element", rel, 1)
    if page.tags["nav"] > 0 or page.pre_main_anchors >= 3:
        R.check("a11y.skip-link")
        first = page.anchors[:3]
        ok = any((a["href"] or "").startswith("#") and len(a["href"]) > 1 and "skip" in accessible_name(a).lower()
                 and a["href"][1:] in page.ids for a in first)
        if not ok:
            R.add("a11y.skip-link", "no skip-to-content link among the first links", rel, 1)
    else:
        R.not_applicable("a11y.skip-link")
    # share and search tags
    robots = page.meta.get("robots", "").lower()
    if "noindex" in robots and not is404:
        R.add("seo.noindex", "robots meta tag contains noindex; search engines will ignore this page", rel, 1)
    if not is404:
        R.check("html.description", "html.canonical", "html.og", "html.og-image-abs", "html.twitter", "html.favicon")
        d = page.meta.get("description")
        if d is None or not d.strip():
            R.add("html.description", "no meta description", rel, 1)
        elif len(d.strip()) < 50 or len(d.strip()) > 160:
            R.add("html.description", "description is %d characters (aim for 50 to 160)" % len(d.strip()), rel, 1)
        if not page.canonical:
            R.add("html.canonical", "no canonical link", rel, 1)
        elif not re.match(r"^https?://", page.canonical.strip(), re.I):
            R.add("html.canonical", "canonical '%s' is not an absolute URL" % page.canonical[:50], rel, 1)
        miss = [k for k in ("og:title", "og:description", "og:image") if not page.meta.get(k, "").strip()]
        if miss:
            R.add("html.og", "missing Open Graph tag(s): %s" % ", ".join(miss), rel, 1)
        img = page.meta.get("og:image", "").strip()
        if img and not re.match(r"^https?://", img, re.I):
            R.add("html.og-image-abs", "og:image '%s' is not an absolute URL" % img[:50], rel, 1)
        tc = page.meta.get("twitter:card", "").strip()
        if not tc:
            R.add("html.twitter", "no twitter:card meta tag", rel, 1)
        has_icon = any("icon" in rels for rels, href, _ in page.link_rels if href)
        if not has_icon:
            if "favicon.ico" in cx.site.files:
                pass
            elif "favicon.svg" in cx.site.files:
                R.add("html.favicon", "favicon.svg exists but no <link rel=icon> points to it", rel, 1)
            else:
                R.add("html.favicon", "no favicon", rel, 1)
    # placeholders
    for ln, s in page.strings:
        if LOREM.search(s):
            R.add("content.lorem", "lorem ipsum text: '%s'" % " ".join(s.split())[:40], rel, ln)
            continue
        for rx, what in PLACEHOLDERS:
            mm = rx.search(s)
            if mm:
                R.add("content.placeholder", "%s: '%s'" % (what, " ".join(mm.group(0).split())[:40]), rel, ln)
    for ln, s in list(page.urls_text) + [(l, x) for l, x in page.strings]:
        mm = EXAMPLE_DOMAIN.search(s)
        if mm:
            R.add("content.example-domain", "placeholder address '%s'" % mm.group(0)[:50], rel, ln)
    # references
    for kind, label, url, ln in page.refs:
        process_ref(cx, rel, base, kind, label, url, ln, page)
    # script urls (possible requests)
    for ln, txt, typ, src in page.scripts:
        scan_script_urls(cx, rel, txt, typ)
    if cx.site.csp is not None:
        check_csp(cx, rel, page)


def scan_script_urls(cx, file, text, typ=""):
    if "ld+json" in typ or not text:
        return
    cx.cur_file = file
    for m in set(re.findall(r"https?://[A-Za-z0-9.\-]+(?::\d+)?", text)):
        host = _host(m)
        if not host or host in ("www.w3.org", "schema.org", "www.schema.org", "example.com") or host in _LOCAL_HOSTS:
            continue
        _loc_note(cx, "script-url", host)
        if any(t in host for t in TRACKERS):
            cx.rep.add("net.tracker", "script mentions tracking host %s" % host, file, None)


CSP_FOR = {
    "script": ("script-src-elem", "script-src", "default-src"), "stylesheet": ("style-src-elem", "style-src", "default-src"),
    "font": ("font-src", "default-src"), "image": ("img-src", "default-src"), "icon": ("img-src", "default-src"),
    "media": ("media-src", "default-src"), "iframe": ("frame-src", "child-src", "default-src"), "form": ("form-action",),
    "manifest": ("manifest-src", "default-src"),
}
CSP_ALIAS = {"style": "stylesheet"}


def csp_pick(csp, names):
    """The first directive of the fallback chain the policy actually sets: (name, tokens), or (last name, None) if unrestricted."""
    for n in names:
        if n in csp:
            return n, csp[n]
    return names[-1], None


def csp_tokens(csp, names):
    return csp_pick(csp, names)[1]


def csp_allows_url(tokens, url, self_hosts=()):
    """Would a Content-Security-Policy source list (None = no restriction) let this URL load?"""
    if tokens is None:
        return True
    low = [t.lower() for t in tokens]
    u = url.strip()
    if not u or u.startswith("#"):
        return True
    if u.startswith("//"):
        u = "https:" + u
    m = _SCHEME.match(u)
    if not m:
        return "'self'" in low or "*" in low
    scheme = m.group(1).lower()
    if scheme in ("data", "blob", "filesystem"):
        return scheme + ":" in low
    if scheme not in ("http", "https"):
        return True
    host = _host(u)
    if "'self'" in low and host in self_hosts:
        return True
    for t in low:
        if t == "*" or (t == "https:" and scheme == "https") or (t == "http:"):
            return True
        rest = t
        if "://" in t:
            ts, rest = t.split("://", 1)
            if ts != scheme and not (ts == "http" and scheme == "https"):
                continue
        rest = rest.split("/")[0].split(":")[0]
        if rest and not rest.startswith("'") and (rest == host or (rest.startswith("*.") and host.endswith(rest[1:]))):
            return True
    return False


def csp_allows_inline(tokens, text, attr=False):
    """True if an inline script or style is allowed, False if blocked, None if a nonce makes it unknowable."""
    if tokens is None:
        return True
    low = [t.lower() for t in tokens]
    if "'none'" in low:
        return False
    marks = [t for t in low if t.startswith(("'sha", "'nonce-"))]
    if "'unsafe-inline'" in low and not marks:
        return True
    if attr:
        return None if "'unsafe-hashes'" in low else False
    if any(t.startswith("'nonce-") for t in low):
        return None
    for variant in (text, text.replace("\n", "\r\n")):
        for algo in ("sha256", "sha384", "sha512"):
            digest = base64.b64encode(hashlib.new(algo, variant.encode("utf-8")).digest()).decode("ascii")
            if "'%s-%s'" % (algo, digest) in tokens:
                return True
    return False


def csp_check_url(cx, file, label, url, ln):
    names = CSP_FOR.get(CSP_ALIAS.get(label, label))
    if not names or cx.site.csp is None:
        return
    directive, tokens = csp_pick(cx.site.csp, names)
    if not csp_allows_url(tokens, url, cx.self_hosts):
        cx.rep.add("net.csp", "%s '%s' is blocked by the Content-Security-Policy (%s)" % (label, url[:70], directive), file, ln)


def check_csp(cx, rel, page):
    R, csp = cx.rep, cx.site.csp
    R.check("net.csp")
    for ln, txt, typ, src in page.scripts:
        if src or not txt.strip() or (typ and typ not in ("text/javascript", "module", "application/javascript")):
            continue
        if csp_allows_inline(csp_tokens(csp, ("script-src-elem", "script-src", "default-src")), txt) is False:
            R.add("net.csp", "an inline <script> is blocked by the Content-Security-Policy (its hash is not in script-src), so it will not run once deployed",
                  rel, ln)
    if page.handlers and csp_allows_inline(csp_tokens(csp, ("script-src-attr", "script-src", "default-src")), "", attr=True) is False:
        R.add("net.csp", "inline event handlers such as %s= are blocked by the Content-Security-Policy" % page.handlers[0][1], rel, page.handlers[0][0])
    sty = csp_tokens(csp, ("style-src-elem", "style-src", "default-src"))
    for ln, txt in page.style_blocks:
        if csp_allows_inline(sty, txt) is False:
            R.add("net.csp", "an inline <style> block is blocked by the Content-Security-Policy (style-src)", rel, ln)
    if page.style_attrs and csp_allows_inline(csp_tokens(csp, ("style-src-attr", "style-src", "default-src")), "", attr=True) is False:
        R.add("net.csp", "style=\"...\" attributes are blocked by the Content-Security-Policy (style-src), so this styling is lost once deployed",
              rel, page.style_attrs[0][0])
    for kind, label, url, ln in page.refs:
        if kind in ("res", "form"):
            csp_check_url(cx, rel, label, url, ln)


def check_css_flags(cx):
    R, css = cx.rep, cx.css
    if not css.has_css:
        R.not_applicable("css.focus-visible", "css.reduced-motion")
        return
    R.check("css.focus-visible")
    if not css.focus_visible:
        R.add("css.focus-visible", "no :focus-visible rule; keyboard users cannot see where they are", css.first_file, None)
    if css.motion:
        R.check("css.reduced-motion")
        if not css.reduced_motion:
            R.add("css.reduced-motion", "animations or smooth scrolling exist but nothing handles prefers-reduced-motion",
                  css.first_file, None)
    else:
        R.not_applicable("css.reduced-motion")


def check_site_level(cx):
    R, site = cx.rep, cx.site
    R.check("files.index", "files.missing", "files.junk", "files.vercel-json", "seo.robots", "seo.sitemap", "html.title-dup",
            "weight.total", "weight.image", "weight.file")
    if "index.html" not in site.files:
        R.add("files.index", "no index.html in the folder", None, None)
    for name, sev in (("styles.css", W), ("404.html", W), ("robots.txt", W), ("sitemap.xml", W), ("vercel.json", I)):
        if name not in site.files:
            R.add("files.missing", "%s is missing" % name, name, None, sev=sev)
    if site.vercel_error:
        R.add("files.vercel-json", "vercel.json is not valid JSON (%s)" % site.vercel_error, "vercel.json", None)
    for path, what in site.junk:
        R.add("files.junk", "%s %s inside the deploy folder" % (what, path), path, None)
    for rel in sorted(site.files):
        base = posixpath.basename(rel).lower()
        ext = posixpath.splitext(base)[1]
        if base in JUNK_NAMES or ext in JUNK_EXT:
            R.add("files.junk", "'%s' should not be public" % rel, rel, None)
    # titles
    seen = {}
    for rel, page in sorted(cx.pages.items()):
        if posixpath.basename(rel).lower() != "404.html" and page.title:
            seen.setdefault(page.title.strip().lower(), []).append(rel)
    for t, rels in sorted(seen.items()):
        if len(rels) > 1:
            R.add("html.title-dup", "pages share the title '%s': %s" % (t[:40], ", ".join(rels[:4])), rels[1], 1)
    # robots and sitemap
    if "robots.txt" in site.files:
        txt = site.read("robots.txt")
        star = False
        for i, raw in enumerate(txt.splitlines(), 1):
            k, _, v = raw.split("#", 1)[0].partition(":")
            k, v = k.strip().lower(), v.strip()
            if k == "user-agent":
                star = v == "*"
            elif k == "disallow" and star and v == "/":
                R.add("seo.robots", "robots.txt tells every crawler to stay away (Disallow: /)", "robots.txt", i)
            if EXAMPLE_DOMAIN.search(raw):
                R.add("content.example-domain", "placeholder address in robots.txt", "robots.txt", i)
    if "sitemap.xml" in site.files:
        txt = site.read("sitemap.xml")
        locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", txt)
        if not locs:
            R.add("seo.sitemap", "sitemap.xml lists no <loc> URLs", "sitemap.xml", None)
        for i, line in enumerate(txt.splitlines(), 1):
            if EXAMPLE_DOMAIN.search(line):
                R.add("content.example-domain", "placeholder address in sitemap.xml", "sitemap.xml", i)
        for u in locs:
            try:
                path = urlsplit(u).path or "/"
            except ValueError:
                continue
            st = site.resolve("", path)[0]
            if st in ("missing", "case"):
                R.add("seo.sitemap", "sitemap lists %s, which does not exist in the site folder" % path[:60], "sitemap.xml", None)
    # weight
    total = sum(site.files.values())
    R.stats.update({"files": len(site.files), "bytes": total, "pages": len(cx.pages)})
    R.add("weight.total", "%d file(s), %s in total%s" % (len(site.files), fmt_size(total),
                                                          " (stopped counting at %d files: point this at the built folder)" % MAX_FILES if site.truncated else ""),
          None, None, sev=W if (total > TOTAL_LIMIT or site.truncated) else I,
          fix="Compress images and remove unused files; aim for under %s." % fmt_size(TOTAL_LIMIT) if total > TOTAL_LIMIT else None)
    for rel, size in sorted(site.files.items()):
        ext = posixpath.splitext(rel)[1].lower()
        if ext in IMG_EXT and size > IMG_LIMIT:
            R.add("weight.image", "image is %s (over %d KB)" % (fmt_size(size), IMG_LIMIT // 1024), rel, None)
        elif ext in (".css", ".js", ".mjs") and size > BIG_TEXT_LIMIT and site.virtual is None:
            txt = site.read(rel)
            lines = txt.count("\n") + 1
            note = "unminified" if lines > 400 and size / float(lines) < 100 else "very large"
            R.add("weight.file", "%s file is %s (%s)" % (ext[1:], fmt_size(size), note), rel, None)
    # scripts as separate files
    for rel, size in sorted(site.files.items()):
        if posixpath.splitext(rel)[1].lower() in (".js", ".mjs") and size <= 300 * 1024 and site.virtual is None:
            scan_script_urls(cx, rel, site.read(rel))
    if R.external:
        parts = []
        for h, v in sorted(R.external.items()):
            parts.append("%s (%s)" % (h, ", ".join(sorted(v["kinds"]))))
        R.add("net.external", "the site contacts %d outside server(s): %s" % (len(R.external), "; ".join(parts)), None, None)
    for (rule, file), n in sorted(cx.css.over.items()):
        R.add(rule, "...and %d more in this file (only the first 25 are listed)" % n, file, None)


# ---- Secrets ----

def redact(secret):
    n = len(secret)
    return (secret[:4] if n > 12 else secret[:1]) + "...(%d chars)" % n


def _entropy(s):
    c = Counter(s)
    n = float(len(s))
    return -sum((v / n) * math.log(v / n, 2) for v in c.values())


_PLACEHOLDER_WORDS = ("your", "xxxx", "example", "changeme", "change-me", "replace", "placeholder", "dummy", "sample", "<", ">",
                      "${", "{{", "process.env", "os.environ", "getenv", "todo", "redacted", "insert", "fake", "not-a-real",
                      "%s", "****")
_DUMMY_VALUES = frozenset(("password", "passw0rd", "secret", "token", "apikey", "api_key", "changeme", "null", "undefined", "string",
                           "required", "12345678", "abcdefgh", "password1", "password123"))


def plausible_secret(v, strict=True):
    lv = v.lower()
    if lv in _DUMMY_VALUES or any(w in lv for w in _PLACEHOLDER_WORDS):
        return False
    if re.fullmatch(r"[*x#._\-]+", lv) or len(set(v)) < 5:
        return False
    if not strict:
        return True
    if re.fullmatch(r"[a-z0-9]+(?:[._/\-][a-z0-9]+)+", v) and not re.search(r"[A-Z]", v):
        return False
    return bool(re.search(r"\d", v) and re.search(r"[A-Za-z]", v) and _entropy(v) >= 3.0)


def _mixed(m):
    v = m.group(0)
    return bool(re.search(r"[a-z]", v) and re.search(r"[A-Z]", v) and re.search(r"\d", v))


def _has_digit(m):
    return bool(re.search(r"\d", m.group(0)))


def _jwt_service(m):
    try:
        seg = m.group(0).split(".")[1]
        seg += "=" * (-len(seg) % 4)
        data = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")).decode("utf-8", "replace"))
        return isinstance(data, dict) and data.get("role") in ("service_role", "supabase_admin")
    except (ValueError, TypeError):
        return False


def _dburl_ok(m):
    pw, host = m.group("pw"), m.group("host").lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal", "db") or pw.startswith("[") or pw.startswith("$"):
        return False
    return plausible_secret(pw, strict=False)


def _dburl_preview(m):
    return "%s://...:****@%s" % (m.group("scheme"), m.group("host"))


def _p(label, hints, rx, group=0, ok=None, rule="secrets.key", preview=None, flags=0):
    return (label, hints, re.compile(rx, flags), group, ok, rule, preview)


PATTERNS = (
    _p("private key block", ("-----BEGIN",), r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----",
       preview=lambda m: "-----BEGIN ... PRIVATE KEY-----"),
    _p("AWS access key id", ("AKIA", "ASIA", "AGPA", "AIDA", "AROA", "ANPA", "ANVA", "AIPA"),
       r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[A-Z0-9]{16}\b", ok=lambda m: "EXAMPLE" not in m.group(0)),
    _p("AWS secret access key", ("ecret",), r"aws_?secret_?access_?key[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])",
       group=1, flags=re.I),
    _p("GitHub token", ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"), r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    _p("GitHub fine-grained token", ("github_pat_",), r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b"),
    _p("GitLab token", ("glpat-",), r"\bglpat-[A-Za-z0-9_\-]{20,}"),
    _p("Stripe live secret key", ("sk_live_", "rk_live_"), r"\b[sr]k_live_[0-9A-Za-z]{16,}\b"),
    _p("Stripe webhook secret", ("whsec_",), r"\bwhsec_[A-Za-z0-9]{24,}\b"),
    _p("Supabase secret key", ("sb_secret_",), r"\bsb_secret_[A-Za-z0-9_\-]{20,}"),
    _p("Supabase service role key (JWT)", ("eyJ",), r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}", ok=_jwt_service),
    _p("Anthropic API key", ("sk-ant-",), r"\bsk-ant-[A-Za-z0-9_\-]{32,}"),
    _p("OpenAI API key", ("sk-",), r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}", ok=_mixed),
    _p("Resend API key", ("re_",), r"\bre_[A-Za-z0-9]{6,}_[A-Za-z0-9]{16,}\b", ok=_has_digit),
    _p("SendGrid API key", ("SG.",), r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"),
    _p("Slack token", ("xox",), r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    _p("Slack webhook URL", ("hooks.slack.com",), r"https://hooks\.slack\.com/services/T[A-Z0-9]{6,}/B[A-Z0-9]{6,}/[A-Za-z0-9]{16,}"),
    _p("npm token", ("npm_",), r"\bnpm_[A-Za-z0-9]{36}\b"),
    _p("Hugging Face token", ("hf_",), r"\bhf_[A-Za-z0-9]{34,}\b", ok=_mixed),
    _p("Replicate token", ("r8_",), r"\br8_[A-Za-z0-9]{30,}\b"),
    _p("Apify token", ("apify_api_",), r"\bapify_api_[A-Za-z0-9]{30,}\b"),
    _p("Vercel Blob token", ("vercel_blob_rw_",), r"\bvercel_blob_rw_[A-Za-z0-9]+_[A-Za-z0-9]+"),
    _p("Shopify token", ("shpat_", "shpss_", "shpca_", "shppa_"), r"\bshp(?:at|ss|ca|pa)_[a-f0-9]{32}\b"),
    _p("DigitalOcean token", ("dop_v1_",), r"\bdop_v1_[a-f0-9]{64}\b"),
    _p("Telegram bot token", (":AA",), r"\b\d{8,10}:AA[A-Za-z0-9_\-]{33}\b"),
    _p("database URL with password", ("://",),
       r"\b(?P<scheme>postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss|amqps?)://[^\s:/@'\"]+:(?P<pw>[^\s@'\"/]{3,})@(?P<host>[^\s'\"/?#:]+)",
       ok=_dburl_ok, preview=_dburl_preview),
    _p("Google API key", ("AIza",), r"\bAIza[0-9A-Za-z_\-]{35}\b", rule="secrets.public-key"),
)
_G1 = re.compile(r"(api[_-]?key|apikey|secret|token|passw(?:or)?d|pwd|private[_-]?key|access[_-]?key|auth[_-]?key|client[_-]?secret|"
                 r"service[_-]?role)[\w-]*[\"']?\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']", re.I)
_G2 = re.compile(r"^\s*(?:export\s+)?[A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSW(?:OR)?D|PRIVATE_?KEY|ACCESS_?KEY|SERVICE_?ROLE)[A-Z0-9_]*"
                 r"\s*[=:]\s*[\"']?([^\s\"'#]{12,})", re.M)
_ENV_LIKE = (".env", ".sh", ".ini", ".cfg", ".conf", ".properties", ".toml", ".yml", ".yaml", ".envrc")
_SKIP_SCAN = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "composer.lock", "poetry.lock", "cargo.lock")
ENV_TEMPLATES = frozenset((".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults", ".env.test.example"))
CRED_NAMES = frozenset(("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".pgpass", ".htpasswd", ".git-credentials",
                        "credentials", "secrets.json", "secrets.yml", "secrets.yaml", "secrets.toml"))
CRED_EXT = (".pem", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".key")
CRED_PATTERNS = (re.compile(r"^service[-_]?account.*\.json$"), re.compile(r"^serviceaccountkey.*\.json$"),
                 re.compile(r"^client_secret.*\.json$"), re.compile(r".*credentials.*\.json$"))


def is_env_file(base):
    b = base.lower()
    return (b == ".env" or (b.startswith(".env.") and b not in ENV_TEMPLATES) or (b.endswith(".env") and len(b) > 4)) \
        and b not in ENV_TEMPLATES


def is_credential_file(base):
    b = base.lower()
    return b in CRED_NAMES or b.endswith(CRED_EXT) or any(p.match(b) for p in CRED_PATTERNS)


def scan_text(text, name):
    """Return [(rule, label, line, preview)] for one file's text. Never returns a full secret."""
    base = posixpath.basename(name).lower()
    if base in _SKIP_SCAN or base.endswith((".lock", ".map")):
        return []
    out, spans = [], []

    def take(rule, label, s, e, preview):
        if any(s < e2 and s2 < e for s2, e2 in spans):
            return
        spans.append((s, e))
        out.append((rule, label, text.count("\n", 0, s) + 1, preview))

    for label, hints, rx, grp, ok, rule, prev in PATTERNS:
        if hints and not any(h in text for h in hints):
            continue
        for m in rx.finditer(text):
            if ok is not None and not ok(m):
                continue
            s, e = m.span(grp)
            take(rule, label, s, e, prev(m) if prev else redact(m.group(grp)))
    if not base.endswith((".css", ".svg")):
        for m in _G1.finditer(text):
            v = m.group(2)
            if re.search(r"pass|pwd", m.group(1), re.I):     # passwords may be plain words, but not a translated label, path or selector
                found = (plausible_secret(v, strict=False) and (len(v) >= 14 or re.search(r"[\d\W_]", v))
                         and v[0] not in "[</.(" and not v.startswith("~/"))
            else:
                found = len(v) >= 16 and plausible_secret(v)
            if found:
                take("secrets.key", "hard-coded %s value" % m.group(1).lower(), m.start(2), m.end(2), redact(v))
    if base.endswith(_ENV_LIKE) or base.startswith(".env"):
        for m in _G2.finditer(text):
            if plausible_secret(m.group(1)):
                take("secrets.key", "secret assigned in a config file", m.start(1), m.end(1), redact(m.group(1)))
    return out


def _is_text_file(path, name, size):
    if posixpath.splitext(name.lower())[1] in BINARY_EXT or size > MAX_TEXT:
        return False
    try:
        with open(path, "rb") as fh:
            return b"\0" not in fh.read(2048)
    except OSError:
        return False


def scan_file(rep, path, rel, scope):
    """Scan one file on disk for secrets and add findings."""
    try:
        size = os.path.getsize(path)
        if not _is_text_file(path, rel, size):
            return
        with open(path, "rb") as fh:
            data = fh.read(MAX_TEXT)
    except OSError:
        return
    text = data.decode("utf-8", "replace")
    for rule, label, line, preview in scan_text(text, rel):
        extra = {"scope": scope} if scope else {}
        rep.add(rule, "%s: %s" % (label, preview), rel, line, **extra)


def name_findings(rep, rel, base, scope, tracked=False, ignored=False):
    """Findings for .env files and credential files, judged by name alone."""
    extra = {"scope": scope} if scope else {}
    if scope is None:
        where, fix = "would be uploaded with the site", None
    elif tracked:
        where = "is tracked by git, so its secrets are already in your history"
        fix = "Remove it from git (git rm --cached), then rotate every key inside it."
    else:
        where = "is not git-ignored, so it would be committed"
        fix = "Add it to .gitignore before the first commit (ship.py repo adds .env* for you)."
    if is_env_file(base):
        if scope is not None and ignored and not tracked:
            rep.add("secrets.env-ignored", "%s is present and git-ignored" % base, rel, None, scope=scope)
        else:
            rep.add("secrets.file", "%s %s" % (base, where), rel, None, fix=fix, **extra)
        return True
    if is_credential_file(base):
        if scope is not None and ignored and not tracked:
            return False
        rep.add("secrets.file", "credential file %s %s" % (base, where), rel, None, fix=fix, **extra)
        return True
    return False


# ---- git helpers (read files directly; no subprocess)

def git_tracked(root):
    try:
        with open(os.path.join(root, ".git", "index"), "rb") as fh:
            data = fh.read()
        if data[:4] != b"DIRC":
            return None
        ver, n = struct.unpack(">II", data[4:12])
        if ver not in (2, 3):
            return None
        pos, out = 12, set()
        for _ in range(n):
            if pos + 62 > len(data):
                return None
            flags = struct.unpack(">H", data[pos + 60:pos + 62])[0]
            hdr = 64 if (ver >= 3 and flags & 0x4000) else 62
            end = data.find(b"\0", pos + hdr)
            if end < 0:
                return None
            out.add(data[pos + hdr:end].decode("utf-8", "replace"))
            pos += (hdr + (end - pos - hdr) + 8) & ~7
        return out
    except (OSError, struct.error):
        return None


def _glob_to_regex(p):
    i, out = 0, []
    while i < len(p):
        c = p[i]
        if c == "*":
            if p[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if p[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[" and p.find("]", i + 1) > i + 1:
            j = p.find("]", i + 1)
            body = p[i + 1:j]
            neg = body[:1] in ("!", "^")
            body = body[1:] if neg else body
            cls = "".join(t if re.fullmatch(r"[A-Za-z0-9]-[A-Za-z0-9]", t) else re.escape(t)
                          for t in re.findall(r"[A-Za-z0-9]-[A-Za-z0-9]|.", body, re.S))
            out.append("[" + ("^" if neg else "") + cls + "]" if cls else re.escape("["))
            i = j
        elif c == "\\" and i + 1 < len(p):
            out.append(re.escape(p[i + 1]))
            i += 1
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


class GitIgnore:
    """Small subset of .gitignore semantics: enough to know what a first commit would include."""

    def __init__(self, lines):
        self.rules = []
        for raw in lines:
            line = raw.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            neg = line.startswith("!")
            line = line[1:] if neg else line
            line = line.rstrip()
            dir_only = line.endswith("/")
            line = line.rstrip("/")
            if not line:
                continue
            anchored = "/" in line
            rx = _glob_to_regex(line.lstrip("/"))
            try:
                self.rules.append((re.compile(("^" if anchored else "^(?:.*/)?") + rx + "$"), neg, dir_only))
            except re.error:
                continue      # a pattern we cannot read matches nothing

    def ignored(self, rel, is_dir=False):
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            sub = "/".join(parts[:i])
            d = is_dir or i < len(parts)
            state = None
            for rx, neg, dir_only in self.rules:
                if (not dir_only or d) and rx.match(sub):
                    state = not neg
            if state:
                return True
        return False


def _read_lines(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(1 << 20).decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def scan_project(rep, root, extra_ignore=(), skip_dir=None, max_files=6000):
    """Scan the files of a project that git tracks or would commit, plus every .env file."""
    root = os.path.abspath(root)
    rep.check("secrets.key", "secrets.file")
    tracked = git_tracked(root)
    gi = GitIgnore(_read_lines(os.path.join(root, ".gitignore")) + _read_lines(os.path.join(root, ".git", "info", "exclude"))
                   + list(extra_ignore))
    skip_rel = None
    if skip_dir:
        r = os.path.relpath(os.path.abspath(skip_dir), root).replace(os.sep, "/")
        skip_rel = None if r.startswith("..") else ("" if r == "." else r)
    if skip_rel == "":
        return
    count = 0
    for cur, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        rel_cur = os.path.relpath(cur, root).replace(os.sep, "/")
        rel_cur = "" if rel_cur == "." else rel_cur
        keep = []
        for d in dirnames:
            if d in HEAVY_DIRS:
                continue
            rd = _join(rel_cur, d)
            if skip_rel is not None and skip_rel != "" and rd == skip_rel:
                continue
            if gi.ignored(rd, True) and not (tracked and any(t.startswith(rd + "/") for t in tracked)):
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            rel = _join(rel_cur, f)
            count += 1
            if count > max_files:
                rep.stats["project_truncated"] = True
                return
            is_tracked = tracked is not None and rel in tracked
            ign = gi.ignored(rel)
            if name_findings(rep, rel, f, "project", tracked=is_tracked, ignored=ign):
                if ign and not is_tracked:
                    continue
            if is_tracked or not ign:
                scan_file(rep, os.path.join(cur, f), rel, "project")
    rep.stats["project_files"] = count


def scan_site_secrets(cx):
    rep, site = cx.rep, cx.site
    rep.check("secrets.key", "secrets.file", "secrets.public-key")
    if site.virtual is not None:
        for rel, text in site.virtual.items():
            for rule, label, line, preview in scan_text(text, rel):
                rep.add(rule, "%s: %s" % (label, preview), rel, line)
        return
    for rel in sorted(site.files):
        base = posixpath.basename(rel)
        name_findings(rep, rel, base, None)
        scan_file(rep, site.path(rel), rel, None)


# ---- Driver ----

def run_checks(site_dir, root=None, stdin_html=None):
    rep = Report()
    if stdin_html is not None:
        site = Site("-", virtual={"stdin.html": stdin_html})
    else:
        site = Site(site_dir)
    site.load_vercel()
    cx = Ctx(site, rep)
    html_files = sorted(f for f in site.files if f.lower().endswith((".html", ".htm")))
    rep.check("html.parse")
    for rel in html_files:
        cx.pages[rel] = Page(site.read(rel))
    cx.multi_page = len([r for r in cx.pages if posixpath.basename(r).lower() != "404.html"]) >= 2
    for page in cx.pages.values():
        for u in (page.canonical, page.meta.get("og:url")):
            if u and _host(u):
                cx.self_hosts.add(_host(u))
    # css: files first, then <style> blocks and style attributes
    for rel in sorted(f for f in site.files if f.lower().endswith(".css")):
        analyze_css(site.read(rel), rel, posixpath.dirname(rel), cx.css, rep)
    for rel, page in cx.pages.items():
        base = posixpath.dirname(rel)
        for ln, text in page.style_blocks:
            analyze_css(text, rel, base, cx.css, rep, line0=ln - 1)
        for ln, text in page.style_attrs:
            analyze_css("inline style{" + text + "}", rel, base, cx.css, rep, line0=ln - 1)
    check_tokens(rep, cx.css)
    check_contrast(rep, cx.css)
    check_css_flags(cx)
    for rel, page in cx.pages.items():
        check_page(cx, rel, page)
    for file, base_dir, ln, url, label in cx.css.urls:
        if site.virtual is None:
            process_ref(cx, file, base_dir, "res", label, url, ln, None)
            csp_check_url(cx, file, label, url, ln)
    if site.csp is None:
        rep.not_applicable("net.csp")
    if site.virtual is None:
        check_site_level(cx)
    else:
        rep.stats.update({"files": 1, "bytes": sum(site.files.values()), "pages": len(cx.pages)})
        if cx.pages and rep.external:
            rep.add("net.external", "the page contacts %d outside server(s): %s" % (len(rep.external), ", ".join(sorted(rep.external))),
                    None, None)
    scan_site_secrets(cx)
    if root:
        scan_project(rep, root, skip_dir=None if stdin_html is not None else site_dir)
    return rep.finish()


# ---- Output and CLI ----

def render_text(rep, label, strict=False, show_all=False, cap=8):
    s = rep.stats
    out = ["Site check: %s (%d page(s), %d file(s), %s)" % (label, s.get("pages", 0), s.get("files", 0), fmt_size(s.get("bytes", 0))),
           "Result: %s  %d checks passed, %d failed, %d not applicable | %d error(s), %d warning(s)"
           % ("PASS" if rep.ok(strict) else "FAIL", len(rep.passed), len(rep.failed), rep.n_na, rep.errors, rep.warnings)]
    for sev, title in ((E, "ERRORS"), (W, "WARNINGS"), (I, "NOTES")):
        items = [f for f in rep.findings if f["severity"] == sev]
        if not items:
            continue
        out += ["", "%s (%d)" % (title, len(items))]
        shown = Counter()
        hidden = Counter()
        last_fix = {}
        for f in items:
            if not show_all and shown[f["rule"]] >= cap:
                hidden[f["rule"]] += 1
                continue
            shown[f["rule"]] += 1
            loc = f["file"] or ""
            if f["file"] and f["line"]:
                loc = "%s:%d" % (f["file"], f["line"])
            if f.get("scope"):
                loc = "(project) " + loc
            out.append("  [%s] %s%s" % (f["rule"], loc + "  " if loc else "", f["message"]))
            if sev != I and last_fix.get(f["rule"]) != f["fix"]:
                out.append("      fix: " + f["fix"])
                last_fix[f["rule"]] = f["fix"]
        for rule, n in sorted(hidden.items()):
            out.append("  [%s] ...and %d more (use --all or --json to see every one)" % (rule, n))
    return "\n".join(out)


class OneLineParser(argparse.ArgumentParser):
    """argparse, but a usage mistake is one line on stderr (exit 2) instead of a wall of usage text."""

    def error(self, message):
        sys.stderr.write("error: %s (see %s --help)\n" % (message, self.prog))
        raise SystemExit(2)


def main(argv=None):
    ap = OneLineParser(prog="check_site.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", metavar="DIR", help="built site folder (for example dist), or - to read one HTML page from stdin")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    ap.add_argument("--root", metavar="PROJECT", help="project folder: also scan its git-tracked or not-ignored files and .env files for secrets")
    ap.add_argument("--all", action="store_true", help="print every finding (default: at most 8 per rule)")
    a = ap.parse_args(argv)
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
    except (ValueError, OSError):
        pass
    stdin_html = None
    if a.dir == "-":
        stdin_html = sys.stdin.read()
        if stdin_html.startswith("\ufeff"):
            stdin_html = stdin_html[1:]
    elif not os.path.isdir(a.dir):
        sys.stderr.write("error: %s is %s\n" % (a.dir, "a file, not a folder" if os.path.exists(a.dir) else "not a folder that exists"))
        return 2
    if a.root and not os.path.isdir(a.root):
        sys.stderr.write("error: --root %s is not a folder that exists\n" % a.root)
        return 2
    try:
        rep = run_checks(a.dir, root=a.root, stdin_html=stdin_html)
    except OSError as ex:
        sys.stderr.write("error: %s\n" % ex)
        return 2
    if a.json:
        print(json.dumps(rep.to_dict(a.dir, a.strict), indent=2, sort_keys=True))
    else:
        print(render_text(rep, a.dir, a.strict, a.all))
    return 0 if rep.ok(a.strict) else 1


def run_cli():
    try:
        code = main()
        sys.stdout.flush()
        return code
    except BrokenPipeError:          # for example `check_site.py dist | head`
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 0


if __name__ == "__main__":
    sys.exit(run_cli())
