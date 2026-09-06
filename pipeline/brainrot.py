#!/usr/bin/env python3
"""brainrot.py — gameplay-background short-form video (no Unreal render).

Takes an approved script that's already been voiced (audio/<name>.wav via
speak.py), transcribes it word-by-word (faster-whisper), and composites the voice
over a looping gameplay clip cropped to 9:16 with big karaoke-style captions.

This is the FAST, high-volume path — pure ffmpeg + whisper, no GPU render.

Pipeline:
    generate.py --format short  ->  approve  ->  speak.py  ->  brainrot.py

Usage:
    python pipeline/brainrot.py --input <clip>
    python pipeline/brainrot.py --input <clip> --gameplay content/gameplay/minecraft.mp4
"""
from __future__ import annotations

import argparse
import random
import subprocess
import sys
import tempfile
from pathlib import Path

from _common import get_path, load_config, resolve, setup_logging

log = setup_logging()

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}


def ass_time(t: float) -> str:
    cs = int(round(t * 100)); h, cs = divmod(cs, 360000); m, cs = divmod(cs, 6000); s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def transcribe_words(audio: Path, model_size: str, device_pref: str):
    from faster_whisper import WhisperModel

    def make(dev, ct):
        log.info("faster-whisper '%s' on %s (%s)…", model_size, dev, ct)
        return WhisperModel(model_size, device=dev, compute_type=ct)

    model = None
    if device_pref in ("auto", "cuda"):
        try:
            import torch
            if torch.cuda.is_available():
                model = make("cuda", "float16")
        except Exception as exc:  # noqa: BLE001
            log.warning("CUDA whisper unavailable (%s) — CPU.", exc)
    if model is None:
        model = make("cpu", "int8")

    segments, _ = model.transcribe(str(audio), word_timestamps=True, beam_size=5)
    words = []
    for seg in segments:
        for w in (seg.words or []):
            tok = w.word.strip()
            if tok:
                words.append((tok, float(w.start), float(w.end)))
    log.info("transcribed %d words.", len(words))
    return words


def _ass_color(v: str) -> str:
    """Config &HAABBGGRR -> inline \\c&HBBGGRR& (drop alpha)."""
    hexpart = v.replace("&H", "").replace("&", "")
    return "&H" + hexpart[-6:] + "&"


def build_ass(words, cfg, w, h) -> str:
    per = max(1, int(cfg.get("words_per_caption", 3)))
    primary = _ass_color(cfg.get("primary_color", "&H00FFFFFF"))
    highlight = _ass_color(cfg.get("highlight_color", "&H0000FFFF"))
    style = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: BR,{cfg.get('font','Arial')},{cfg.get('font_size',90)},"
        f"{cfg.get('primary_color','&H00FFFFFF')},{cfg.get('outline_color','&H00000000')},"
        f"&H64000000,-1,0,1,5,2,2,60,60,{cfg.get('margin_v',600)},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    # quick scale "pop" at the start of each word for a lively, fun feel
    pop = "{\\fscx82\\fscy82\\t(0,90,\\fscx100\\fscy100)}"
    lines = []
    # Group words; within each group emit one event PER WORD so the active word is
    # highlighted (karaoke) while the rest of the group stays visible.
    for i in range(0, len(words), per):
        group = words[i:i + per]
        for wi, (_, ws, we) in enumerate(group):
            parts = []
            for j, (w2, _, _) in enumerate(group):
                wt = w2.upper().replace("\n", " ")
                parts.append(f"{{\\c{highlight}}}{wt}{{\\c{primary}}}" if j == wi else wt)
            text = pop + " ".join(parts)
            lines.append(f"Dialogue: 0,{ass_time(ws)},{ass_time(we)},BR,,0,0,0,,{text}")
    return style + "\n".join(lines) + "\n"


def escape_sub(path: Path) -> str:
    p = str(path).replace("\\", "/").replace(":", "\\:")
    return f"'{p}'"


