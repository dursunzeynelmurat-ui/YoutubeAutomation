#!/usr/bin/env python3
"""reddit_card.py — render a Reddit-style post card PNG for the video intro."""
from __future__ import annotations

import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


_FONT_DIRS = ["C:/Windows/Fonts",                       # Windows
              "/System/Library/Fonts/Supplemental", "/Library/Fonts", "/System/Library/Fonts",  # macOS
              "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts"]  # Linux


def _font(size, bold=False):
    names = (["seguisb.ttf", "arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"] if bold
             else ["segoeui.ttf", "arial.ttf", "Arial.ttf", "DejaVuSans.ttf"])
    for d in _FONT_DIRS:
        for n in names:
            p = Path(d) / n
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def render_card(post: dict, out_path: Path, width: int = 960) -> Path:
    """Draw a Reddit post card (subreddit, user, title, upvotes/comments) -> RGBA PNG."""
    pad = 40
    title_font = _font(46, bold=True)
    meta_font = _font(30)
    foot_font = _font(30, bold=True)

    # wrap the title to compute height
    draw_probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    avg = draw_probe.textlength("m", font=title_font) or 24
    wrap_cols = max(10, int((width - 2 * pad) / avg))
    lines = textwrap.wrap(post.get("title", ""), width=wrap_cols) or [""]
    line_h = title_font.size + 12
    # RSS feeds carry no score/comment counts; only draw the footer when we have them.
    has_footer = bool(post.get("score")) or bool(post.get("num_comments"))
    footer_h = (24 + 52) if has_footer else 0
    height = pad + 54 + 24 + len(lines) * line_h + footer_h + pad

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # card background (dark, rounded)
    d.rounded_rectangle([0, 0, width, height], radius=28, fill=(26, 26, 27, 245))

    # header: avatar dot + r/subreddit + u/author + time
    d.ellipse([pad, pad, pad + 44, pad + 44], fill=(255, 69, 0, 255))  # reddit orange
    sub = f"r/{post.get('subreddit','reddit')}"
    d.text((pad + 60, pad + 2), sub, font=_font(32, bold=True), fill=(255, 255, 255, 255))
    d.text((pad + 60, pad + 40), f"u/{post.get('author','user')} \u00b7 {post.get('age','5h')}",
           font=meta_font, fill=(160, 160, 162, 255))

    # title
    y = pad + 54 + 24
    for ln in lines:
        d.text((pad, y), ln, font=title_font, fill=(215, 218, 220, 255))
        y += line_h

    # footer: upvote score + comments (only when the source provided them)
    if has_footer:
        y += 20
        up = f"\u25b2 {_short(post.get('score', 0))}"
        cm = f"\U0001f4ac {_short(post.get('num_comments', 0))}"
        d.text((pad, y), up, font=foot_font, fill=(255, 69, 0, 255))
        d.text((pad + 220, y), cm, font=foot_font, fill=(160, 160, 162, 255))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def _short(n) -> str:
    n = int(n or 0)
    if n >= 1000:
        return f"{n/1000:.1f}k".replace(".0k", "k")
    return str(n)


def render_hook_card(text: str, out_path: Path, width: int = 1000) -> Path:
    """Big bold outlined hook text (no background) to burn on-screen for the first seconds."""
    font = _font(72, bold=True)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    avg = probe.textlength("m", font=font) or 40
    cols = max(8, int((width - 40) / avg))
    lines = textwrap.wrap(text.upper(), width=cols) or [""]
    line_h = font.size + 16
    h = 40 + len(lines) * line_h
    img = Image.new("RGBA", (width, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = 20
    for ln in lines:
        cx = width / 2
        # thick outline for readability over gameplay
        for dx in (-4, -2, 0, 2, 4):
            for dy in (-4, -2, 0, 2, 4):
                if dx or dy:
                    d.text((cx + dx, y + dy), ln, font=font, fill=(0, 0, 0, 255), anchor="ma")
        d.text((cx, y), ln, font=font, fill=(255, 235, 59, 255), anchor="ma")  # bright yellow
        y += line_h
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def render_progress_pill(text: str, out_path: Path) -> Path:
    """Small dark pill with white text, e.g. 'PART 1/4', for the top corner."""
    font = _font(34, bold=True)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    tw = int(probe.textlength(text, font=font))
    pad_x, h = 26, 60
    w = tw + pad_x * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w, h], radius=30, fill=(0, 0, 0, 190))
    d.text((w / 2, h / 2), text, font=font, fill=(255, 255, 255, 255), anchor="mm")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def _draw_bell(d: ImageDraw.ImageDraw, cx: int, cy: int, s: int, fill):
    """Draw a simple notification bell centered at (cx, cy), height ~s."""
    top = cy - s // 2
    # dome
    d.pieslice([cx - s * 0.42, top, cx + s * 0.42, top + s * 0.9], 180, 360, fill=fill)
    d.rectangle([cx - s * 0.42, top + s * 0.44, cx + s * 0.42, top + s * 0.72], fill=fill)
    # flared rim
    d.polygon([(cx - s * 0.5, top + s * 0.72), (cx + s * 0.5, top + s * 0.72),
               (cx + s * 0.4, top + s * 0.82), (cx - s * 0.4, top + s * 0.82)], fill=fill)
    # top nub
    d.ellipse([cx - s * 0.08, top - s * 0.08, cx + s * 0.08, top + s * 0.08], fill=fill)
    # clapper
    d.ellipse([cx - s * 0.12, top + s * 0.82, cx + s * 0.12, top + s * 0.98], fill=fill)


def render_cta_card(out_path: Path, text: str = "Turn on notifications",
                    button: str = "SUBSCRIBE", width: int = 900) -> Path:
    """Draw a 'SUBSCRIBE + bell' call-to-action banner (RGBA PNG) for the end of a video."""
    h = 150
    img = Image.new("RGBA", (width, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, width, h], radius=32, fill=(26, 26, 27, 235))

    btn_font = _font(40, bold=True)
    txt_font = _font(34, bold=True)
    pad = 28
    # red SUBSCRIBE pill
    bw = int(d.textlength(button, font=btn_font)) + 56
    bx0, by0, bx1, by1 = pad, (h - 78) // 2, pad + bw, (h - 78) // 2 + 78
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=39, fill=(255, 0, 0, 255))
    d.text(((bx0 + bx1) / 2, h / 2), button, font=btn_font, fill=(255, 255, 255, 255), anchor="mm")
    # bell + text
    bell_cx = bx1 + 60
    _draw_bell(d, bell_cx, int(h / 2), 64, (255, 255, 255, 255))
    d.text((bell_cx + 48, h / 2), text, font=txt_font, fill=(235, 235, 235, 255), anchor="lm")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path
