#!/usr/bin/env python3
"""
render_cards.py — renders the animated SVG card stack for the GitHub profile README.

Everything is driven by config.json (content, colours, stagger) and
data/contributions.json (refreshed daily by .github/workflows/profile-cards.yml).

Output, into cards/:
    hero.svg                 name, one-liner, portrait
    tile-site.svg            three linked tiles, rendered at width 32.5% each
    tile-linkedin.svg
    tile-email.svg
    contributions.svg        contributions per month + streak, active days, busiest day
    repo-<name>.svg          one card per featured repo
    graph.svg                contribution graph in the brand colour
    preview.html             all of the above stacked exactly as the README lays them out

Constraints this file respects, because the cards are shown through <img> tags:
    * no <script>, no external requests at render time or view time
    * motion is CSS keyframes in an inline <style> block, plus SMIL <animate>
    * fonts are subset with pyftsubset and embedded as base64 WOFF data URIs
    * every animation is disabled under prefers-reduced-motion
    * STATIC=1 (or --static) emits the final frame only, for screenshots

Usage:
    python scripts/render_cards.py
    STATIC=1 python scripts/render_cards.py --out cards-static
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

try:
    from fontTools.ttLib import TTFont
except ImportError:  # pragma: no cover
    sys.exit("fontTools is required: pip install 'fonttools[woff]'")

ROOT = Path(__file__).resolve().parent.parent

EASE = "cubic-bezier(0.16, 1, 0.3, 1)"
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".gif": "image/gif"}


# ─────────────────────────────────────────────────────────────── small helpers

def esc(text: str) -> str:
    return xml_escape(str(text), {'"': "&quot;", "'": "&apos;"})


def f(value: float) -> str:
    """Trim floats so the SVG stays small and diffs stay readable."""
    return f"{value:.2f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def rgba(hex_colour: str, alpha: float) -> str:
    h = hex_colour.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha:g})"


# ───────────────────────────────────────────────────────── font subset + metrics

class FontShop:
    """Subsets fonts with pyftsubset, embeds them as base64 WOFF, and measures text."""

    def __init__(self, fonts_cfg: dict, root: Path):
        self.root = root
        self.files: dict[tuple[str, str], Path] = {}
        self.families: dict[str, str] = {}
        for kind, spec in fonts_cfg.items():
            self.families[kind] = spec["family"]
            for weight, rel in spec["weights"].items():
                path = root / rel
                if not path.exists():
                    sys.exit(f"missing font file: {rel}")
                self.files[(kind, str(weight))] = path
        self._metrics: dict[tuple[str, str], tuple[dict, int]] = {}
        self._subsets: dict[tuple[str, str, str], str] = {}
        self._subsetter = self._find_subsetter()
        self.missing: set[str] = set()

    @staticmethod
    def _find_subsetter() -> list[str]:
        from shutil import which
        if which("pyftsubset"):
            return ["pyftsubset"]
        return [sys.executable, "-m", "fontTools.subset"]

    def _load_metrics(self, kind: str, weight: str):
        key = (kind, weight)
        if key not in self._metrics:
            font = TTFont(self.files[key], lazy=True)
            cmap = font.getBestCmap()
            hmtx = font["hmtx"]
            upem = font["head"].unitsPerEm
            advances = {ch: hmtx[name][0] for ch, name in cmap.items() if name in hmtx.metrics}
            self._metrics[key] = (advances, upem)
            font.close()
        return self._metrics[key]

    def width(self, text: str, kind: str = "sans", weight: str = "400",
              size: float = 14.0, tracking: float = 0.0) -> float:
        """Advance width in px. Ignores kerning, which is within a pixel for our sizes."""
        advances, upem = self._load_metrics(kind, weight)
        fallback = advances.get(ord("x"), upem // 2)
        total = sum(advances.get(ord(ch), fallback) for ch in text)
        return total * size / upem + tracking * max(len(text) - 1, 0)

    def check(self, text: str, kind: str, weight: str) -> None:
        advances, _ = self._load_metrics(kind, weight)
        for ch in text:
            if ch not in ("\n",) and ord(ch) not in advances:
                self.missing.add(ch)

    def face(self, kind: str, weight: str, chars: set[str]) -> str:
        """@font-face rule carrying a base64 WOFF subset of exactly `chars`."""
        charset = "".join(sorted(chars | {" "}))
        key = (kind, weight, charset)
        if key not in self._subsets:
            src = self.files[(kind, weight)]
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "subset.woff"
                cmd = self._subsetter + [
                    str(src),
                    f"--text={charset}",
                    "--flavor=woff",
                    f"--output-file={out}",
                    "--layout-features=kern,liga,calt,tnum",
                    "--no-hinting",
                    "--desubroutinize",
                    "--name-IDs=",
                    "--drop-tables+=DSIG",
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode != 0 or not out.exists():
                    sys.exit(f"pyftsubset failed for {src.name}:\n{proc.stderr}")
                self._subsets[key] = base64.b64encode(out.read_bytes()).decode("ascii")
        b64 = self._subsets[key]
        family = self.families[kind]
        return (f"@font-face{{font-family:'{family}';font-style:normal;font-weight:{weight};"
                f"src:url(data:font/woff;base64,{b64}) format('woff');}}")


# ──────────────────────────────────────────────────────────────────── card base

class Card:
    """Collects SVG fragments, tracks which glyphs each weight needs, emits the file."""

    def __init__(self, name: str, width: int, height: int, *, ctx: "Ctx",
                 base_delay: float = 0.0, label: str = ""):
        self.name = name
        self.w = width
        self.h = height
        self.ctx = ctx
        self.base_delay = base_delay
        self.label = label or name
        self.body: list[str] = []
        self.defs: list[str] = []
        self.css: list[str] = []
        self.keyframes: dict[str, str] = {}
        self.used: dict[tuple[str, str], set[str]] = {}

    # -- plumbing ---------------------------------------------------------

    def add(self, markup: str) -> None:
        self.body.append(markup)

    def define(self, markup: str) -> None:
        self.defs.append(markup)

    def rule(self, css: str) -> None:
        if css not in self.css:
            self.css.append(css)

    def keyframe(self, name: str, body: str) -> None:
        self.keyframes[name] = body

    def delay(self, seconds: float) -> float:
        return round(self.base_delay + seconds, 3)

    @property
    def static(self) -> bool:
        return self.ctx.static

    # -- animation wrappers ----------------------------------------------

    def enter(self, markup: str, delay: float = 0.0, *, kind: str = "up") -> str:
        """Wrap a fragment in a staggered entrance. Final frame when static."""
        if self.static:
            return f"<g>{markup}</g>"
        if kind == "up":
            self.keyframe("en-up", "from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}")
            cls, dur = "en-up", "0.7s"
        elif kind == "right":
            self.keyframe("en-rt", "from{opacity:0;transform:translateX(-8px)}to{opacity:1;transform:translateX(0)}")
            cls, dur = "en-rt", "0.6s"
        else:
            self.keyframe("en-in", "from{opacity:0}to{opacity:1}")
            cls, dur = "en-in", "0.6s"
        self.rule(f".{cls}{{opacity:0;transform-box:fill-box;transform-origin:center;"
                  f"animation:{cls} {dur} {EASE} forwards;animation-delay:var(--d,0s)}}")
        return f'<g class="{cls}" style="--d:{f(self.delay(delay))}s">{markup}</g>'

    def motion(self, animated: str, static_twin: str) -> str:
        """SMIL-driven markup plus its final-frame twin. CSS swaps them when motion is off."""
        if self.static:
            return static_twin
        self.rule(".twin{display:none}")
        return f'<g class="mo">{animated}</g><g class="twin">{static_twin}</g>'

    # -- drawing ----------------------------------------------------------

    def text(self, x: float, y: float, content: str, *, kind: str = "sans", weight: str = "400",
             size: float = 14, fill: str = "#fff", anchor: str = "start", tracking: float = 0.0,
             opacity: float | None = None, extra: str = "", tabular: bool = False) -> str:
        self.used.setdefault((kind, weight), set()).update(content)
        self.ctx.fonts.check(content, kind, weight)
        family = self.ctx.fonts.families[kind]
        style = (f"font-family:'{family}';font-weight:{weight};font-size:{f(size)}px;"
                 f"fill:{fill}")
        if tracking:
            style += f";letter-spacing:{f(tracking)}px"
        if tabular:
            style += ";font-variant-numeric:tabular-nums"
        if opacity is not None:
            style += f";opacity:{opacity:g}"
        anchor_attr = f' text-anchor="{anchor}"' if anchor != "start" else ""
        return (f'<text x="{f(x)}" y="{f(y)}"{anchor_attr} style="{style}"{extra}>'
                f"{esc(content)}</text>")

    def fit(self, content: str, max_width: float, *, kind: str, weight: str,
            size: float, min_size: float = 9.0, tracking: float = 0.0) -> tuple[str, float]:
        """Shrink, then ellipsize, until the string fits max_width."""
        s = size
        while s > min_size and self.ctx.fonts.width(content, kind, weight, s, tracking) > max_width:
            s -= 0.25
        if self.ctx.fonts.width(content, kind, weight, s, tracking) <= max_width:
            return content, s
        ell = "…"
        trimmed = content
        while trimmed and self.ctx.fonts.width(trimmed + ell, kind, weight, s, tracking) > max_width:
            trimmed = trimmed[:-1]
        return (trimmed.rstrip() + ell), s

    def wrap(self, content: str, max_width: float, *, kind: str, weight: str,
             size: float, lines: int) -> list[str]:
        words, out, current = content.split(), [], ""
        for word in words:
            trial = f"{current} {word}".strip()
            if self.ctx.fonts.width(trial, kind, weight, size) <= max_width or not current:
                current = trial
            else:
                out.append(current)
                current = word
                if len(out) == lines:
                    break
        if current and len(out) < lines:
            out.append(current)
        if len(out) > lines:
            out = out[:lines]
        if out:
            consumed = len(" ".join(out).split())
            if consumed < len(words):
                tail, _ = self.fit(out[-1] + " " + words[consumed], max_width,
                                   kind=kind, weight=weight, size=size, min_size=size)
                out[-1] = tail
        return out

    def panel(self, x: float = 0.5, y: float = 0.5, w: float | None = None, h: float | None = None,
              *, radius: float | None = None, fill: str | None = None,
              stroke: str | None = None) -> str:
        t = self.ctx.theme
        w = (self.w - 1) if w is None else w
        h = (self.h - 1) if h is None else h
        return (f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" '
                f'rx="{f(radius if radius is not None else t["radius"])}" '
                f'fill="{fill or t["bg_card"]}" stroke="{stroke or t["border"]}" stroke-width="1"/>')

    # -- output -----------------------------------------------------------

    def render(self) -> str:
        faces = "".join(self.ctx.fonts.face(kind, weight, chars)
                        for (kind, weight), chars in sorted(self.used.items()) if chars)
        keyframes = "".join(f"@keyframes {name}{{{body}}}" for name, body in self.keyframes.items())
        reduced = (
            "@media (prefers-reduced-motion:reduce){"
            "*{animation:none!important;transition:none!important}"
            "[class]{opacity:1!important;transform:none!important}"
            # Elements whose resting state is mid-animation need their end state
            # restored by hand: the rule is drawn by width, the sheen is a moving
            # highlight that would otherwise freeze as a band across the card.
            ".dash{width:20px!important}"
            ".sheen{display:none!important}"
            ".mo{display:none!important}.twin{display:block!important}}"
        )
        style = faces + "".join(self.css) + keyframes + ("" if self.static else reduced)
        defs = f"<defs>{''.join(self.defs)}</defs>" if self.defs else ""
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" '
            f'role="img" aria-label="{esc(self.label)}">'
            f"<title>{esc(self.label)}</title>"
            f"{defs}<style>{style}</style>"
            f"{''.join(self.body)}"
            "</svg>"
        )


class Ctx:
    def __init__(self, cfg: dict, fonts: FontShop, static: bool, data: dict):
        self.cfg = cfg
        self.theme = cfg["theme"]
        self.fonts = fonts
        self.static = static
        self.data = data


# ─────────────────────────────────────────────────────────── shared decorations

def grid_and_glow(card: Card, *, glow: bool = True) -> None:
    """The site's faint grid plus a drifting amber blob, clipped to the card."""
    t = card.ctx.theme
    card.define(
        f'<clipPath id="cp"><rect x="0.5" y="0.5" width="{f(card.w - 1)}" '
        f'height="{f(card.h - 1)}" rx="{f(t["radius"])}"/></clipPath>'
        '<pattern id="grid" width="28" height="28" patternUnits="userSpaceOnUse">'
        f'<path d="M28 0H0v28" fill="none" stroke="{rgba(t["text_primary"], 0.028)}" stroke-width="1"/>'
        "</pattern>"
        '<radialGradient id="blob"><stop offset="0%" stop-color="'
        f'{rgba(t["accent"], 0.22)}"/><stop offset="70%" stop-color="{rgba(t["accent"], 0)}"/>'
        "</radialGradient>"
        '<radialGradient id="blob2"><stop offset="0%" stop-color="'
        f'{rgba(t["purple"], 0.16)}"/><stop offset="70%" stop-color="{rgba(t["purple"], 0)}"/>'
        "</radialGradient>"
    )
    inner = f'<rect width="{card.w}" height="{card.h}" fill="url(#grid)"/>'
    if glow:
        if not card.static:
            card.keyframe("drift", "0%,100%{transform:translate(0,0) scale(1)}"
                                   "50%{transform:translate(26px,-18px) scale(1.08)}")
            card.rule(".blob{transform-box:fill-box;transform-origin:center;"
                      "animation:drift 11s ease-in-out infinite}")
            card.keyframe("drift2", "0%,100%{transform:translate(0,0) scale(1)}"
                                    "50%{transform:translate(-22px,16px) scale(1.06)}")
            card.rule(".blob2{transform-box:fill-box;transform-origin:center;"
                      "animation:drift2 14s ease-in-out infinite}")
        inner += (f'<ellipse class="blob" cx="{f(card.w * 0.08)}" cy="-10" rx="260" ry="190" fill="url(#blob)"/>'
                  f'<ellipse class="blob2" cx="{f(card.w * 0.92)}" cy="{f(card.h)}" rx="220" ry="150" fill="url(#blob2)"/>')
    card.add(f'<g clip-path="url(#cp)">{inner}</g>')


