#!/usr/bin/env python3
"""promo.py — cut punchy vertical PROMO shorts from a finished long-form video.

Funnel: each long video auto-produces N teaser Shorts from its tensest beats, cropped
9:16, with a big on-screen hook + an end "▶ FULL STORY" card. The OG long video's link
is injected into each promo's description at upload time (see upload.py). Pure ffmpeg —
no GPU (it slices the already-rendered long mp4).
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import re

import requests

import brainrot as br
import reddit_card as rc
from _common import get_path, load_config, setup_logging

log = setup_logging()


def _promo_hooks(config, title: str, texts: list[str]) -> list[str]:
    """One LLM call -> a punchy on-screen hook per promo (scroll-stopper, no spoilers).
    Falls back to the story title on any failure."""
    llm = config["llm"]
    listing = "\n".join(f"[{j}] {t[:220]}" for j, t in enumerate(texts))
    system = ("You write ultra-short punchy on-screen HOOK captions for horror teaser Shorts that make "
              "people stop scrolling and want the full story. For each numbered excerpt write ONE hook, "
              "max 8 words, present tense, no spoilers, no ending period. "
              f'Return ONLY JSON: {{"hooks": ["...", ...]}} with exactly {len(texts)} strings in order.')
    try:
        r = requests.post(f"{llm['ollama_host']}/api/chat", timeout=llm.get("request_timeout", 600),
                          json={"model": llm.get("seo_model", llm["fallback"]), "stream": False,
                                "format": "json", "keep_alive": llm.get("keep_alive", 0),
                                "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": f"Title: {title}\n\n{listing}"}]})
        r.raise_for_status()
        data = json.loads(r.json()["message"]["content"])
        arr = data.get("hooks") if isinstance(data, dict) else data
        out = [str(x).strip().rstrip(".") for x in (arr or []) if str(x).strip()]
        if out:
            return (out + [title] * len(texts))[:len(texts)]
    except Exception as exc:  # noqa: BLE001
        log.warning("promo hook LLM failed (%s) — using title.", exc)
    return [title] * len(texts)

_TENSE_KW = ("scream", "blood", "dead", "ran", "run", "running", "crept", "creeping", "toward",
             "behind", "shadow", "figure", "staring", "smile", "smiling", "grin", "door", "footstep",
             "panic", "terrified", "frozen", "whisper", "closer", "grabbed", "knock", "silhouette",
             "insane", "shaking", "breathing", "watching", "gone", "wrong")


def _pick_tense(scenes: list[str], k: int) -> list[int]:
    """Top-k tensest chunks (1-based), spread out (bias to later/climax)."""
    n = max(1, len(scenes))
    def score(i):
        return sum(scenes[i].lower().count(w) for w in _TENSE_KW) + 1.2 * (i / (n - 1) if n > 1 else 0)
    picked = []
    for i in sorted(range(len(scenes)), key=score, reverse=True):
        if len(picked) >= k:
            break
        if all(abs(i - p) > 1 for p in picked):
            picked.append(i)
    return sorted(p + 1 for p in picked)


def make_promos(config, long_mp4: Path, base: str, title: str, sub: str, intro: float,
                dur: float, spans: list, scenes: list, promo_idx: list, seo_tags=None) -> list[Path]:
    """Cut one promo per scene index in promo_idx. Returns the promo mp4 paths."""
    lf = config.get("longform", {})
    tw, th = 1080, 1920
    plen = float(lf.get("promo_seconds", 30))
    cta_txt = lf.get("promo_cta", "Full story on the channel")
    out_dir = get_path(config, "output") / "shorts"; out_dir.mkdir(parents=True, exist_ok=True)
    total = intro + dur
    hooks = _promo_hooks(config, title, [scenes[i - 1] for i in promo_idx])
    outs = []

    for k, i in enumerate(promo_idx, 1):
        s0 = intro + spans[i - 1][0]                      # absolute start in the long video
        start = max(0.0, min(s0, max(0.0, total - plen)))
        length = min(plen, total - start)
        if length < 8:
            continue
        hook = hooks[k - 1].strip()

        tmp = Path(tempfile.mkdtemp(prefix=f"promo_{base}_{k}_"))
        try:
            hook_png = rc.render_hook_card(hook, tmp / "hook.png", width=int(tw * 0.92))
            cta_png = rc.render_cta_card(tmp / "cta.png", text=cta_txt, button="WATCH FULL", width=int(tw * 0.88))
            name = f"{base}_promo{k}"
            out = out_dir / f"{name}.mp4"
            cta_start = max(0.0, length - 4.0)
            filt = (f"[0:v]crop=ih*9/16:ih,scale={tw}:{th},setsar=1[bg];"
                    f"[bg][1:v]overlay=(W-w)/2:140:enable='between(t,0,{min(length,4.5):.2f})'[v1];"
                    f"[v1][2:v]overlay=(W-w)/2:{int(th*0.60)}:enable='between(t,{cta_start:.2f},{length:.2f})'[v]")
            cmd = ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(long_mp4),
                   "-loop", "1", "-i", str(hook_png), "-loop", "1", "-i", str(cta_png),
                   "-t", f"{length:.2f}", "-filter_complex", filt,
                   "-map", "[v]", "-map", "0:a:0",
                   "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-r", "30", str(out)]
            if subprocess.run(cmd, capture_output=True).returncode != 0:
                log.warning("promo %d failed (ffmpeg) — skipping", k); continue
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

        # sidecar — promo_for links it to the OG long video; description CTA gets the
        # real OG URL injected at upload (upload.py) via the {OG_LINK} placeholder.
        tags = list(dict.fromkeys((seo_tags or []) + ["shorts", "horror", "scary", "reddit", "nosleep", "creepypasta"]))
        desc = (f"{hook}\n\n▶ Watch the FULL story: {{OG_LINK}}\n\n"
                f"Story adapted from a public Reddit post for entertainment.\n"
                f"#shorts #horror #scary #nosleep #creepypasta")
        meta = {"title": f"{hook} #Shorts"[:100], "description": desc, "tags": tags,
                "clip": name, "video": out.name, "subreddit": sub,
                "promo_for": base, "og_title": title, "kind": "promo"}
        out.with_suffix(".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        out.with_suffix(".txt").write_text(
            f"TITLE:\n{meta['title']}\n\nDESCRIPTION:\n{desc}\n\nTAGS:\n{', '.join(tags)}\n", encoding="utf-8")
        log.info("  promo %d/%d -> %s  (from scene %d)", k, len(promo_idx), out.name, i)
        outs.append(out)
    return outs


def main() -> None:
    """Standalone: cut promo shorts from an ALREADY-rendered long video (any origin).
    Re-derives timing with whisper, so it works on long videos made before this feature.
        python pipeline/promo.py --input <long clip name> --config config.reddit.yaml
    """
    import argparse
    ap = argparse.ArgumentParser(description="Cut promo shorts from a finished long video.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--input", help="long clip base name (output/longform/<name>.mp4)")
    g.add_argument("--file", help="explicit long video path")
    ap.add_argument("--count", type=int, help="override longform.promo_shorts")
    ap.add_argument("--whisper-device", help="auto|cuda|cpu")
    ap.add_argument("--config", help="use config.reddit.yaml")
    args = ap.parse_args()
    config = load_config(args.config)
    lf = config.get("longform", {})

    if args.input:
        mp4 = get_path(config, "output") / "longform" / f"{Path(args.input).stem}.mp4"
    else:
        mp4 = Path(args.file).resolve()
    if not mp4.exists():
        raise SystemExit(f"[fatal] long video not found: {mp4}")
    base = mp4.stem
    sc = mp4.with_suffix(".json")
    meta = json.loads(sc.read_text(encoding="utf-8")) if sc.exists() else {}
    title = meta.get("title", base); sub = meta.get("subreddit", "nosleep")

    words = br.transcribe_words(mp4, config.get("brainrot", {}).get("whisper_model", "small"),
                                args.whisper_device or "auto")
    if not words:
        raise SystemExit("[fatal] no words transcribed from the long video.")
    # group into ~40-word chunks with absolute timings (intro already baked into the video)
    chunks = [words[i:i + 40] for i in range(0, len(words), 40)]
    scenes = [" ".join(w[0] for w in c) for c in chunks]
    spans = [(c[0][1], c[-1][2]) for c in chunks]
    dur = words[-1][2]
    n = args.count or int(lf.get("promo_shorts", 4))
    idx = _pick_tense(scenes, min(n, len(scenes)))
    log.info("cutting %d promo(s) from %s (chunks: %s)", len(idx), mp4.name, idx)
    outs = make_promos(config, mp4, base, title, sub, 0.0, dur, spans, scenes, idx, meta.get("tags"))
    log.info("DONE: %d promo short(s) -> output/shorts/", len(outs))


if __name__ == "__main__":
    main()
