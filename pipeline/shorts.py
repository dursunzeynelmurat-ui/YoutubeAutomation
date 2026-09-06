#!/usr/bin/env python3
"""shorts.py — cut 9:16 captioned shorts from a long (Phase 3, ffmpeg + faster-whisper).

Transcribes the clip's audio locally (faster-whisper), picks a few windows, and for
each: cuts it from the long, reframes 16:9 -> 9:16 (center crop), and burns captions.
Outputs output/shorts/<name>_short01.mp4 ...

Usage:
    python pipeline/shorts.py --input 2026-09-06_your-topic-here
    python pipeline/shorts.py --input <name> --count 3 --max-duration 45
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from _common import get_path, load_config, setup_logging

log = setup_logging()


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def transcribe(audio: Path, model_size: str, device_pref: str):
    """Return a list of {start, end, text} segments using faster-whisper (local)."""
    from faster_whisper import WhisperModel

    def make(dev, ct):
        log.info("loading faster-whisper '%s' on %s (%s)…", model_size, dev, ct)
        return WhisperModel(model_size, device=dev, compute_type=ct)

    model = None
    if device_pref in ("auto", "cuda"):
        try:
            import torch
            if torch.cuda.is_available():
                model = make("cuda", "float16")
        except Exception as exc:  # noqa: BLE001 — ctranslate2 may not support this GPU
            log.warning("CUDA whisper unavailable (%s) — using CPU.", exc)
    if model is None:
        model = make("cpu", "int8")

    segments, _ = model.transcribe(str(audio), beam_size=5)
    out = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments]
    log.info("transcribed %d segments.", len(out))
    return out


def choose_windows(segments, count, max_dur):
    """Greedily group consecutive segments into <=max_dur windows, then spread `count`."""
    windows, cur = [], None
    for seg in segments:
        if cur and (seg["end"] - cur["start"]) <= max_dur:
            cur["end"] = seg["end"]
            cur["segs"].append(seg)
        else:
            if cur:
                windows.append(cur)
            cur = {"start": seg["start"], "end": seg["end"], "segs": [seg]}
    if cur:
        windows.append(cur)
    if not windows:
        return []
    if len(windows) <= count:
        return windows
    step = len(windows) / count            # evenly spread selection across the video
    return [windows[int(i * step)] for i in range(count)]


def write_srt(window, path: Path):
    lines = []
    for i, seg in enumerate(window["segs"], 1):
        st = max(0.0, seg["start"] - window["start"])   # times relative to clip start
        en = max(st, seg["end"] - window["start"])
        lines += [str(i), f"{srt_time(st)} --> {srt_time(en)}", seg["text"], ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def ffmpeg_escape_sub(path: Path) -> str:
    """Escape a Windows path for the ffmpeg subtitles filter."""
    p = str(path).replace("\\", "/").replace(":", "\\:")
    return f"'{p}'"


def main() -> None:
    ap = argparse.ArgumentParser(description="Cut captioned 9:16 shorts from a long.")
    ap.add_argument("--input", required=True, help="clip base name (wav stem)")
    ap.add_argument("--count", type=int, help="override shorts.count")
    ap.add_argument("--max-duration", type=int, help="override shorts.max_duration (s)")
    ap.add_argument("--long", help="path to the long mp4 (default output/longs/<name>.mp4)")
    ap.add_argument("--config", help="path to config.yaml")
    args = ap.parse_args()

    config = load_config(args.config)
    s = config["shorts"]
    name = Path(args.input).stem
    count = args.count or s.get("count", 3)
    max_dur = args.max_duration or s.get("max_duration", 60)
    tw, th = s.get("aspect", [1080, 1920])

    long_mp4 = Path(args.long).resolve() if args.long else (get_path(config, "output") / "longs" / f"{name}.mp4")
    audio = get_path(config, "audio") / f"{name}.wav"
    if not long_mp4.exists():
        sys.exit(f"[fatal] long video not found: {long_mp4} — run composite.py first.")
    if not audio.exists():
        sys.exit(f"[fatal] audio not found: {audio}")

    segments = transcribe(audio, s.get("whisper_model", "small"), s.get("whisper_device", "auto"))
    windows = choose_windows(segments, count, max_dur)
    if not windows:
        sys.exit("[fatal] no caption segments — cannot cut shorts.")
    log.info("cutting %d short(s).", len(windows))

    out_dir = get_path(config, "output") / "shorts"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="shorts_"))

    for i, win in enumerate(windows, 1):
        dur = win["end"] - win["start"]
        srt = tmp / f"cap{i}.srt"
        write_srt(win, srt)
        out = out_dir / f"{name}_short{i:02d}.mp4"
        # center-crop 16:9 -> 9:16, scale to target, then burn captions
        vf = (f"crop=ih*{tw}/{th}:ih,scale={tw}:{th},setsar=1,"
              f"subtitles={ffmpeg_escape_sub(srt)}:force_style='Alignment=2,FontSize=18,"
              f"BorderStyle=3,Outline=2,MarginV=60'")
        cmd = ["ffmpeg", "-y", "-ss", f"{win['start']:.3f}", "-t", f"{dur:.3f}",
               "-i", str(long_mp4), "-vf", vf,
               "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "160k", str(out)]
        log.info("short %d/%d  [%.1fs-%.1fs]  -> %s", i, len(windows), win["start"], win["end"], out.name)
        if subprocess.run(cmd).returncode != 0:
            log.error("ffmpeg failed on short %d — skipping.", i)
            continue

    log.info("shorts written to %s", out_dir)


if __name__ == "__main__":
    main()