def eyebrow(card: Card, x: float, y: float, text: str, *, size: float = 9.5,
            tracking: float = 1.3, dash: bool = False) -> str:
    """Uppercase amber label, optionally preceded by the site's little rule."""
    t = card.ctx.theme
    out = ""
    text_x = x
    if dash:
        if card.static:
            out += (f'<rect x="{f(x)}" y="{f(y - 4)}" width="20" height="1.5" rx="1" '
                    f'fill="{t["accent"]}"/>')
        else:
            card.keyframe("draw", "from{width:0}to{width:20px}")
            card.rule(".dash{animation:draw .5s ease forwards;animation-delay:var(--d,0s)}")
            out += (f'<rect class="dash" x="{f(x)}" y="{f(y - 4)}" width="0" height="1.5" rx="1" '
                    f'fill="{t["accent"]}" style="--d:{f(card.delay(0.15))}s"/>')
        text_x = x + 28
    out += card.text(text_x, y, text, kind="mono", weight="500", size=size,
                     fill=t["accent"], tracking=tracking)
    return out


def arrow(x: float, y: float, colour: str, size: float = 9.0, *, nudge: bool = False,
          card: Card | None = None, delay: float = 0.0) -> str:
    """Small → drawn as paths, because the latin font subsets carry no arrow glyph."""
    body = (f'<path d="M{f(x)} {f(y)}h{f(size)}" stroke="{colour}" stroke-width="1.4" '
            f'stroke-linecap="round"/>'
            f'<path d="M{f(x + size - 3.2)} {f(y - 3.2)}l3.2 3.2-3.2 3.2" fill="none" '
            f'stroke="{colour}" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>')
    if nudge and card is not None and not card.static:
        card.keyframe("nudge", "0%,100%{transform:translateX(0)}50%{transform:translateX(3px)}")
        card.rule(".nudge{transform-box:fill-box;animation:nudge 2.4s ease-in-out infinite;"
                  "animation-delay:var(--d,0s)}")
        return f'<g class="nudge" style="--d:{f(card.delay(delay))}s">{body}</g>'
    return body


