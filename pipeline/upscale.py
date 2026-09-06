#!/usr/bin/env python3
"""upscale.py — AI super-resolution of a rendered clip to 4K (Phase 3).

Upscales the presenter PNG frames with Real-ESRGAN (realesrgan-ncnn-vulkan — a
Vulkan binary that runs on 8 GB without torch/CUDA conflicts), then reassembles
them with the voice track into a 4K long video.

Processes in bounded CHUNKS: upscale a batch to Nx, immediately downscale those
to the exact 4K target, delete the huge intermediate, move to the next batch —
so a long clip never needs hundreds of GB of temp space.

Usage:
    python pipeline/upscale.py --input <clip>
    python pipeline/upscale.py --input <clip> --chunk 200
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _common import get_path, load_config, resolve, setup_logging

log = setup_logging()


def frame_pattern(pngs):
    m = re.match(r"^(.*?)(\d+)$", pngs[0].stem)
    return m.group(1), len(m.group(2)), int(m.group(2))


def main() -> None:
    ap = argparse.ArgumentParser(description="Real-ESRGAN chunked upscale to 4K.")
    ap.add_argument("--input", required=True, help="clip base name (wav stem)")
    ap.add_argument("--chunk", type=int, default=200, help="frames per chunk (disk vs speed)")
    ap.add_argument("--output", help="output .mp4 (default output/longs/<name>_4k.mp4)")
    ap.add_argument("--config", help="path to config.yaml")
    args = ap.parse_args()

    config = load_config(args.config)
    up = config.get("upscale", {})
    if not up.get("enabled", False):
        sys.exit("[fatal] upscale.enabled is false in config.yaml")

    name = Path(args.input).stem
    fps = config["unreal"].get("render_fps", 30)
    tw, th = up.get("target", [3840, 2160])
    native_scale = int(up.get("native_scale", 4))
    model = up.get("model", "realesrgan-x4plus")
    exe = resolve(up["exe"])
    if not exe.exists():
        sys.exit(f"[fatal] Real-ESRGAN tool not found: {exe} (see docs/PHASE3_RENDER.md).")

    frames_dir = get_path(config, "renders") / "presenter" / name
    pngs = sorted(frames_dir.glob("*.png")) if frames_dir.exists() else []
    if not pngs:
        sys.exit(f"[fatal] no presenter frames in {frames_dir} — run render.py --input {name} first.")
    audio = get_path(config, "audio") / f"{name}.wav"
    if not audio.exists():
        sys.exit(f"[fatal] audio not found: {audio}")

    prefix, width, start = frame_pattern(pngs)
    tmp = Path(tempfile.mkdtemp(prefix=f"up_{name}_"))
    final_dir = tmp / "final4k"; final_dir.mkdir(parents=True, exist_ok=True)

    n = len(pngs)
    log.info("upscaling %d frames -> x%d -> %dx%d, in chunks of %d (slow; overnight ok)",
             n, native_scale, tw, th, args.chunk)
    try:
        for ci, i in enumerate(range(0, n, args.chunk)):
            batch = pngs[i:i + args.chunk]
            in_dir = tmp / "in"; out_dir = tmp / "out"
            for d in (in_dir, out_dir):
                shutil.rmtree(d, ignore_errors=True); d.mkdir(parents=True)
            for f in batch:
                shutil.copy2(f, in_dir / f.name)
            rc = subprocess.run([str(exe), "-i", str(in_dir), "-o", str(out_dir),
                                 "-n", model, "-s", str(native_scale), "-f", "png"],
                                capture_output=True, text=True).returncode
            if rc != 0:
                sys.exit(f"[fatal] Real-ESRGAN failed on chunk {ci} (exit {rc}).")
            # downscale each upscaled frame to the exact 4K target into final_dir
            for f in batch:
                uf = out_dir / f.name
                subprocess.run(["ffmpeg", "-y", "-i", str(uf),
                                "-vf", f"scale={tw}:{th}:flags=lanczos",
                                str(final_dir / f.name)], capture_output=True).returncode
            log.info("  chunk %d: frames %d-%d done", ci + 1, i, min(i + args.chunk, n) - 1)

        out = Path(args.output).resolve() if args.output else (get_path(config, "output") / "longs" / f"{name}_4k.mp4")
        out.parent.mkdir(parents=True, exist_ok=True)
        pat = f"{prefix}%0{width}d.png"
        pix = config["composite"].get("pixel_format", "yuv420p")
        log.info("encoding 4K + audio -> %s", out.name)
        enc = ["ffmpeg", "-y", "-framerate", str(fps), "-start_number", str(start),
               "-i", str(final_dir / pat), "-i", str(audio),
               "-vf", f"setsar=1,format={pix}",
               "-c:v", config["composite"].get("video_codec", "libx264"),
               "-crf", str(config["composite"].get("crf", 18)),
               "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)]
        if subprocess.run(enc).returncode != 0:
            sys.exit("[fatal] ffmpeg 4K encode failed.")
        log.info("done: %s (%.1f MB, %dx%d)", out, out.stat().st_size / 1e6, tw, th)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
