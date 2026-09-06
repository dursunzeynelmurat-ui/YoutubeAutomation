#!/usr/bin/env python3
"""composite.py — presenter over plate -> finished long video (Phase 3, ffmpeg).

Lays the rendered presenter PNG sequence (with alpha, from renders/presenter/<name>/)
over the baked static background plate (renders/plate.png), muxes the approved voice
track (audio/<name>.wav), optionally overlays graphics PNGs, and encodes a 16:9 long
to output/longs/<name>.mp4.

Usage:
    python pipeline/composite.py --input 2026-09-06_your-topic-here
    python pipeline/composite.py --input <name> --graphics lower_third.png --graphics chart.png
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from _common import get_path, load_config, resolve, setup_logging

log = setup_logging()


def detect_sequence(dir_: Path) -> tuple[str, int, int]:
    """Find the presenter PNG pattern in dir_. Returns (printf_pattern, start, count).

    UE MRQ writes frames like '<SequenceName>.0000.png'. We detect the shared
    prefix + zero-padded frame field and build an ffmpeg-friendly pattern.
    """
    pngs = sorted(dir_.glob("*.png"))
    if not pngs:
        sys.exit(f"[fatal] no presenter frames in {dir_} — run render.py --input first.")
    m = re.match(r"^(?P<prefix>.*?)(?P<num>\d+)$", pngs[0].stem)
    if not m:
        sys.exit(f"[fatal] cannot parse frame numbering from {pngs[0].name}")
    prefix, width = m.group("prefix"), len(m.group("num"))
    start = int(m.group("num"))
    pattern = f"{prefix}%0{width}d.png"
    return pattern, start, len(pngs)


def main() -> None:
    ap = argparse.ArgumentParser(description="Composite presenter over plate into a long video.")
    ap.add_argument("--input", required=True, help="clip base name (wav stem)")
    ap.add_argument("--graphics", action="append", default=[],
                    help="optional overlay PNG (repeatable); path rel. to automation/ or absolute")
    ap.add_argument("--output", help="output .mp4 path (default output/longs/<name>.mp4)")
    ap.add_argument("--config", help="path to config.yaml")
    args = ap.parse_args()

    config = load_config(args.config)
    c = config["composite"]
    name = Path(args.input).stem
    w, h = config["render"]["resolution"]
    fps = config["unreal"].get("render_fps", 30)

    transparent = bool(config["render"].get("transparent_bg", False))
    presenter_dir = get_path(config, "renders") / "presenter" / name
    audio = get_path(config, "audio") / f"{name}.wav"
    if not audio.exists():
        sys.exit(f"[fatal] audio not found: {audio}")

    pattern, start, nframes = detect_sequence(presenter_dir)

    out = Path(args.output).resolve() if args.output else (get_path(config, "output") / "longs" / f"{name}.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["ffmpeg", "-y"]
    graphics = [resolve(g) for g in args.graphics]
    for g in graphics:
        if not g.exists():
            sys.exit(f"[fatal] graphics overlay not found: {g}")

    if transparent:
        # presenter (alpha) OVER the baked plate: [0]=plate, [1]=presenter, [2]=audio
        plate = resolve(c["background"])
        if not plate.exists():
            sys.exit(f"[fatal] plate not found: {plate} (run render.py --plate)")
        log.info("compositing (transparent): %d presenter frames over %s + %s",
                 nframes, plate.name, audio.name)
        cmd += ["-loop", "1", "-framerate", str(fps), "-i", str(plate),
                "-framerate", str(fps), "-start_number", str(start), "-i", str(presenter_dir / pattern),
                "-i", str(audio)]
        fc = [f"[0:v]scale={w}:{h},setsar=1[bg]", "[bg][1:v]overlay=0:0:shortest=1[v0]"]
        gfx_base = 3
    else:
        # scene mode: presenter frames ARE the video. [0]=presenter, [1]=audio
        log.info("compositing (scene): %d presenter frames + %s", nframes, audio.name)
        cmd += ["-framerate", str(fps), "-start_number", str(start), "-i", str(presenter_dir / pattern),
                "-i", str(audio)]
        fc = [f"[0:v]scale={w}:{h},setsar=1[v0]"]
        gfx_base = 2

    for g in graphics:
        cmd += ["-loop", "1", "-framerate", str(fps), "-i", str(g)]

    last = "v0"
    for i, _ in enumerate(graphics):
        fc.append(f"[{last}][{gfx_base + i}:v]overlay=0:0[v{i+1}]")
        last = f"v{i+1}"
    fc.append(f"[{last}]format={c.get('pixel_format','yuv420p')}[vout]")
    audio_idx = 2 if transparent else 1

    cmd += ["-filter_complex", ";".join(fc),
            "-map", "[vout]", "-map", f"{audio_idx}:a",
            "-c:v", c.get("video_codec", "libx264"), "-crf", str(c.get("crf", 18)),
            "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)]

    log.info("compositing -> %s", out)
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        sys.exit(f"[fatal] ffmpeg failed (exit {rc}).")
    log.info("done: %s (%.1f MB)", out, out.stat().st_size / 1e6)


if __name__ == "__main__":
    main()