def star(x: float, y: float, colour: str, r: float = 5.2) -> str:
    """Five-pointed star path, same reason as the arrow."""
    import math
    pts = []
    for i in range(10):
        angle = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.42
        pts.append(f"{f(x + rad * math.cos(angle))},{f(y + rad * math.sin(angle))}")
    return f'<polygon points="{" ".join(pts)}" fill="{colour}"/>'


def sheen(card: Card, x: float, y: float, w: float, h: float, delay: float,
          radius: float | None = None) -> str:
    """One-shot light sweep across a card. :hover never fires inside an <img>."""
    if card.static:
        return ""
    t = card.ctx.theme
    gid = f"sheen{abs(hash((card.name, x, y))) % 9999}"
    card.define(
        f'<linearGradient id="{gid}" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0%" stop-color="{rgba(t["text_primary"], 0)}"/>'
        f'<stop offset="50%" stop-color="{rgba(t["text_primary"], 0.07)}"/>'
        f'<stop offset="100%" stop-color="{rgba(t["text_primary"], 0)}"/></linearGradient>'
    )
    card.keyframe("sweep", "0%{transform:translateX(-100%)}100%{transform:translateX(220%)}")
    card.rule(".sheen{transform-box:fill-box;animation:sweep 1.5s ease-in-out forwards;"
              "animation-delay:var(--d,0s)}")
    return (f'<g clip-path="url(#cp)"><rect class="sheen" x="{f(x)}" y="{f(y)}" '
            f'width="{f(w * 0.55)}" height="{f(h)}" fill="url(#{gid})" '
            f'style="--d:{f(card.delay(delay))}s"/></g>')


# ───────────────────────────────────────────────────────────────────── the cards