def pick_gameplay(config, override):
    if override:
        p = resolve(override)
        if not p.exists():
            sys.exit(f"[fatal] gameplay clip not found: {p}")
        return p
    gdir = get_path(config, "gameplay")
    clips = [p for p in gdir.iterdir() if p.suffix.lower() in VIDEO_EXTS] if gdir.exists() else []
    if not clips:
        sys.exit(f"[fatal] no gameplay clips in {gdir} — add a .mp4/.mov/.mkv/.webm "
                 f"(see content/gameplay/README.md).")
    return random.choice(clips)


def oneshot_script_and_voice(config, topic, language):
    """One-command path: generate a short script (Ollama) + voice it (TTS).
    Returns the clip base name; writes audio/<name>.wav. Reuses generate + speak."""
    import generate as gen
    import speak as spk
    out_path, body = gen.produce_script(config, topic, fmt="short", language=language)
    name = out_path.stem
    # Voice the body only (disclaimer stays in the script file / goes in the description).
    text = spk.clean_for_tts(body)
    engine = config["tts"]["engine"]
    engine_cfg = dict(config["tts"].get(engine, {}))
    sr = config["tts"].get("sample_rate", 24000)
    lang = (language or config["content"]["language"]).lower()
    log.info("voicing short (engine=%s, lang=%s)…", engine, lang)
    import numpy as np, soundfile as sf
    audio_arr, sr = spk.ENGINES[engine](text, lang, engine_cfg, sr)
    audio_dir = get_path(config, "audio"); audio_dir.mkdir(parents=True, exist_ok=True)
    sf.write(str(audio_dir / f"{name}.wav"), np.asarray(audio_arr, dtype=np.float32), sr)
    log.info("voiced -> audio/%s.wav", name)
    return name


