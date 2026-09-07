#!/usr/bin/env python3
"""compile_weekly.py — stitch recent shorts into one long-form compilation.

Grabs the shorts produced in the last N days from output/shorts/, keeps multi-part
stories together and in order, and concatenates them into a single vertical video
(output/compilations/weekly_<date>.mp4) with a matching SEO sidecar. Long-form watch
time on top of the individual Shorts, at no extra generation cost.

Usage:
    python pipeline/compile_weekly.py --config config.reddit.yaml
    python pipeline/compile_weekly.py --days 7 --max 12 --config config.reddit.yaml
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from _common import get_path, load_config, setup_logging

log = setup_logging()


def _part_key(p: Path):
    """Sort so a story's parts stay together and in order: (base, part_number)."""
    m = re.match(r"^(.*?)(?:_p(\d+))?$", p.stem)
    base = m.group(1) if m else p.stem
    part = int(m.group(2)) if (m and m.group(2)) else 0
    return (base, part)


def _is_channel_clip(mp4: Path, story_channel: bool) -> bool:
    """Keep only THIS channel's clips (finance + reddit share output/shorts/).
    Story channel = sidecar has a reddit_id; finance channel = it doesn't."""
    sc = mp4.with_suffix(".json")
    has_rid = False
    if sc.exists():
        try:
            has_rid = bool(json.loads(sc.read_text(encoding="utf-8")).get("reddit_id"))
        except Exception:  # noqa: BLE001
            pass
    return has_rid if story_channel else not has_rid


def gather(config, days: int, max_clips: int) -> list[Path]:
    shorts = get_path(config, "output") / "shorts"
    if not shorts.exists():
        return []
    story_channel = config.get("content", {}).get("seo_profile") == "story"
    cutoff = time.time() - days * 86400
    clips = [p for p in shorts.glob("*.mp4")
             if p.stat().st_mtime >= cutoff and _is_channel_clip(p, story_channel)]
    clips.sort(key=_part_key)
    return clips[:max_clips] if max_clips else clips


def main() -> None:
    ap = argparse.ArgumentParser(description="Stitch recent shorts into one compilation.")
    ap.add_argument("--days", type=int, default=7, help="include shorts from the last N days")
    ap.add_argument("--max", type=int, default=0, help="cap number of clips (0 = all)")
    ap.add_argument("--output", help="output .mp4 (default output/compilations/weekly_<date>.mp4)")
    ap.add_argument("--config", help="path to config")
    args = ap.parse_args()
    config = load_config(args.config)

    clips = gather(config, args.days, args.max)
    if not clips:
        sys.exit(f"[fatal] no shorts in the last {args.days} days under output/shorts/.")
    log.info("compiling %d clip(s):", len(clips))
    for c in clips:
        log.info("  + %s", c.name)

    comp_dir = get_path(config, "output") / "compilations"
    comp_dir.mkdir(parents=True, exist_ok=True)
    out = Path(args.output).resolve() if args.output else comp_dir / f"weekly_{time.strftime('%Y-%m-%d')}.mp4"

    # concat via demuxer, re-encoding to uniform params (robust across clips)
    listfile = comp_dir / "_concat.txt"
    listfile.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips), encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
           "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)]
    log.info("stitching -> %s", out.name)
    rc = subprocess.run(cmd).returncode
    listfile.unlink(missing_ok=True)
    if rc != 0:
        sys.exit("[fatal] ffmpeg concat failed.")

    # simple SEO sidecar for the compilation
    n_stories = len({_part_key(c)[0] for c in clips})
    title = f"Reddit Stories Compilation — {n_stories} stories to binge"[:100]
    meta = {"title": title,
            "description": ("The best Reddit stories of the week, back to back. Grab a snack and binge.\n\n"
                            "Story adapted from public Reddit posts for entertainment.\n#reddit #redditstories #storytime"),
            "tags": ["reddit", "reddit stories", "compilation", "storytime", "aita", "nosleep"],
            "clips": [c.name for c in clips]}
    out.with_suffix(".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    dur = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "default=nk=1:nw=1", str(out)]).decode().strip()
    log.info("done ✓  %s  (%.1f min, %d stories)", out.name, float(dur) / 60, n_stories)


if __name__ == "__main__":
    main()