def build_hero(ctx: Ctx) -> Card:
    t, ident = ctx.theme, ctx.cfg["identity"]
    portrait_path = ROOT / ident.get("portrait", "")
    has_portrait = portrait_path.suffix.lower() in MIME and portrait_path.exists()

    card = Card("hero", 880, 268, ctx=ctx,
                base_delay=ctx.cfg["stagger"]["hero"],
                label=f'{ident["name"]} — {ident["one_liner"]}')
    card.add(card.panel())
    grid_and_glow(card)

    art_x, art_size = 700, 132
    left_limit = art_x - 48
    x = 44

    card.add(card.enter(eyebrow(card, x, 56, ident["label"], dash=True), 0.05))

    name, name_size = card.fit(ident["name"], left_limit - x, kind="sans", weight="800",
                               size=40, min_size=26)
    card.add(card.enter(card.text(x, 106, name, kind="sans", weight="800", size=name_size,
                                  fill=t["text_primary"], tracking=-1.2), 0.18))

    # One-liner, with the site's amber tail + blinking cursor.
    line = ident["one_liner"]
    head, tail = (line.rsplit(" ", 2)[0], " ".join(line.rsplit(" ", 2)[1:])) if line.count(" ") >= 2 else (line, "")
    one_size = 17.0
    while card.ctx.fonts.width(line, "sans", "400", one_size) > (left_limit - x - 14) and one_size > 12:
        one_size -= 0.25
    head_w = card.ctx.fonts.width(head + " ", "sans", "400", one_size)
    frag = card.text(x, 141, head + " ", kind="sans", weight="400", size=one_size,
                     fill=t["text_secondary"])
    if tail:
        frag += card.text(x + head_w, 141, tail, kind="sans", weight="700", size=one_size,
                          fill=t["accent_2"])
    cursor_x = x + head_w + card.ctx.fonts.width(tail, "sans", "700", one_size) + 4
    if card.static:
        frag += f'<rect x="{f(cursor_x)}" y="{f(141 - one_size * 0.78)}" width="2.5" height="{f(one_size)}" rx="1" fill="{t["accent"]}"/>'
    else:
        card.keyframe("blink", "0%,100%{opacity:1}50%{opacity:0}")
        card.rule(".cur{animation:blink 1s step-end infinite;animation-delay:var(--d,0s)}")
        frag += (f'<rect class="cur" x="{f(cursor_x)}" y="{f(141 - one_size * 0.78)}" width="2.5" '
                 f'height="{f(one_size)}" rx="1" fill="{t["accent"]}" style="--d:{f(card.delay(1.1))}s"/>')
    card.add(card.enter(frag, 0.3))

    sub = card.wrap(ident["sub_line"], left_limit - x, kind="sans", weight="400", size=12.5, lines=2)
    for i, row in enumerate(sub):
        card.add(card.enter(card.text(x, 170 + i * 18, row, kind="sans", weight="400",
                                      size=12.5, fill=t["text_muted"]), 0.4 + i * 0.06))

    # Stack chips.
    chips, chip_x = ["LangGraph", "RAG", "MCP", "Python", "Quant"], x
    for i, chip in enumerate(chips):
        cw = card.ctx.fonts.width(chip, "mono", "400", 10) + 20
        chip_markup = (
            f'<rect x="{f(chip_x)}" y="210" width="{f(cw)}" height="24" rx="6" '
            f'fill="{rgba(t["accent"], 0.09)}" stroke="{rgba(t["accent"], 0.2)}" stroke-width="1"/>'
            + card.text(chip_x + 10, 226, chip, kind="mono", weight="400", size=10, fill=t["accent"])
        )
        card.add(card.enter(chip_markup, 0.62 + i * 0.07, kind="right"))
        chip_x += cw + 8

    # Portrait, or the monogram fallback if no file is present.
    cx, cy, r = art_x + art_size / 2, card.h / 2, art_size / 2
    card.define(f'<clipPath id="pc"><circle cx="{f(cx)}" cy="{f(cy)}" r="{f(r - 6)}"/></clipPath>'
                f'<linearGradient id="ring" x1="0" y1="0" x2="1" y2="1">'
                f'<stop offset="0%" stop-color="{t["accent"]}"/>'
                f'<stop offset="55%" stop-color="{t["accent_soft"]}"/>'
                f'<stop offset="100%" stop-color="{t["purple"]}"/></linearGradient>')

    if has_portrait:
        b64 = base64.b64encode(portrait_path.read_bytes()).decode("ascii")
        art = (f'<image clip-path="url(#pc)" x="{f(cx - r + 6)}" y="{f(cy - r + 6)}" '
               f'width="{f((r - 6) * 2)}" height="{f((r - 6) * 2)}" '
               f'preserveAspectRatio="xMidYMid slice" '
               f'xlink:href="data:{MIME[portrait_path.suffix.lower()]};base64,{b64}"/>')
    else:
        art = (f'<circle cx="{f(cx)}" cy="{f(cy)}" r="{f(r - 6)}" fill="{t["bg_inset"]}"/>'
               + card.text(cx, cy + 15, ident.get("monogram", "DC"), kind="sans", weight="800",
                           size=46, fill=t["accent"], anchor="middle", tracking=-1.5))

    # The ring draws itself with SMIL; reduced motion must not leave it half-drawn,
    # so the static twin is a plain full circle.
    circumference = 2 * 3.14159 * (r - 2)
    ring_static = (f'<circle cx="{f(cx)}" cy="{f(cy)}" r="{f(r - 2)}" fill="none" '
                   f'stroke="url(#ring)" stroke-width="2"/>')
    ring_animated = (
        f'<circle cx="{f(cx)}" cy="{f(cy)}" r="{f(r - 2)}" fill="none" stroke="url(#ring)" '
        f'stroke-width="2" stroke-linecap="round" stroke-dasharray="{f(circumference)}" '
        f'stroke-dashoffset="{f(circumference)}" transform="rotate(-90 {f(cx)} {f(cy)})">'
        f'<animate attributeName="stroke-dashoffset" values="{f(circumference)};0" dur="1.3s" '
        f'begin="{f(card.delay(0.35))}s" fill="freeze" calcMode="spline" keyTimes="0;1" '
        f'keySplines="0.16 1 0.3 1"/></circle>'
    )
    card.add(card.enter(art + card.motion(ring_animated, ring_static), 0.25))

    # Availability pill under the portrait.
    pill = (f'<rect x="{f(cx - 62)}" y="{f(cy + r + 2)}" width="124" height="22" rx="11" '
            f'fill="{rgba(t["green"], 0.1)}" stroke="{rgba(t["green"], 0.22)}" stroke-width="1"/>')
    dot = f'<circle cx="{f(cx - 48)}" cy="{f(cy + r + 13)}" r="3.4" fill="{t["green"]}"/>'
    if not card.static:
        card.keyframe("pulse", "0%,100%{opacity:1}50%{opacity:0.35}")
        card.rule(".pdot{animation:pulse 2s ease infinite}")
        dot = dot.replace("<circle", '<circle class="pdot"')
    pill += dot + card.text(cx - 38, cy + r + 17, "open to work", kind="mono", weight="400",
                            size=9.5, fill=t["green"])
    card.add(card.enter(pill, 0.85))
    card.add(sheen(card, -200, 0, card.w + 200, card.h, 0.5))
    return card


