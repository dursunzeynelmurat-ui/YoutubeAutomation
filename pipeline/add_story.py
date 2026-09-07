#!/usr/bin/env python3
"""add_story.py — create a local story record from pasted text (Reddit-block workaround).

Reddit is blocked on some networks, so instead of fetching, paste a story's text into a
file and register it here. Then render it with redditstory.py (9:16) or longform.py (16:9)
using --id <id>.

Usage:
    python pipeline/add_story.py --id smilingman --title "The Smiling Man" \
        --subreddit nosleep --textfile mystory.txt
    # or pipe/enter the text inline:
    python pipeline/add_story.py --id tifu42 --title "..." --subreddit tifu --text "full story..."
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _common import get_path, load_config, resolve, setup_logging

log = setup_logging()


def main() -> None:
    ap = argparse.ArgumentParser(description="Register a story from pasted text.")
    ap.add_argument("--id", required=True, help="short id (used for the filename + --id later)")
    ap.add_argument("--title", required=True, help="story title (shown on the card)")
    ap.add_argument("--subreddit", default="nosleep", help="subreddit label (drives mood)")
    ap.add_argument("--author", default="anonymous")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--textfile", help="path to a .txt file with the full story body")
    g.add_argument("--text", help="the full story body inline")
    ap.add_argument("--config", help="use config.reddit.yaml")
    args = ap.parse_args()
    config = load_config(args.config)

    body = (resolve(args.textfile).read_text(encoding="utf-8") if args.textfile else args.text).strip()
    if len(body.split()) < 30:
        sys.exit(f"[fatal] story body looks too short ({len(body.split())} words) — check the text.")

    rec = {"id": args.id, "subreddit": args.subreddit, "title": args.title,
           "author": args.author, "selftext": body, "score": 0, "num_comments": 0}
    d = get_path(config, "stories"); d.mkdir(parents=True, exist_ok=True)
    out = d / f"{args.id}.json"
    out.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("saved %s  (r/%s, %d words)", out, args.subreddit, len(body.split()))
    log.info("now render it:")
    log.info("  9:16 shorts :  python pipeline/redditstory.py --id %s --config %s", args.id, args.config or "config.reddit.yaml")
    log.info("  16:9 long   :  python pipeline/longform.py   --id %s --config %s", args.id, args.config or "config.reddit.yaml")


if __name__ == "__main__":
    main()
