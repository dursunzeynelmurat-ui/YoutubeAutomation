#!/usr/bin/env python3
"""longform.py — long-form 16:9 horror/creepypasta "audiobook with visuals" (Phase 1).

Full faithful narration (NO splitting) over an AI-image Ken Burns slideshow + subtitles
+ ducked horror music, with a title-card intro and film grain. Local + sequential VRAM.

    narration -> whisper timings -> scene split -> LLM image prompts -> SDXL-Turbo stills
    -> Ken Burns clips -> concat -> subtitles + grain + music duck + -14 LUFS -> 1080p mp4

Usage:
    python pipeline/longform.py --script content/scripts/<saved nosleep>.md --config config.reddit.yaml
    python pipeline/longform.py --id <postid>  --config config.reddit.yaml
    python pipeline/longform.py --auto          --config config.reddit.yaml
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import requests
import soundfile as sf
from PIL import Image, ImageDraw, ImageFont, ImageFilter

import brainrot as br
import generate as gen
import imagegen
import reddit_fetch as rf
import speak as spk
from _common import get_path, load_config, resolve, setup_logging

log = setup_logging()


# --------------------------------------------------------------------------- #
def _parse_script(md_text: str) -> tuple[str, str]:
    title = ""
    m = re.search(r"<!--\s*topic:\s*(.*?)\s*\|\s*model:", md_text, re.DOTALL)
    if m:
        tm = re.match(r"Title:\s*(.*?)(?:\n|$)", m.group(1))
        title = (tm.group(1) if tm else m.group(1)).strip()
    body = md_text.split("-->", 1)[-1].strip() if "-->" in md_text else md_text.strip()
    return title, body


def get_story(config, args) -> tuple[str, str, dict]:
    """Return (title, narration_body, story_meta)."""
    if args.script:
        title, body = _parse_script(Path(resolve(args.script)).read_text(encoding="utf-8"))
        return title, body, {"subreddit": args.subreddit or "nosleep", "id": Path(args.script).stem}
    if args.id:
        rec = get_path(config, "stories") / f"{args.id}.json"
        if not rec.exists():
            sys.exit(f"[fatal] no local record {rec}. Use --auto or --script.")
        d = json.loads(rec.read_text(encoding="utf-8"))
    else:  # --auto
        d = rf.select_story(config, rf.fetch_candidates(config))
        if not d:
            sys.exit("[fatal] no story fetched/selected.")
    _, body = gen.produce_script(config, f"Title: {d['title']}\n\n{d['selftext']}", fmt="story")
    return d["title"], body, d


def split_into_n(text: str, n: int) -> list[str]:
    sents = [s for s in re.split(r"(?<=[.!?…])\s+", text.replace("\n", " ")) if s.strip()]
    if n <= 1 or len(sents) <= 1:
        return [text.strip()]
    target = math.ceil(len(sents) / n)
    groups = [" ".join(sents[i:i + target]) for i in range(0, len(sents), target)]
    return [g for g in groups if g.strip()]


def scene_prompts(config, title: str, scenes: list[str]) -> list[str]:
    """One LLM call -> a visual image prompt per scene (spoiler-safe, no on-image text)."""
    llm = config["llm"]
    numbered = "\n".join(f"[{i}] {s[:700]}" for i, s in enumerate(scenes))
    system = (
        "You write IMAGE-GENERATION prompts for a dark horror video — exactly one per numbered scene. "
        "CRITICAL: each prompt MUST be GROUNDED in the LITERAL setting, location, objects and subject "
        "described in THAT scene's own text. If the scene happens in a hospital, the image is a hospital "
        "room; a car, then a car interior; a basement, then a basement; a kitchen at night, then that "
        "kitchen. NEVER invent an unrelated location (no random forests/gardens if the text doesn't say "
        "so). Begin each prompt with the concrete place/subject drawn from the scene, then add lighting "
        "and mood. Keep the location consistent with the story's world. No readable text/words in the "
        "image, no explicit gore, spoiler-safe. "
        f'Return ONLY JSON of the form {{"prompts": ["<prompt 1>", "<prompt 2>", ...]}} with exactly '
        f"{len(scenes)} short strings, in scene order.")
    try:
        r = requests.post(f"{llm['ollama_host']}/api/chat", timeout=llm.get("request_timeout", 600),
                          json={"model": llm.get("seo_model", llm["fallback"]), "stream": False,
                                "format": "json", "keep_alive": llm.get("keep_alive", 0),
                                "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": f"Title: {title}\n\n{numbered}"}]})
        r.raise_for_status()
        raw = r.json()["message"]["content"]
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001 — salvage an array/object embedded in text
            m = re.search(r"\{.*\}", raw, re.DOTALL) or re.search(r"\[.*\]", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else None
        # normalize to a list of prompt strings (accept array, {"prompts":[...]}, or any list value)
        if isinstance(data, dict):
            lists = [v for v in data.values() if isinstance(v, list)]
            arr = lists[0] if lists else list(data.values())
        elif isinstance(data, list):
            arr = data
        else:
            arr = []
        out = [str(x).strip() for x in arr if str(x).strip()]
        if not out:
            raise ValueError("no prompts parsed")
        if len(out) >= len(scenes):
            return out[:len(scenes)]
        out += [scenes[i][:120] for i in range(len(out), len(scenes))]   # pad with scene text
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("scene-prompt LLM failed (%s) — using scene text.", exc)
        return [s[:120] for s in scenes]


_FONT_DIRS = ["C:/Windows/Fonts",                       # Windows
              "/System/Library/Fonts/Supplemental", "/Library/Fonts", "/System/Library/Fonts",  # macOS
              "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts"]  # Linux


def _font(size, bold=True):
    names = (["seguisb.ttf", "arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"] if bold
             else ["segoeui.ttf", "arial.ttf", "Arial.ttf", "DejaVuSans.ttf"])
    for d in _FONT_DIRS:
        for n in names:
            p = Path(d) / n
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def _draw_tracked(d, text, font, cx, y, fill, track=0):
    """Centered text with letter-spacing (tracking) for a cinematic kicker."""
    widths = [d.textlength(ch, font=font) for ch in text]
    x = cx - (sum(widths) + track * (len(text) - 1)) / 2
    for ch, wch in zip(text, widths):
        d.text((x, y), ch, font=font, fill=fill)
        x += wch + track


def render_title_card(title: str, subreddit: str, out: Path, w: int, h: int, bg: Path | None = None) -> Path:
    """Cinematic title over a darkened, blurred first-scene image (not a flat slide):
    tracked kicker + accent rule + soft-shadow title."""
    import textwrap
    if bg and Path(bg).exists():
        base = Image.open(bg).convert("RGB").resize((w, h)).filter(ImageFilter.GaussianBlur(22))
        base = Image.blend(base, Image.new("RGB", (w, h), (0, 0, 0)), 0.66)   # darken for legibility
    else:
        base = Image.new("RGB", (w, h), (8, 8, 10))
    d = ImageDraw.Draw(base)
    tf = _font(88, bold=True); kf = _font(26, bold=True)
    cx = w // 2
    ky = int(h * 0.30)
    _draw_tracked(d, f"R/{subreddit.upper()}", kf, cx, ky, (198, 198, 204), track=8)
    d.line([(cx - 150, ky + 46), (cx + 150, ky + 46)], fill=(140, 140, 146), width=2)
    probe = d.textlength("m", font=tf) or 44
    lines = textwrap.wrap(title, width=max(14, int(w * 0.78 / probe))) or [""]
    lh = tf.size + 16
    y = int(h * 0.42)
    for ln in lines:
        for dx, dy in ((-2, 3), (2, 3), (0, 4)):
            d.text((cx + dx, y + dy), ln, font=tf, fill=(0, 0, 0), anchor="ma")   # soft shadow
        d.text((cx, y), ln, font=tf, fill=(238, 238, 242), anchor="ma")
        y += lh
    out.parent.mkdir(parents=True, exist_ok=True)
    base.save(out)
    return out


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(words, offset: float, per_line: int = 9) -> str:
    lines, idx = [], 1
    for i in range(0, len(words), per_line):
        grp = words[i:i + per_line]
        st = grp[0][1] + offset; en = grp[-1][2] + offset
        txt = " ".join(w[0] for w in grp)
        lines.append(f"{idx}\n{_srt_time(st)} --> {_srt_time(en)}\n{txt}\n")
        idx += 1
    return "\n".join(lines)


def ken_burns_clip(img: Path, dur: float, out: Path, w: int, h: int, zoom_in: bool):
    frames = max(1, int(round(dur * 30)))
    # Motion is spread across the WHOLE clip (z is a function of the output frame `on`),
    # so it never reaches a cap early and freeze — it keeps moving until the last frame.
    amp = 0.18
    if zoom_in:
        z = f"1+{amp}*on/{frames}"
    else:
        z = f"{1 + amp}-{amp}*on/{frames}"
    vf = (f"scale={w*2}:{h*2},zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
          f"d={frames}:s={w}x{h}:fps=30,setsar=1")
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(img), "-t", f"{dur:.3f}", "-r", "30",
           "-vf", vf, "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(out)]
    if subprocess.run(cmd, capture_output=True).returncode != 0:
        raise RuntimeError(f"ken burns failed for {img.name}")


# High-tension words only (generic scene-setting words like "night"/"walk" removed so
# they don't pull the motion accent onto a calm intro).
_HERO_KW = ("scream", "screamed", "screaming", "blood", "dead", "died", "death", "running", "ran",
            "run", "crouch", "crept", "creeping", "toward", "fast", "frozen", "froze", "shadow",
            "figure", "staring", "stared", "smile", "smiling", "grin", "chase", "chasing", "behind",
            "footstep", "panic", "terrified", "horror", "shaking", "closer", "grabbed", "reached",
            "knock", "breathing", "whisper", "silhouette", "wide", "insane", "shocked")


def pick_hero_scenes(scenes: list[str], k: int) -> set[int]:
    """Choose the k tensest scenes (1-based) to animate, spread out (no adjacency).
    Tension = high-tension keyword hits + a bias toward later scenes (the climax)."""
    n = max(1, len(scenes))

    def score(i):
        kw = sum(scenes[i].lower().count(w) for w in _HERO_KW)
        return kw + 1.3 * (i / (n - 1) if n > 1 else 0)   # later scenes weighted up

    picked = []
    for i in sorted(range(len(scenes)), key=score, reverse=True):
        if len(picked) >= k:
            break
        if all(abs(i - p) > 1 for p in picked):
            picked.append(i)
    return {i + 1 for i in picked}          # 1-based to match the render loop


def hero_scene_clip(hero_raw: Path, dur: float, out: Path, w: int, h: int):
    """Loop a short SVD motion clip to fill the scene duration."""
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=30"
    cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(hero_raw), "-t", f"{dur:.3f}",
           "-vf", vf, "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(out)]
    if subprocess.run(cmd, capture_output=True).returncode != 0:
        raise RuntimeError("hero loop-fill failed")


def main() -> None:
    ap = argparse.ArgumentParser(description="Long-form 16:9 horror narration with AI visuals.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--auto", action="store_true", help="fetch + select a story")
    g.add_argument("--id", help="local content/stories/<id>.json")
    g.add_argument("--script", help="reuse a saved narration script (.md)")
    ap.add_argument("--subreddit", help="subreddit label for the title card (with --script)")
    ap.add_argument("--no-hero", action="store_true", help="skip SVD hero-shot motion (stills only)")
    ap.add_argument("--whisper-device", help="auto|cuda|cpu")
    ap.add_argument("--config", help="use config.reddit.yaml")
    args = ap.parse_args()
    config = load_config(args.config)
    lf = config.get("longform", {})
    w, h = lf.get("aspect", [1920, 1080])

    title, body, meta = get_story(config, args)
    sub = meta.get("subreddit", "nosleep")
    log.info("long-form: r/%s — %s  (%d words)", sub, title, len(body.split()))

    # voice (horror narrator) + sentence pauses
    vcfg = dict(config["tts"]["kokoro"])
    vcfg["voice"] = lf.get("voice", "am_michael"); vcfg["voice_locked"] = True
    vcfg["speed"] = lf.get("speed", 0.88)
    arr, sr = spk.ENGINES["kokoro"](spk.clean_for_tts(body), "en", vcfg, config["tts"].get("sample_rate", 24000))
    arr = np.asarray(arr, dtype=np.float32)
    audio_dir = get_path(config, "audio"); audio_dir.mkdir(parents=True, exist_ok=True)
    base = gen.slugify(title)[:40]
    audio = audio_dir / f"lf_{base}.wav"; sf.write(str(audio), arr, sr)
    dur = len(arr) / sr
    log.info("narration voiced: %.1f min", dur / 60)

    words = br.transcribe_words(audio, config.get("brainrot", {}).get("whisper_model", "small"),
                                args.whisper_device or "auto")
    if not words:
        sys.exit("[fatal] no words transcribed.")

    # scenes: one image per ~seconds_per_scene of narration
    n_scenes = max(1, math.ceil(dur / float(lf.get("seconds_per_scene", 20))))
    scenes = split_into_n(body, n_scenes)
    n_scenes = len(scenes)
    log.info("planning %d scene(s) (~%.0fs each)", n_scenes, dur / n_scenes)

    # time span per scene, from whisper word timings (by cumulative word count)
    counts = [len(s.split()) for s in scenes]
    spans, ci = [], 0
    for k, c in enumerate(counts):
        start = words[min(ci, len(words) - 1)][1]
        ci += c
        end = words[min(ci, len(words)) - 1][2] if ci <= len(words) else dur
        if k == n_scenes - 1:
            end = dur
        spans.append((max(0.0, start), max(start + 0.5, end)))

    prompts = scene_prompts(config, title, scenes)

    tmp = Path(tempfile.mkdtemp(prefix=f"longform_{base}_"))
    out_dir = get_path(config, "output") / "longform"; out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{base}.mp4"
    try:
        # 1) images
        imgs = imagegen.generate_images(prompts, tmp / "img", lf)

        # 1b) Phase 2 (D): sparse SVD motion accents at the tensest scenes (~1 per N minutes,
        #     capped); stills stay the base. Graceful fallback to a still if SVD fails/OOMs.
        if args.no_hero:
            n_hero = 0
        else:
            every = float(lf.get("hero_every_minutes", 3))
            n_hero = min(int(lf.get("hero_max", 10)), round((dur / 60) / every))
            if n_hero == 0 and dur >= 120:          # short story still gets one accent
                n_hero = 1
            n_hero = min(n_hero, len(scenes))
        hero_clips = {}
        if n_hero > 0:
            hero_list = sorted(pick_hero_scenes(scenes, n_hero))
            log.info("hero shots (SVD) for scene(s): %s", hero_list)
            # Regenerate the hero scenes' images motion-safe (wide, low-detail, no close-up
            # faces) so SVD animates them cleanly, then animate those instead of the detailed
            # slideshow stills.
            msafe = imagegen.generate_images([prompts[i - 1] for i in hero_list],
                                             tmp / "heroimg", lf, seed=1000, motion_safe=True)
            for idx, i in enumerate(hero_list):
                try:
                    hraw = tmp / f"hero_{i:03d}.mp4"
                    imagegen.animate_image(msafe[idx], hraw, lf)
                    hero_clips[i] = hraw
                except Exception as exc:  # noqa: BLE001
                    log.warning("hero animate failed for scene %d (%s) — using still.", i, exc)

        # 2) optional cinematic title-card intro + per-scene clips (motion for hero scenes)
        clips = []
        if lf.get("title_card", True):
            intro = float(lf.get("intro_seconds", 6.0))
            tcard = render_title_card(title, sub, tmp / "title.png", w, h,
                                      bg=(imgs[0] if imgs else None))
            tclip = tmp / "clip_000.mp4"
            ken_burns_clip(tcard, intro, tclip, w, h, zoom_in=True)
            clips.append(tclip)
        else:
            intro = 0.0        # no title card -> narration/scenes start at t=0
        for i, (img, (s0, s1)) in enumerate(zip(imgs, spans), 1):
            d = max(0.8, s1 - s0)
            cp = tmp / f"clip_{i:03d}.mp4"
            if i in hero_clips:
                hero_scene_clip(hero_clips[i], d, cp, w, h)
            else:
                ken_burns_clip(img, d, cp, w, h, zoom_in=(i % 2 == 1))
            clips.append(cp)
        listf = tmp / "concat.txt"
        listf.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips), encoding="utf-8")
        slideshow = tmp / "slideshow.mp4"
        if subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf),
                           "-c", "copy", str(slideshow)], capture_output=True).returncode != 0:
            # fallback: re-encode concat if stream-copy refuses
            subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf),
                            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(slideshow)], check=True)

        # 3) subtitles (shifted past the intro)
        srt = tmp / "subs.srt"
        srt.write_text(build_srt(words, offset=intro) if lf.get("subtitles", True) else "", encoding="utf-8")

        # 4) final mux: subtitles + grain/vignette + ducked music + loudnorm
        a = config.get("audio", {})
        music = None
        mdir = get_path(config, "music") / lf.get("music_mood", "horror")
        mus = [p for p in mdir.iterdir() if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg"}] if mdir.exists() else []
        import random as _r
        if mus:
            music = _r.choice(mus)

        intro_ms = int(intro * 1000)
        cmd = ["ffmpeg", "-y", "-i", str(slideshow), "-i", str(audio)]
        if music:
            cmd += ["-stream_loop", "-1", "-i", str(music)]
        vf = f"subtitles={br.escape_sub(srt)}:force_style='Fontsize=15,PrimaryColour=&H00FFFFFF,OutlineColour=&H90000000,BorderStyle=1,Outline=2,Shadow=0,MarginV=60,Alignment=2'" if lf.get("subtitles", True) else "null"
        if lf.get("grain", True):
            vf += f",noise=alls={int(lf.get('grain_strength', 5))}:allf=t,vignette=PI/5"
        af = [f"[1:a]adelay={intro_ms}|{intro_ms},aformat=channel_layouts=mono"]
        if music:
            af[0] += ",asplit=2[vmain][vside]"
            af.append(f"[2:a]aformat=channel_layouts=mono,volume={a.get('music_volume',0.12)}[m0]")
            af.append(f"[m0][vside]sidechaincompress=threshold={a.get('duck_threshold',0.03)}:ratio={a.get('duck_ratio',8)}:attack=5:release=350[m]")
            af.append(f"[vmain][m]amix=inputs=2:duration=first:normalize=0[amx]")
            cur = "[amx]"
        else:
            af[0] += "[amx]"; cur = "[amx]"
        af.append(f"{cur}loudnorm=I={a.get('loudness_i',-14)}:TP={a.get('loudness_tp',-1.5)}:LRA={a.get('loudness_lra',11)}[aout]")
        filt = f"[0:v]{vf}[vout];" + ";".join(af)
        cmd += ["-t", f"{intro + dur:.3f}", "-filter_complex", filt, "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-crf", str(lf.get("crf", 22)), "-pix_fmt", "yuv420p", "-r", "30",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)]
        log.info("compositing long-form (music=%s, %d scenes)…", "yes" if music else "no", n_scenes)
        if subprocess.run(cmd).returncode != 0:
            raise RuntimeError("final ffmpeg failed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # SEO sidecar (story profile)
    meta_seo = gen.produce_metadata(config, " ".join(w[0] for w in words), topic=title, language="en")
    meta_seo.update({"clip": base, "video": out.name, "subreddit": sub, "kind": "longform",
                     "duration_min": round((intro + dur) / 60, 1), "scenes": n_scenes})
    out.with_suffix(".json").write_text(json.dumps(meta_seo, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("DONE ✓ %s  (%.1f min, %d scenes)", out, (intro + dur) / 60, n_scenes)

    # Funnel: auto-cut punchy vertical promo Shorts from the tensest beats (ffmpeg, no GPU).
    n_promo = int(lf.get("promo_shorts", 0))
    if n_promo > 0:
        import promo
        promo_idx = sorted(pick_hero_scenes(scenes, min(n_promo, len(scenes))))
        try:
            promos = promo.make_promos(config, out, base, title, sub, intro, dur, spans, scenes,
                                       promo_idx, meta_seo.get("tags"))
            log.info("promo shorts produced: %d  -> output/shorts/", len(promos))
        except Exception as exc:  # noqa: BLE001
            log.warning("promo generation failed: %s", exc)


if __name__ == "__main__":
    main()