def build_tile(ctx: Ctx, tile: dict, index: int) -> Card:
    t = ctx.theme
    stag = ctx.cfg["stagger"]
    card = Card(f'tile-{tile["id"]}', 420, 136, ctx=ctx,
                base_delay=stag["tiles"] + index * stag["tile_step"],
                label=f'{tile["eyebrow"]}: {tile["title"]}')
    card.add(card.panel(radius=t["radius_sm"]))
    card.define(f'<clipPath id="cp"><rect x="0.5" y="0.5" width="{f(card.w - 1)}" '
                f'height="{f(card.h - 1)}" rx="{f(t["radius_sm"])}"/></clipPath>'
                f'<linearGradient id="bar" x1="0" y1="0" x2="1" y2="0">'
                f'<stop offset="0%" stop-color="{t["accent"]}"/>'
                f'<stop offset="100%" stop-color="{t["accent_2"]}"/></linearGradient>')

    # Top accent bar, mirroring .contact-card::before on the site.
    bar_animated = (f'<rect x="0.5" y="0.5" width="0" height="2.5" fill="url(#bar)">'
                    f'<animate attributeName="width" from="0" to="{f(card.w - 1)}" dur="0.6s" '
                    f'begin="{f(card.delay(0.1))}s" fill="freeze" calcMode="spline" '
                    f'keyTimes="0;1" keySplines="0.16 1 0.3 1"/></rect>')
    bar_static = f'<rect x="0.5" y="0.5" width="{f(card.w - 1)}" height="2.5" fill="url(#bar)"/>'
    card.add(f'<g clip-path="url(#cp)">{card.motion(bar_animated, bar_static)}</g>')

    glyphs = {
        "site": (f'<circle cx="0" cy="0" r="10" fill="none" stroke="{t["accent"]}" stroke-width="1.5"/>'
                 f'<path d="M-10 0h20M0-10c4 4 4 16 0 20M0-10c-4 4-4 16 0 20" fill="none" '
                 f'stroke="{t["accent"]}" stroke-width="1.3"/>'),
        "linkedin": (f'<rect x="-10" y="-10" width="20" height="20" rx="4" fill="none" '
                     f'stroke="{t["accent"]}" stroke-width="1.5"/>'
                     f'<path d="M-5.5-3.5v8M-5.5-6.6v0.1M-1-3.5v8M-1 0.5c0-3 5-3 5 0v4.5" fill="none" '
                     f'stroke="{t["accent"]}" stroke-width="1.5" stroke-linecap="round"/>'),
        "mail": (f'<rect x="-11" y="-8" width="22" height="16" rx="3" fill="none" '
                 f'stroke="{t["accent"]}" stroke-width="1.5"/>'
                 f'<path d="M-11-6l11 8 11-8" fill="none" stroke="{t["accent"]}" stroke-width="1.5" '
                 f'stroke-linejoin="round"/>'),
    }
    icon = glyphs.get(tile.get("glyph", ""), glyphs["site"])
    card.add(card.enter(f'<g transform="translate(42,46)">{icon}</g>', 0.2))

    card.add(card.enter(eyebrow(card, 70, 40, tile["eyebrow"], size=9, tracking=1.4), 0.26))

    title, title_size = card.fit(tile["title"], card.w - 70 - 34, kind="sans", weight="700",
                                 size=16.5, min_size=11)
    card.add(card.enter(card.text(70, 66, title, kind="sans", weight="700", size=title_size,
                                  fill=t["text_primary"], tracking=-0.3), 0.32))

    blurb, blurb_size = card.fit(tile["blurb"], card.w - 70 - 34, kind="sans", weight="400",
                                 size=12, min_size=10)
    card.add(card.enter(card.text(70, 88, blurb, kind="sans", weight="400", size=blurb_size,
                                  fill=t["text_secondary"]), 0.38))

    foot = card.text(42, 113, "open", kind="mono", weight="500", size=10,
                     fill=t["accent"], tracking=0.6)
    foot += arrow(78, 110, t["accent"], 10, nudge=True, card=card, delay=0.9)
    card.add(card.enter(foot, 0.46))
    card.add(sheen(card, -120, 0, card.w + 120, card.h, 0.3))
    return card


