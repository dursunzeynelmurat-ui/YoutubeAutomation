#!/usr/bin/env python3
"""thumb.py — generate a 1280x720 thumbnail for a clip: a striking frame from the video
+ a bold title overlay (+ optional series tag). Pure ffmpeg + PIL, no GPU.

    python pipeline/thumb.py --input <clip name> --config config.reddit.yaml
    python pipeline/thumb.py --file path/to.mp4 --title "..." --tag "THE DEAD HOUR"
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw

import reddit_card as rc
from _common import get_path, load_config, setup_logging

log = setup_logging()


def _find_video(config, name):
    for sub in ("longform", "shorts"):
        p = get_path(config, "output") / sub / f"{name}.mp4"
        if p.exists():
            return p
    return None


def make_thumbnail(video: Path, title: str, tag: str, out: Path) -> Path:
    try:
        dur = float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(video)]).decode().strip() or 60)
    except Exception:  # noqa: BLE001
        dur = 60.0
    tmp = Path(tempfile.mkdtemp(prefix="thumb_"))
    try:
        fr = tmp / "f.png"
        subprocess.run(["ffmpeg", "-y", "-ss", f"{dur * 0.42:.2f}", "-i", str(video),
                        "-frames:v", "1", str(fr)], capture_output=True)
        base = (Image.open(fr).convert("RGB").resize((1280, 720)) if fr.exists()
                else Image.new("RGB", (1280, 720), (10, 10, 12)))
        # bottom darkening gradient for legibility
        grad = Image.new("L", (1, 720), 0)
        for y in range(720):
            grad.putpixel((0, y), int(210 * max(0.0, (y - 320) / 400)))
        base = Image.composite(Image.new("RGB", (1280, 720), (0, 0, 0)), base, grad.resize((1280, 720)))
        d = ImageDraw.Draw(base)
        if tag:
            tf = rc._font(30, bold=True)
            w = int(d.textlength(tag, font=tf))
            d.rectangle([44, 40, 44 + w + 28, 40 + 46], fill=(198, 57, 43))
            d.text((58, 48), tag, font=tf, fill=(255, 255, 255))
        ttf = rc._font(84, bold=True)
        probe = d.textlength("m", font=ttf) or 46
        lines = textwrap.wrap(title, width=max(10, int(1180 / probe)))[:3]
        y = 690 - len(lines) * (ttf.size + 10)
        for ln in lines:
            for dx in (-3, 0, 3):
                for dy in (-3, 0, 3):
                    if dx or dy:
                        d.text((50 + dx, y + dy), ln, font=ttf, fill=(0, 0, 0))
            d.text((50, y), ln, font=ttf, fill=(233, 233, 237))
            y += ttf.size + 10
        out.parent.mkdir(parents=True, exist_ok=True)
        base.save(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a thumbnail for a clip.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--input", help="clip name (output/longform or output/shorts)")
    g.add_argument("--file", help="explicit video path")
    ap.add_argument("--title", help="override title text")
    ap.add_argument("--tag", help='series tag (default: "THE DEAD HOUR" for long-form, none for shorts)')
    ap.add_argument("--config")
    args = ap.parse_args()
    config = load_config(args.config)

    video = Path(args.file).resolve() if args.file else _find_video(config, Path(args.input).stem)
    if not video or not video.exists():
        sys.exit("[fatal] video not found.")
    meta = {}
    sc = video.with_suffix(".json")
    if sc.exists():
        meta = json.loads(sc.read_text(encoding="utf-8"))
    title = args.title or meta.get("og_title") or meta.get("title") or video.stem
    tag = args.tag if args.tag is not None else ("THE DEAD HOUR" if meta.get("kind") == "longform" else "")
    out = get_path(config, "output") / "thumbnails" / f"{video.stem}.png"
    make_thumbnail(video, title, tag, out)
    log.info("thumbnail -> %s", out)


if __name__ == "__main__":
    main()