def make_short(config, args, topic=None, input_name=None):
    """Produce one 9:16 short (+ SEO sidecar). Returns the output Path.
    Provide `topic` (one-shot: generate+voice) OR `input_name` (existing wav)."""
    import json as _json
    import shutil
    import soundfile as sf
    import generate as gen

    br = config.get("brainrot", {})
    tw, th = br.get("aspect", [1080, 1920])
    name = oneshot_script_and_voice(config, topic, args.language) if topic else Path(input_name).stem

    audio = get_path(config, "audio") / f"{name}.wav"
    if not audio.exists():
        raise FileNotFoundError(f"voice not found: {audio} — run speak.py --input {name}.md")

    dur = sf.info(str(audio)).duration
    gameplay = pick_gameplay(config, args.gameplay)
    log.info("clip=%s  dur=%.1fs  gameplay=%s", name, dur, gameplay.name)

    dev = args.whisper_device or br.get("whisper_device", "auto")
    words = transcribe_words(audio, br.get("whisper_model", "small"), dev)
    if not words:
        raise RuntimeError("no words transcribed — cannot caption.")

    tmp = Path(tempfile.mkdtemp(prefix=f"brainrot_{name}_"))
    try:
        ass = tmp / "captions.ass"
        ass.write_text(build_ass(words, br, tw, th), encoding="utf-8")

        start = args.start
        if start is None:
            try:
                gdur = float(subprocess.check_output(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=nk=1:nw=1", str(gameplay)]).decode().strip())
                start = round(random.uniform(0, max(0, gdur - dur - 1)), 2) if gdur > dur + 1 else 0.0
            except Exception:
                start = 0.0
        log.info("gameplay start offset: %.1fs", start)

        out = (Path(args.output).resolve() if args.output
               else get_path(config, "output") / "shorts" / f"{name}.mp4")
        out.parent.mkdir(parents=True, exist_ok=True)

        vf = (f"scale={tw}:{th}:force_original_aspect_ratio=increase,"
              f"crop={tw}:{th},setsar=1,subtitles={escape_sub(ass)}")
        cmd = ["ffmpeg", "-y", "-stream_loop", "-1"]
        if start and start > 0:
            cmd += ["-ss", f"{start:.2f}"]
        cmd += ["-i", str(gameplay), "-i", str(audio), "-t", f"{dur:.3f}", "-vf", vf,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-r", "30", str(out)]
        log.info("compositing short -> %s", out)
        if subprocess.run(cmd).returncode != 0:
            raise RuntimeError("ffmpeg failed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    log.info("done: %s (%.1f MB, %dx%d)", out, out.stat().st_size / 1e6, tw, th)

    transcript = " ".join(w[0] for w in words)
    meta = gen.produce_metadata(config, transcript, topic=(topic or name), language=args.language)
    meta.update({"clip": name, "video": out.name, "voice_engine": config["tts"]["engine"]})
    out.with_suffix(".json").write_text(_json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    out.with_suffix(".txt").write_text(
        f"TITLE:\n{meta['title']}\n\nDESCRIPTION:\n{meta['description']}\n\n"
        f"TAGS:\n{', '.join(meta['tags'])}\n", encoding="utf-8")
    log.info("SEO title: %s", meta["title"])
    return out


def upload_short(name: str):
    """Upload a produced short via upload.py (PRIVATE — never public here)."""
    up = Path(__file__).resolve().parent / "upload.py"
    log.info("uploading '%s' (private)…", name)
    rc = subprocess.run([sys.executable, str(up), "--input", name]).returncode
    if rc != 0:
        log.error("upload failed for '%s' (rc=%d) — the short is still saved locally.", name, rc)
    return rc == 0


def read_topics(path: Path) -> list[str]:
    """One topic per line; blank lines and #comments ignored."""
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Make gameplay-background 9:16 shorts.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--input", help="existing clip base name (needs audio/<name>.wav from speak.py)")
    g.add_argument("--topic", help="one-shot: generate script + voice + short from this topic")
    g.add_argument("--topics", help="BATCH: a text file of topics (one per line) -> many shorts")
    ap.add_argument("--language", help="override content.language (en|tr)")
    ap.add_argument("--gameplay", help="specific gameplay clip (else random per short)")
    ap.add_argument("--start", type=float, default=None, help="gameplay start seconds (else random)")
    ap.add_argument("--whisper-device", help="override brainrot.whisper_device (auto|cuda|cpu)")
    ap.add_argument("--output", help="output .mp4 (single-clip only; ignored in batch)")
    ap.add_argument("--upload", action="store_true", help="upload each finished short to YouTube (PRIVATE)")
    ap.add_argument("--config", help="path to config.yaml")
    args = ap.parse_args()
    config = load_config(args.config)

    if args.topics:
        topics = read_topics(resolve(args.topics))
        if not topics:
            sys.exit(f"[fatal] no topics found in {args.topics}")
        args.output = None  # never force one output path across a batch
        log.info("BATCH: %d topics", len(topics))
        made, failed = [], []
        for i, t in enumerate(topics, 1):
            log.info("──── [%d/%d] %s ────", i, len(topics), t)
            try:
                out = make_short(config, args, topic=t)
                if args.upload:
                    upload_short(out.stem)
                made.append(out)
            except Exception as exc:  # noqa: BLE001 — one bad topic shouldn't stop the batch
                log.error("topic failed (%s): %s", t, exc)
                failed.append(t)
        log.info("")
        log.info("BATCH DONE: %d made, %d failed", len(made), len(failed))
        for p in made:
            log.info("  ✓ %s", Path(p).name)
        for t in failed:
            log.info("  ✗ %s", t)
        log.info("upload them:  for each -> python pipeline\\upload.py --input <clip>")
    elif args.topic:
        out = make_short(config, args, topic=args.topic)
        if args.upload:
            upload_short(out.stem)
    else:
        out = make_short(config, args, input_name=args.input)
        if args.upload:
            upload_short(out.stem)


if __name__ == "__main__":
    main()