def build_chart(ctx: Ctx) -> Card:
    t = ctx.theme
    stats = ctx.data["stats"]
    months = ctx.data["months"]

    card = Card("contributions", 880, 320, ctx=ctx, base_delay=ctx.cfg["stagger"]["chart"],
                label=(f'Contributions per month: {stats["total"]} in the last year, '
                       f'current streak {stats["current_streak"]} days, '
                       f'{stats["active_days"]} active days, '
                       f'busiest day {stats["busiest_label"]}'))
    card.add(card.panel())
    grid_and_glow(card, glow=True)

    card.add(card.enter(eyebrow(card, 44, 44, "CONTRIBUTIONS · LAST 12 MONTHS", dash=True), 0.05))
    total_text = f'{stats["total"]:,}'
    card.add(card.enter(card.text(44, 84, total_text, kind="sans", weight="800", size=30,
                                  fill=t["text_primary"], tracking=-1, tabular=True), 0.14))
    tw = card.ctx.fonts.width(total_text, "sans", "800", 30)
    card.add(card.enter(card.text(44 + tw + 16, 84, "commits, PRs, issues, reviews",
                                  kind="sans", weight="400", size=12, fill=t["text_muted"]), 0.2))

    # Stat blocks, top right.
    blocks = [
        ("CURRENT STREAK", f'{stats["current_streak"]}', "days", t["accent"]),
        ("ACTIVE DAYS", f'{stats["active_days"]}', f'of {stats["total_days"]}', t["text_primary"]),
        ("BUSIEST DAY", f'{stats["busiest_count"]}', f'on {stats["busiest_date_short"]}', t["accent_2"]),
    ]
    bw, bx0 = 128, 880 - 44 - 3 * 128 - 2 * 10
    for i, (label, value, note, colour) in enumerate(blocks):
        bx = bx0 + i * (bw + 10)
        markup = (f'<rect x="{f(bx)}" y="34" width="{bw}" height="62" rx="10" '
                  f'fill="{t["bg_inset"]}" stroke="{t["border"]}" stroke-width="1"/>'
                  + card.text(bx + 12, 54, label, kind="mono", weight="500", size=8.2,
                              fill=t["text_muted"], tracking=0.9)
                  + card.text(bx + 12, 82, value, kind="sans", weight="800", size=22,
                              fill=colour, tracking=-0.8, tabular=True))
        vw = card.ctx.fonts.width(value, "sans", "800", 22)
        note_fit, note_size = card.fit(note, bw - 24 - vw - 6, kind="mono", weight="400",
                                       size=9.5, min_size=7.5)
        markup += card.text(bx + 12 + vw + 6, 82, note_fit, kind="mono", weight="400",
                            size=note_size, fill=t["text_secondary"])
        card.add(card.enter(markup, 0.3 + i * 0.1))

    # Bars.
    plot_x, plot_right = 44, 880 - 44
    base_y, plot_top = 262, 124
    plot_h = base_y - plot_top
    peak = max([m["count"] for m in months] + [1])
    slot = (plot_right - plot_x) / max(len(months), 1)
    bar_w = min(slot - 14, 46)

    card.define(f'<linearGradient id="barfill" x1="0" y1="0" x2="0" y2="1">'
                f'<stop offset="0%" stop-color="{t["accent_2"]}"/>'
                f'<stop offset="100%" stop-color="{rgba(t["accent"], 0.45)}"/></linearGradient>')

    for i in range(4):
        gy = plot_top + (plot_h / 3) * i
        card.add(f'<line x1="{f(plot_x)}" y1="{f(gy)}" x2="{f(plot_right)}" y2="{f(gy)}" '
                 f'stroke="{t["border"]}" stroke-width="1" stroke-dasharray="2 4" opacity="0.7"/>')
    card.add(card.text(plot_x, plot_top - 8, f"peak {peak}", kind="mono", weight="400",
                       size=9, fill=t["text_muted"]))

    for i, month in enumerate(months):
        cx = plot_x + slot * i + slot / 2
        bx = cx - bar_w / 2
        height = (month["count"] / peak) * plot_h if peak else 0
        height = max(height, 2.5) if month["count"] else 2.0
        delay = card.delay(0.42 + i * 0.055)
        animated = (
            f'<rect x="{f(bx)}" y="{f(base_y)}" width="{f(bar_w)}" height="0" rx="4" '
            f'fill="url(#barfill)">'
            f'<animate attributeName="y" values="{f(base_y)};{f(base_y - height)}" dur="0.85s" '
            f'begin="{f(delay)}s" fill="freeze" calcMode="spline" keyTimes="0;1" '
            f'keySplines="0.16 1 0.3 1"/>'
            f'<animate attributeName="height" values="0;{f(height)}" dur="0.85s" '
            f'begin="{f(delay)}s" fill="freeze" calcMode="spline" keyTimes="0;1" '
            f'keySplines="0.16 1 0.3 1"/></rect>'
        )
        static_bar = (f'<rect x="{f(bx)}" y="{f(base_y - height)}" width="{f(bar_w)}" '
                      f'height="{f(height)}" rx="4" fill="url(#barfill)"/>')
        card.add(card.motion(animated, static_bar))
        value_label = card.text(cx, base_y - height - 8, str(month["count"]), kind="mono",
                                weight="500", size=9.5, fill=t["text_secondary"],
                                anchor="middle", tabular=True)
        card.add(card.enter(value_label, 0.42 + i * 0.055 + 0.5, kind="in"))
        card.add(card.enter(card.text(cx, base_y + 20, month["label"], kind="mono", weight="400",
                                      size=9.5, fill=t["text_muted"], anchor="middle"),
                            0.42 + i * 0.055 + 0.15, kind="in"))

    card.add(f'<line x1="{f(plot_x)}" y1="{f(base_y)}" x2="{f(plot_right)}" y2="{f(base_y)}" '
             f'stroke="{t["border_hover"]}" stroke-width="1"/>')

    longest = (f'longest streak {stats["longest_streak"]} days · '
               f'best month {stats["best_month"]} · updated {ctx.data["updated"]}')
    card.add(card.enter(card.text(plot_x, 300, longest, kind="mono", weight="400", size=9,
                                  fill=t["text_muted"]), 1.4, kind="in"))
    return card


def build_repo(ctx: Ctx, repo: dict, index: int) -> Card:
    t = ctx.theme
    stag = ctx.cfg["stagger"]
    colours = ctx.cfg["language_colors"]
    card = Card(f'repo-{repo["name"]}', 430, 164, ctx=ctx,
                base_delay=stag["repos"] + index * stag["repo_step"],
                label=f'{repo["name"]} — {repo["blurb"]}')
    card.add(card.panel(radius=t["radius_sm"]))
    card.define(f'<clipPath id="cp"><rect x="0.5" y="0.5" width="{f(card.w - 1)}" '
                f'height="{f(card.h - 1)}" rx="{f(t["radius_sm"])}"/></clipPath>'
                f'<linearGradient id="edge" x1="0" y1="0" x2="1" y2="1">'
                f'<stop offset="0%" stop-color="{rgba(t["accent"], 0.55)}"/>'
                f'<stop offset="55%" stop-color="{rgba(t["accent"], 0)}"/>'
                f'<stop offset="100%" stop-color="{rgba(t["purple"], 0.3)}"/></linearGradient>')

    edge_animated = (f'<rect x="0.5" y="0.5" width="{f(card.w - 1)}" height="{f(card.h - 1)}" '
                     f'rx="{f(t["radius_sm"])}" fill="none" stroke="url(#edge)" stroke-width="1" '
                     f'opacity="0"><animate attributeName="opacity" values="0;1" dur="0.7s" '
                     f'begin="{f(card.delay(0.3))}s" fill="freeze"/></rect>')
    edge_static = (f'<rect x="0.5" y="0.5" width="{f(card.w - 1)}" height="{f(card.h - 1)}" '
                   f'rx="{f(t["radius_sm"])}" fill="none" stroke="url(#edge)" stroke-width="1"/>')
    card.add(card.motion(edge_animated, edge_static))

    x = 28
    tag_w = card.ctx.fonts.width(repo["tag"], "mono", "500", 8.4, 1.1) + 20
    tag = (f'<rect x="{f(x)}" y="26" width="{f(tag_w)}" height="20" rx="5" '
           f'fill="{rgba(t["accent"], 0.1)}" stroke="{rgba(t["accent"], 0.2)}" stroke-width="1"/>'
           + card.text(x + 10, 40, repo["tag"], kind="mono", weight="500", size=8.4,
                       fill=t["accent"], tracking=1.1))
    card.add(card.enter(tag, 0.08, kind="right"))

    name, name_size = card.fit(repo["name"], card.w - 2 * x, kind="mono", weight="500",
                               size=15, min_size=10.5)
    card.add(card.enter(card.text(x, 72, name, kind="mono", weight="500", size=name_size,
                                  fill=t["text_primary"]), 0.16))

    for i, row in enumerate(card.wrap(repo["blurb"], card.w - 2 * x, kind="sans", weight="400",
                                      size=11.5, lines=2)):
        card.add(card.enter(card.text(x, 94 + i * 17, row, kind="sans", weight="400", size=11.5,
                                      fill=t["text_secondary"]), 0.24 + i * 0.06))

    # Footer: language dot, stars, forks.
    fy = 140
    card.add(f'<line x1="{f(x)}" y1="{f(fy - 18)}" x2="{f(card.w - x)}" y2="{f(fy - 18)}" '
             f'stroke="{t["border"]}" stroke-width="1"/>')
    lang = repo.get("language") or "Code"
    dot_colour = colours.get(lang, t["text_muted"])
    foot = (f'<circle cx="{f(x + 5)}" cy="{f(fy - 4)}" r="5" fill="{dot_colour}"/>'
            + card.text(x + 16, fy, lang, kind="mono", weight="400", size=10,
                        fill=t["text_secondary"]))
    sx = x + 16 + card.ctx.fonts.width(lang, "mono", "400", 10) + 20
    foot += star(sx + 5, fy - 4, t["accent"]) + card.text(
        sx + 15, fy, str(repo.get("stars", 0)), kind="mono", weight="500", size=10,
        fill=t["text_primary"], tabular=True)
    fx = sx + 15 + card.ctx.fonts.width(str(repo.get("stars", 0)), "mono", "500", 10) + 18
    foot += (f'<path d="M{f(fx)} {f(fy - 9)}v4a3 3 0 003 3h3a3 3 0 003-3v-4M{f(fx + 4.5)} '
             f'{f(fy - 1)}v3" fill="none" stroke="{t["text_muted"]}" stroke-width="1.3" '
             f'stroke-linecap="round"/>'
             + card.text(fx + 14, fy, str(repo.get("forks", 0)), kind="mono", weight="400",
                         size=10, fill=t["text_secondary"], tabular=True))
    card.add(card.enter(foot, 0.4, kind="in"))
    card.add(card.enter(arrow(card.w - x - 12, fy - 4, t["accent"], 10, nudge=True,
                              card=card, delay=1.0), 0.46, kind="in"))
    card.add(sheen(card, -140, 0, card.w + 140, card.h, 0.35))
    return card


