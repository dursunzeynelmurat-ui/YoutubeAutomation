#!/usr/bin/env python3
"""redditstory.py — Reddit-story shorts (2nd channel).

fetch/select (RSS) -> FAITHFUL narration -> safety check -> split into parts ->
mood-matched voice/pace/gameplay/music -> per-word captions (with emphasis) ->
on-screen hook + animated Reddit card + progress pill + Subscribe/bell CTA ->
music duck + intro/ding/outro SFX + -14 LUFS -> SEO + series links -> optional upload.

Usage (use the 2nd-channel profile):
    python pipeline/redditstory.py --auto  --config config.reddit.yaml
    python pipeline/redditstory.py --id <postid> --config config.reddit.yaml
    python pipeline/redditstory.py --auto --count 3 --upload --config config.reddit.yaml
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

import brainrot as br
import generate as gen
import reddit_fetch as rf
import reddit_card as rc
import speak as spk
from _common import get_path, load_config, resolve, rotate_pick, setup_logging

log = setup_logging()

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


def get_story(config, args) -> dict | None:
    if args.id:
        rec = get_path(config, "stories") / f"{args.id}.json"
        if rec.exists():
            return json.loads(rec.read_text(encoding="utf-8"))
        sys.exit(f"[fatal] no local story record for id '{args.id}'. "
                 f"Create one first with:  python pipeline/reddit_fetch.py --select --config {args.config}\n"
                 f"(or use --auto to fetch+select in one step). RSS has no fetch-by-id.")
    return rf.select_story(config, rf.fetch_candidates(config))


def split_parts(text: str, words_per_part: int) -> list[str]:
    import math
    sents = [s for s in re.split(r"(?<=[.!?…])\s+", text.replace("\n", " ")) if s.strip()]
    total = sum(len(s.split()) for s in sents)
    if total <= words_per_part or len(sents) <= 1:
        return [text.strip()]
    n = math.ceil(total / words_per_part)
    target = math.ceil(total / n)
    parts, cur, c = [], [], 0
    for s in sents:
        w = len(s.split())
        if cur and c + w > target and len(parts) < n - 1:
            parts.append(" ".join(cur)); cur, c = [s], w
        else:
            cur.append(s); c += w
    if cur:
        parts.append(" ".join(cur))
    return parts


def synth(config, text, voice_cfg):
    sr = config["tts"].get("sample_rate", 24000)
    arr, sr = spk.ENGINES["kokoro"](spk.clean_for_tts(text), "en", voice_cfg, sr)
    return np.asarray(arr, dtype=np.float32), sr


# --- mood + optional assets -------------------------------------------------
def _resolve_mood(config, subreddit: str) -> tuple[str, dict]:
    m = config.get("moods", {})
    bysub = {k.lower(): v for k, v in (m.get("by_subreddit") or {}).items()}
    mood = bysub.get(subreddit.lower(), m.get("default", "drama"))
    return mood, (m.get("profiles") or {}).get(mood, {})


def _pick_from(base: Path, mood: str | None, exts: set) -> list[Path]:
    for d in ([base / mood] if mood else []) + [base]:
        if d.exists():
            files = [p for p in d.iterdir() if p.suffix.lower() in exts]
            if files:
                return files
    return []


def _pick_music(config, mood=None) -> list[Path]:
    if "music" not in config.get("paths", {}):
        return []
    return _pick_from(get_path(config, "music"), mood, AUDIO_EXTS)


def _gameplay_pool(config, override, mood=None) -> list[Path]:
    if override:
        return [br.pick_gameplay(config, override)]
    return _pick_from(get_path(config, "gameplay"), mood, br.VIDEO_EXTS)


def _find_sfx(config) -> dict:
    out = {"intro": None, "whoosh": None, "ding": None, "outro": None}
    if "sfx" not in config.get("paths", {}):
        return out
    d = get_path(config, "sfx")
    if d.exists():
        for p in d.iterdir():
            if p.suffix.lower() in AUDIO_EXTS and p.stem.lower() in out:
                out[p.stem.lower()] = p
    return out


def _build_audio_filter(a, idx_voice, idx_music, sfx_list) -> list[str]:
    """voice (+ ducked music) (+ timed SFX) -> loudnorm -> [aout]. sfx_list = [(idx, delay_s)]."""
    parts = []
    if idx_music is not None:
        parts.append(f"[{idx_voice}:a]aformat=channel_layouts=mono,asplit=2[vmain][vside]")
        parts.append(f"[{idx_music}:a]aformat=channel_layouts=mono,volume={a.get('music_volume',0.12)}[mus0]")
        parts.append(f"[mus0][vside]sidechaincompress=threshold={a.get('duck_threshold',0.03)}:"
                     f"ratio={a.get('duck_ratio',8)}:attack=5:release=300[mus]")
        parts.append("[vmain][mus]amix=inputs=2:duration=first:normalize=0[amx]")
        cur = "[amx]"
    else:
        parts.append(f"[{idx_voice}:a]aformat=channel_layouts=mono[amx]")
        cur = "[amx]"
    sv, labels = a.get("sfx_volume", 0.5), []
    for k, (sidx, delay) in enumerate(sfx_list):
        dms = int(max(0.0, delay) * 1000)
        parts.append(f"[{sidx}:a]aformat=channel_layouts=mono,volume={sv},adelay={dms}|{dms}[sx{k}]")
        labels.append(f"[sx{k}]")
    if labels:
        parts.append(f"{cur}{''.join(labels)}amix=inputs={1 + len(labels)}:duration=first:normalize=0[asm]")
        cur = "[asm]"
    parts.append(f"{cur}loudnorm=I={a.get('loudness_i',-14)}:TP={a.get('loudness_tp',-1.5)}:"
                 f"LRA={a.get('loudness_lra',11)}[aout]")
    return parts


def make_story_videos(config, args, story: dict, body: str | None = None) -> list[Path]:
    r = config["reddit"]
    brc = config.get("brainrot", {})
    a = config.get("audio", {})
    tw, th = brc.get("aspect", [1080, 1920])
    words_per_part = int(r.get("max_seconds_per_part", 90) * r.get("wpm", 175) / 60)

    mood, prof = _resolve_mood(config, story["subreddit"])
    log.info("story r/%s [mood=%s]: %s", story["subreddit"], mood, story["title"])
    if body is None:
        _, body = gen.produce_script(config, f"Title: {story['title']}\n\n{story['selftext']}", fmt="story")
    else:
        log.info("using provided narration (%d words) — skipping generation", len(body.split()))

    mod = {"ok": True, "reasons": []}
    if r.get("moderate", True):
        mod = gen.moderate_story(config, body, language="en")
        if not mod["ok"]:
            log.warning("moderation flagged %s: %s", story["id"], "; ".join(mod["reasons"]) or "unspecified")
    if story["id"] in set(r.get("moderate_allow") or []):
        mod = {"ok": True, "reasons": (mod.get("reasons") or []) + ["manually approved by user"]}
        log.info("moderation override: %s manually approved by user", story["id"])

    parts = split_parts(body, words_per_part)
    n_parts = len(parts)
    log.info("script %d words -> %d part(s) (<=%d words each)", len(body.split()), n_parts, words_per_part)

    # one voice locked per story; mood profile can pin a voice / speed
    voices = config["tts"]["kokoro"].get("voices") or [config["tts"]["kokoro"].get("voice", "am_adam")]
    voice = prof["voice"] if prof.get("voice") else rotate_pick("kokoro_voice", list(voices))
    vcfg = dict(config["tts"]["kokoro"]); vcfg["voice"] = voice; vcfg["voice_locked"] = True
    if prof.get("speed"):
        vcfg["speed"] = prof["speed"]
    log.info("voice=%s speed=%.2f", voice, float(vcfg.get("speed", 1.0)))

    teaser = gen.produce_teaser(config, story["title"], body) if r.get("teaser", True) else ""
    if teaser:
        log.info("teaser: %s", teaser)

    base = gen.slugify(story["title"])[:40]
    title = story["title"]
    audio_dir = get_path(config, "audio"); audio_dir.mkdir(parents=True, exist_ok=True)
    out_dir = get_path(config, "output") / "shorts"; out_dir.mkdir(parents=True, exist_ok=True)

    music_pool = _pick_music(config, mood)
    sfx = _find_sfx(config)
    gpool = _gameplay_pool(config, args.gameplay, mood)
    if not gpool:
        raise RuntimeError("no gameplay clips available (add files to content/gameplay/)")
    last_gp = None
    outputs = []

    for i, part_text in enumerate(parts, 1):
        name = f"{base}_p{i}" if n_parts > 1 else base
        if i == 1:
            lead_in = f"{teaser} {title}. " if teaser else f"{title}. "
        else:
            lead_in = f"{title}. Part {i}. "
        spoken = lead_in + part_text
        if n_parts > 1 and i < n_parts:
            spoken += (f" And that's where part {i} ends. Part {i + 1} is up next — "
                       f"follow so you don't miss how this one ends.")

        arr, sr = synth(config, spoken, vcfg)
        audio = audio_dir / f"{name}.wav"
        sf.write(str(audio), arr, sr)
        dur = len(arr) / sr

        words = br.transcribe_words(audio, brc.get("whisper_model", "small"),
                                    args.whisper_device or brc.get("whisper_device", "auto"))

        # timing from whisper: teaser (hook) then title (card) read sequentially
        cardT = float(r.get("card_intro_seconds", 6.0))
        hook_end = 0.0
        if words and i == 1 and teaser:
            twc = len(spk.clean_for_tts(teaser).split())
            if twc > 0:
                hook_end = min(words[min(twc, len(words)) - 1][2] + 0.2, dur * 0.5)
        if words:
            k = min(len(spk.clean_for_tts(lead_in).split()), len(words))
            if k > 0:
                cardT = min(max(words[k - 1][2] + 0.3, hook_end + 1.0, 2.0), dur * 0.78)
        card_start = hook_end if (i == 1 and teaser) else 0.0
        cta_start = max(cardT + 1.0, dur - float(r.get("cta_seconds", 5.0)))

        gameplay = random.choice([g for g in gpool if g != last_gp] or gpool)
        last_gp = gameplay
        gstart = _rand_start(gameplay, dur)

        tmp = Path(tempfile.mkdtemp(prefix=f"reddit_{name}_"))
        try:
            ass = tmp / "cap.ass"
            ass.write_text(br.build_ass(words, brc, tw, th), encoding="utf-8")
            card = rc.render_card(story, tmp / "card.png", width=int(tw * 0.9))
            cta_txt = f"Part {i + 1} is up next" if (n_parts > 1 and i < n_parts) else "Turn on notifications"
            cta = rc.render_cta_card(tmp / "cta.png", text=cta_txt, width=int(tw * 0.86))
            hook_png = None
            if r.get("onscreen_hook", True) and i == 1 and teaser and hook_end > 0:
                hook_png = rc.render_hook_card(teaser, tmp / "hook.png", width=int(tw * 0.92))
            pill_png = None
            if r.get("progress_pill", True) and n_parts > 1:
                pill_png = rc.render_progress_pill(f"PART {i}/{n_parts}", tmp / "pill.png")

            # inputs: 0 gameplay, 1 voice, 2 card, 3 cta, then hook/pill/music/sfx
            cmd = ["ffmpeg", "-y", "-stream_loop", "-1"]
            if gstart > 0:
                cmd += ["-ss", f"{gstart:.2f}"]
            cmd += ["-i", str(gameplay), "-i", str(audio),
                    "-loop", "1", "-i", str(card), "-loop", "1", "-i", str(cta)]
            idx = 4
            hook_idx = pill_idx = idx_music = None
            if hook_png:
                cmd += ["-loop", "1", "-i", str(hook_png)]; hook_idx = idx; idx += 1
            if pill_png:
                cmd += ["-loop", "1", "-i", str(pill_png)]; pill_idx = idx; idx += 1
            if music_pool:
                cmd += ["-stream_loop", "-1", "-i", str(random.choice(music_pool))]; idx_music = idx; idx += 1
            sfx_list = []
            start_sfx = sfx["intro"] or sfx["whoosh"]
            if start_sfx:
                cmd += ["-i", str(start_sfx)]; sfx_list.append((idx, 0.1)); idx += 1
            if sfx["ding"]:
                cmd += ["-i", str(sfx["ding"])]; sfx_list.append((idx, cta_start)); idx += 1
            if sfx["outro"]:
                cmd += ["-i", str(sfx["outro"])]; sfx_list.append((idx, max(cta_start + 0.4, dur - 1.2))); idx += 1

            # video overlay chain
            vf = [f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[bg]"]
            cur = "bg"
            if r.get("card_animate", True):
                yexpr = f"'min(180, -h + ((t-{card_start:.2f})/0.45)*(180+h))'"
                vf.append(f"[{cur}][2:v]overlay=(W-w)/2:{yexpr}:enable='between(t,{card_start:.2f},{cardT:.2f})'[vc]")
            else:
                vf.append(f"[{cur}][2:v]overlay=(W-w)/2:180:enable='between(t,{card_start:.2f},{cardT:.2f})'[vc]")
            cur = "vc"
            vf.append(f"[{cur}][3:v]overlay=(W-w)/2:{int(th * 0.60)}:enable='between(t,{cta_start:.2f},{dur:.2f})'[vt]")
            cur = "vt"
            if hook_idx is not None:
                vf.append(f"[{cur}][{hook_idx}:v]overlay=(W-w)/2:{int(th * 0.30)}:enable='between(t,0,{hook_end:.2f})'[vh]")
                cur = "vh"
            if pill_idx is not None:
                vf.append(f"[{cur}][{pill_idx}:v]overlay=W-w-40:60[vp]")
                cur = "vp"
            vf.append(f"[{cur}]subtitles={br.escape_sub(ass)}[vout]")

            filt = vf + _build_audio_filter(a, 1, idx_music, sfx_list)
            out = out_dir / f"{name}.mp4"
            cmd += ["-t", f"{dur:.3f}", "-filter_complex", ";".join(filt),
                    "-map", "[vout]", "-map", "[aout]",
                    "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-r", "30", str(out)]
            log.info("[part %d/%d] compositing -> %s (music=%s sfx=%d)", i, n_parts, out.name,
                     "yes" if idx_music is not None else "no", len(sfx_list))
            if subprocess.run(cmd).returncode != 0:
                raise RuntimeError(f"ffmpeg failed on part {i}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        transcript = " ".join(w[0] for w in words)
        meta = gen.produce_metadata(config, transcript, topic=story["title"], language="en")
        if n_parts > 1:
            meta["title"] = f"{meta['title']} (Part {i}/{n_parts})"[:100]
            series = [f"📺 Part {i} of {n_parts} — watch the whole story on the channel.",
                      (f"▶ Part {i + 1} is up next!" if i < n_parts else "✅ Final part.")]
            meta["description"] = "\n".join(series) + "\n\n" + meta["description"]
            meta["playlist"] = title[:90]
        meta.update({"clip": name, "video": out.name, "subreddit": story["subreddit"],
                     "reddit_id": story["id"], "part": i, "parts": n_parts, "voice": voice,
                     "mood": mood, "moderation": mod})
        out.with_suffix(".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        out.with_suffix(".txt").write_text(
            f"TITLE:\n{meta['title']}\n\nDESCRIPTION:\n{meta['description']}\n\nTAGS:\n{', '.join(meta['tags'])}\n",
            encoding="utf-8")
        log.info("[part %d/%d] SEO: %s", i, n_parts, meta["title"])
        outputs.append(out)
        if args.upload:
            if mod["ok"]:
                br.upload_short(name)
            else:
                log.warning("[part %d/%d] SKIPPING upload (moderation flagged) — kept locally.", i, n_parts)

    rf.mark_used(config, story["id"])
    return outputs


def _rand_start(gameplay: Path, need: float) -> float:
    try:
        g = float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(gameplay)]).decode().strip())
        return round(random.uniform(0, max(0, g - need - 1)), 2) if g > need + 1 else 0.0
    except Exception:
        return 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description="Reddit-story shorts (fetch/select -> parts -> render).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--auto", action="store_true", help="fetch + LLM-select stor(y/ies)")
    g.add_argument("--id", help="specific Reddit post id (needs a local content/stories/<id>.json)")
    ap.add_argument("--count", type=int, default=1, help="how many stories (with --auto)")
    ap.add_argument("--gameplay", help="specific gameplay clip (else random per part)")
    ap.add_argument("--whisper-device", help="override whisper device (auto|cuda|cpu)")
    ap.add_argument("--upload", action="store_true", help="upload each part to YouTube (PRIVATE)")
    ap.add_argument("--config", help="use config.reddit.yaml")
    args = ap.parse_args()
    config = load_config(args.config)

    if args.id:
        story = get_story(config, args)
        stories = [story] if story else []
    else:
        stories = rf.select_stories(config, rf.fetch_candidates(config), args.count)

    if not stories:
        log.error("no story available (all used / none matched filters / fetch blocked).")
        return

    total = []
    for story in stories:
        try:
            total += make_story_videos(config, args, story)
        except Exception as exc:  # noqa: BLE001
            log.error("story failed (%s): %s", story.get("id"), exc)
    log.info("DONE: %d video(s) produced", len(total))
    for p in total:
        log.info("  ✓ %s", p.name)


if __name__ == "__main__":
    main()
