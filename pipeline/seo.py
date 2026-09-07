#!/usr/bin/env python3
"""seo.py — (re)generate SEO title / description / tags for a clip or a topic.

For a rendered clip it transcribes the audio and writes the metadata back into the
clip's sidecar (.json/.txt). For a --topic it just prints suggested metadata.

    python pipeline/seo.py --input <clip name> --config config.reddit.yaml
    python pipeline/seo.py --topic "haunted hospital story" --config config.reddit.yaml
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import brainrot as br
import generate as gen
from _common import get_path, load_config, setup_logging

log = setup_logging()


def _find_video(config, name: str) -> Path | None:
    for sub in ("longform", "shorts"):
        p = get_path(config, "output") / sub / f"{name}.mp4"
        if p.exists():
            return p
    return None


def _print(meta):
    log.info("TITLE:\n%s", meta["title"])
    log.info("DESCRIPTION:\n%s", meta["description"])
    log.info("TAGS:\n%s", ", ".join(meta.get("tags", [])))


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate SEO title/description/tags.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--input", help="clip name (searches output/longform then output/shorts)")
    g.add_argument("--file", help="explicit video file")
    g.add_argument("--topic", help="generate from a topic string (no video)")
    ap.add_argument("--language")
    ap.add_argument("--whisper-device")
    ap.add_argument("--config")
    args = ap.parse_args()
    config = load_config(args.config)
    lang = args.language or config["content"]["language"]

    if args.topic:
        _print(gen.produce_metadata(config, args.topic, topic=args.topic, language=lang))
        return

    video = Path(args.file).resolve() if args.file else _find_video(config, Path(args.input).stem)
    if not video or not video.exists():
        sys.exit("[fatal] video not found (check the clip name / output folders).")
    sc = video.with_suffix(".json")
    existing = json.loads(sc.read_text(encoding="utf-8")) if sc.exists() else {}
    topic = existing.get("og_title") or existing.get("title") or video.stem

    log.info("transcribing %s for SEO…", video.name)
    words = br.transcribe_words(video, config.get("brainrot", {}).get("whisper_model", "small"),
                                args.whisper_device or "auto")
    transcript = " ".join(w[0] for w in words)
    meta = gen.produce_metadata(config, transcript, topic=topic, language=lang)

    existing.update({"title": meta["title"], "description": meta["description"],
                     "tags": meta["tags"], "hashtags": meta.get("hashtags")})
    sc.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    video.with_suffix(".txt").write_text(
        f"TITLE:\n{meta['title']}\n\nDESCRIPTION:\n{meta['description']}\n\nTAGS:\n{', '.join(meta['tags'])}\n",
        encoding="utf-8")
    _print(meta)
    log.info("updated sidecar: %s", sc.name)


if __name__ == "__main__":
    main()