def build_graph(ctx: Ctx) -> Card:
    t = ctx.theme
    weeks = ctx.data["weeks"]
    stats = ctx.data["stats"]

    # Fill the same 880px column as the other cards, whatever the week count.
    left, right, gap, top = 70, 44, 3.2, 64
    column = (880 - left - right) / max(len(weeks), 1)
    cell = column - gap
    card = Card("graph", 880, 212, ctx=ctx, base_delay=ctx.cfg["stagger"]["graph"],
                label=f'Contribution graph: {stats["total"]} contributions in the last year')
    card.add(card.panel())
    grid_and_glow(card, glow=False)
    card.add(card.enter(eyebrow(card, 44, 42, "CONTRIBUTION GRAPH", dash=True), 0.05))

    levels = {
        0: rgba(t["text_primary"], 0.055),
        1: rgba(t["accent"], 0.3),
        2: rgba(t["accent"], 0.55),
        3: t["accent"],
        4: t["accent_2"],
    }
    for level, colour in levels.items():
        card.rule(f".l{level}{{fill:{colour}}}")

    if not card.static:
        card.keyframe("pop", "from{opacity:0;transform:scale(0.4)}to{opacity:1;transform:scale(1)}")
        card.rule(".c{opacity:0;transform-box:fill-box;transform-origin:center;"
                  "animation:pop .45s ease-out forwards;animation-delay:var(--d,0s)}")

    cells, month_labels, last_month = [], [], None
    for wi, week in enumerate(weeks):
        cx = left + wi * column
        for di, day in enumerate(week):
            if day is None:
                continue
            cy = top + di * column
            level = day["level"]
            attrs = (f'x="{f(cx)}" y="{f(cy)}" width="{f(cell)}" height="{f(cell)}" rx="2.5" '
                     f'class="{"c " if not card.static else ""}l{level}"')
            if card.static:
                cells.append(f"<rect {attrs}/>")
            else:
                # Diagonal wave: delay rises with week and day, so it sweeps left to right.
                d = card.delay(0.2 + wi * 0.013 + di * 0.025)
                cells.append(f'<rect {attrs} style="--d:{f(d)}s"/>')
        first = next((d for d in week if d), None)
        if first:
            month = datetime.strptime(first["date"], "%Y-%m-%d").strftime("%b")
            if month != last_month and wi < len(weeks) - 2:
                month_labels.append((cx, month))
                last_month = month
    card.add("".join(cells))

    for cx, month in month_labels:
        card.add(card.enter(card.text(cx, top - 10, month, kind="mono", weight="400", size=9,
                                      fill=t["text_muted"]), 0.12, kind="in"))
    for di, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        card.add(card.enter(card.text(left - 10, top + di * column + 9, name, kind="mono",
                                      weight="400", size=9, fill=t["text_muted"], anchor="end"),
                            0.12, kind="in"))

    legend_y = top + 7 * column + 26
    legend = card.text(left, legend_y + 9, "Less", kind="mono", weight="400", size=9,
                       fill=t["text_muted"])
    lx = left + 32
    for level in range(5):
        legend += (f'<rect x="{f(lx + level * column)}" y="{f(legend_y)}" width="{f(cell)}" '
                   f'height="{f(cell)}" rx="2.5" class="l{level}"/>')
    legend += card.text(lx + 5 * column + 6, legend_y + 9, "More", kind="mono",
                        weight="400", size=9, fill=t["text_muted"])
    summary = (f'{stats["total"]:,} contributions · {stats["active_days"]} active days · '
               f'{stats["current_streak"]}-day streak')
    legend += card.text(card.w - 44, legend_y + 9, summary, kind="mono", weight="500", size=9.5,
                        fill=t["text_secondary"], anchor="end", tabular=True)
    card.add(card.enter(legend, 1.1, kind="in"))
    return card


# ─────────────────────────────────────────────────────────── contribution data

def load_data(path: Path, handle: str) -> dict:
    """Read the JSON the daily Action refreshes, then derive months, weeks and stats."""
    if not path.exists():
        sys.exit(f"missing contribution data: {path}\n"
                 f"Run: python scripts/fetch_contributions.py --demo   (or set GH_PAT and drop --demo)")
    raw = json.loads(path.read_text())
    days = sorted(raw.get("days", []), key=lambda d: d["date"])
    if not days:
        sys.exit(f"{path} contains no days")

    # Levels: GitHub's own if the feed carried them, otherwise quartiles over the
    # non-zero days. Quartiles, not a share of the peak — one 70-commit day would
    # otherwise flatten an entire year of ones and twos into the palest shade.
    busy = sorted(int(d.get("count", 0)) for d in days if int(d.get("count", 0)) > 0)

    def quartile(count: int) -> int:
        if count <= 0 or not busy:
            return 0
        cuts = [busy[int(len(busy) * q)] for q in (0.25, 0.5, 0.75)]
        return 1 + sum(1 for cut in cuts if count > cut)

    for day in days:
        day["count"] = int(day.get("count", 0))
        level = day.get("level")
        if day["count"] == 0:
            day["level"] = 0
        elif level is None or level == 0:
            day["level"] = quartile(day["count"])
        else:
            day["level"] = int(level)

    # Months, oldest to newest, last 12 present.
    buckets: dict[str, int] = {}
    for day in days:
        buckets[day["date"][:7]] = buckets.get(day["date"][:7], 0) + day["count"]
    keys = sorted(buckets)[-12:]
    months = [{"key": k,
               "label": datetime.strptime(k, "%Y-%m").strftime("%b"),
               "count": buckets[k]} for k in keys]

    # Weeks for the heatmap, Sunday-first columns like GitHub.
    first = datetime.strptime(days[0]["date"], "%Y-%m-%d").date()
    by_date = {d["date"]: d for d in days}
    start = first - timedelta(days=(first.weekday() + 1) % 7)
    last = datetime.strptime(days[-1]["date"], "%Y-%m-%d").date()
    weeks, cursor = [], start
    while cursor <= last:
        week = []
        for _ in range(7):
            week.append(by_date.get(cursor.isoformat()) if cursor >= first and cursor <= last else None)
            cursor += timedelta(days=1)
        weeks.append(week)
    weeks = weeks[-53:]

    # Streaks: current counts back from the most recent day, today's zero not breaking it.
    longest = run = 0
    for day in days:
        run = run + 1 if day["count"] else 0
        longest = max(longest, run)
    current = 0
    for day in reversed(days):
        if day["count"]:
            current += 1
        elif current or day is days[-1]:
            break
    busiest = max(days, key=lambda d: d["count"])
    best_month = max(months, key=lambda m: m["count"]) if months else {"label": "—", "count": 0}
    busiest_dt = datetime.strptime(busiest["date"], "%Y-%m-%d")

    return {
        "days": days,
        "months": months,
        "weeks": weeks,
        "updated": raw.get("generated_at", datetime.now(timezone.utc).isoformat())[:10],
        "stats": {
            "total": raw.get("total") or sum(d["count"] for d in days),
            "current_streak": current,
            "longest_streak": longest,
            "active_days": sum(1 for d in days if d["count"]),
            "total_days": len(days),
            "busiest_count": busiest["count"],
            "busiest_date_short": busiest_dt.strftime("%d %b"),
            "busiest_label": f'{busiest_dt.strftime("%d %b %Y")} ({busiest["count"]})',
            "best_month": f'{best_month["label"]} ({best_month["count"]})',
        },
    }


def overlay_repos(cfg: dict, path: Path) -> None:
    """Live stars/forks/language from data/repos.json win; hand-written blurbs stay."""
    if not path.exists():
        return
    live = json.loads(path.read_text()).get("repos", {})
    for repo in cfg["repos"]:
        fresh = live.get(repo["name"])
        if not fresh:
            continue
        for key in ("stars", "forks"):
            if fresh.get(key) is not None:
                repo[key] = fresh[key]
        if fresh.get("language"):
            repo["language"] = fresh["language"]


# ──────────────────────────────────────────────────────────────── preview page

def write_preview(out_dir: Path, cards: list[Card], cfg: dict, static: bool) -> None:
    t = cfg["theme"]
    tiles = [c for c in cards if c.name.startswith("tile-")]
    repos = [c for c in cards if c.name.startswith("repo-")]
    rows = [f'<img src="hero.svg" width="100%" alt="">']
    rows.append('<p class="row">' + "".join(
        f'<img src="{c.name}.svg" width="32.5%" alt="">' for c in tiles) + "</p>")
    rows.append('<img src="contributions.svg" width="100%" alt="">')
    repo_row = "".join(f'<img src="{c.name}.svg" width="49%" alt="">' for c in repos)
    rows.append(f'<p class="row wrap">{repo_row}</p>')
    rows.append('<img src="graph.svg" width="100%" alt="">')
    mode = "STATIC — final frame" if static else "ANIMATED — reload to replay"
    (out_dir / "preview.html").write_text(
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>Profile cards — {mode}</title><style>"
        f"body{{background:{t['bg']};margin:0;padding:40px 20px;"
        "font:13px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;color:#7a7a92}"
        ".wrapper{max-width:880px;margin:0 auto}"
        "img{display:block;margin:0 0 12px}"
        ".row{display:flex;gap:1.25%;margin:0 0 12px;flex-wrap:nowrap}"
        ".row.wrap{flex-wrap:wrap;gap:2%;row-gap:12px}"
        ".row img{margin:0}"
        f".badge{{color:{t['accent']};font-family:monospace;margin-bottom:18px;display:block}}"
        "</style></head><body><div class='wrapper'>"
        f"<span class='badge'>{mode} · 880px wide, same as GitHub's README column</span>"
        + "".join(rows) +
        "</div></body></html>", encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────── driver

def main() -> int:
    parser = argparse.ArgumentParser(description="Render the animated SVG card stack.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--data", default="data/contributions.json")
    parser.add_argument("--out", default="cards")
    parser.add_argument("--static", action="store_true",
                        help="render the final frame only (same as STATIC=1)")
    args = parser.parse_args()

    static = args.static or os.environ.get("STATIC", "") not in ("", "0", "false", "False")

    cfg = json.loads((ROOT / args.config).read_text())
    overlay_repos(cfg, ROOT / "data" / "repos.json")
    fonts = FontShop(cfg["fonts"], ROOT)
    data = load_data(ROOT / args.data, cfg["identity"]["handle"])
    ctx = Ctx(cfg, fonts, static, data)

    cards: list[Card] = [build_hero(ctx)]
    cards += [build_tile(ctx, tile, i) for i, tile in enumerate(cfg["tiles"])]
    cards.append(build_chart(ctx))
    cards += [build_repo(ctx, repo, i) for i, repo in enumerate(cfg["repos"])]
    cards.append(build_graph(ctx))

    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    for card in cards:
        svg = card.render()
        path = out_dir / f"{card.name}.svg"
        path.write_text(svg, encoding="utf-8")
        total += len(svg.encode("utf-8"))
        print(f"  {path.relative_to(ROOT)}  {card.w}×{card.h}  {len(svg) / 1024:6.1f} KB")

    write_preview(out_dir, cards, cfg, static)
    print(f"\n{len(cards)} cards · {total / 1024:.0f} KB total · "
          f"{'STATIC (final frame)' if static else 'ANIMATED'}")
    print(f"preview: {(out_dir / 'preview.html').relative_to(ROOT)}")
    if fonts.missing:
        print(f"\nwarning: characters absent from the font subsets and they will not "
              f"render: {''.join(sorted(fonts.missing))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
