#!/usr/bin/env python3
"""rerender.py — re-render existing Reddit-story shorts with the CURRENT templates.

Recovers each story from its saved narration script (content/scripts/*.md, whose
header comment holds the original title + selftext and whose body is the faithful
narration) plus the existing sidecar (subreddit / reddit_id), then re-runs the render
with every current feature (on-screen hook, animated card, progress pill, emphasis
captions, mood voice/pace, music+SFX, CTA, -14 LUFS). It REUSES the saved narration
(no re-generation, no re-fetch, no drift) and overwrites the old output files.

Usage:
    python pipeline/rerender.py --config config.reddit.yaml               # every old story
    python pipeline/rerender.py --config config.reddit.yaml --only 1w4x5o8,1w6c0fe
    python pipeline/rerender.py --config config.reddit.yaml --skip demo1  # e.g. skip a test
"""
from __future__ import annotations

import argparse
import glob
import re
from pathlib import Path
from types import SimpleNamespace

import generate as gen
import redditstory as rs
from _common import get_path, load_config, setup_logging

log = setup_logging()


def _parse_script(md_text: str) -> tuple[str, str]:
    """Return (original_title, narration_body) from a saved story script."""
    title = ""
    m = re.search(r"<!--\s*topic:\s*(.*?)\s*\|\s*model:", md_text, re.DOTALL)
    if m:
        topic = m.group(1)
        tm = re.match(r"Title:\s*(.*?)(?:\n|$)", topic)
        title = (tm.group(1) if tm else topic).strip()
    body = md_text.split("-->", 1)[-1].strip() if "-->" in md_text else md_text.strip()
    return title, body


def build_sidecar_index(config) -> dict:
    """base filename (without _pN) -> {subreddit, reddit_id, author, parts}."""
    idx = {}
    import json
    for j in glob.glob(str(get_path(config, "output") / "shorts" / "*.json")):
        try:
            d = json.loads(Path(j).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not d.get("reddit_id"):
            continue
        base = re.sub(r"_p\d+$", "", Path(j).stem)
        idx.setdefault(base, {"subreddit": d.get("subreddit", "reddit"), "reddit_id": d["reddit_id"],
                              "author": d.get("author", "user"), "parts": d.get("parts", 1)})
    return idx


def build_script_index(config) -> dict:
    """slug(title)[:40] -> (title, body), preferring the longest body on collisions."""
    scripts = {}
    for md in glob.glob(str(get_path(config, "scripts") / "*.md")):
        title, body = _parse_script(Path(md).read_text(encoding="utf-8"))
        if not title or not body:
            continue
        key = gen.slugify(title)[:40]
        if key not in scripts or len(body) > len(scripts[key][1]):
            scripts[key] = (title, body)
    return scripts


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-render old Reddit shorts with current templates.")
    ap.add_argument("--only", help="comma-separated reddit_ids to include")
    ap.add_argument("--skip", help="comma-separated reddit_ids to exclude")
    ap.add_argument("--gameplay", help="force a gameplay clip (else mood/random)")
    ap.add_argument("--whisper-device", help="auto|cuda|cpu")
    ap.add_argument("--config", help="use config.reddit.yaml")
    args = ap.parse_args()
    config = load_config(args.config)

    sidecars = build_sidecar_index(config)
    scripts = build_script_index(config)
    only = set(x for x in (args.only or "").split(",") if x)
    skip = set(x for x in (args.skip or "").split(",") if x)

    story_args = SimpleNamespace(upload=False, gameplay=args.gameplay, whisper_device=args.whisper_device)
    out_dir = get_path(config, "output") / "shorts"
    done, missing = [], []

    for base, meta in sidecars.items():
        rid = meta["reddit_id"]
        if (only and rid not in only) or (rid in skip):
            continue
        if base not in scripts:
            log.warning("no saved script for '%s' (rid %s) — skipping", base, rid)
            missing.append(rid)
            continue
        title, body = scripts[base]
        story = {"id": rid, "subreddit": meta["subreddit"], "title": title,
                 "author": meta["author"], "selftext": body}
        # remove old outputs for this base (new part count may differ)
        for f in out_dir.glob(f"{base}*"):
            if re.match(rf"^{re.escape(base)}(_p\d+)?\.(mp4|json|txt)$", f.name):
                f.unlink(missing_ok=True)
        log.info("=== re-rendering r/%s: %s", meta["subreddit"], title)
        try:
            done += rs.make_story_videos(config, story_args, story, body=body)
        except Exception as exc:  # noqa: BLE001
            log.error("rerender failed (%s): %s", rid, exc)

    log.info("RERENDER DONE: %d video(s) across %d stor(y/ies)", len(done),
             len({re.sub(r'_p\d+$', '', p.stem) for p in done}))
    if missing:
        log.info("no script found for: %s", ", ".join(missing))

    # mark everything as pending upload (consumed later by: upload.py --queue)
    if done:
        import json as _json
        qf = get_path(config, "output") / "upload_queue.txt"
        existing = set()
        if qf.exists():
            existing = {ln.strip().lstrip("# ").split("   ")[0]
                        for ln in qf.read_text(encoding="utf-8").splitlines()
                        if ln.strip() and not ln.startswith("##")}
        lines = ["## Reddit upload queue — one clip per line, uploaded in order (parts stay together).",
                 "## '#' prefix = skipped (moderation-flagged; review before enabling).",
                 "## Once the channel + OAuth are set up:  python pipeline/upload.py --queue --config config.reddit.yaml",
                 ""]
        for p in done:
            ok = True
            try:
                ok = _json.loads(p.with_suffix(".json").read_text(encoding="utf-8")).get("moderation", {}).get("ok", True)
            except Exception:  # noqa: BLE001
                pass
            lines.append(p.stem if ok else f"# {p.stem}   (moderation-flagged, review first)")
        qf.write_text("\n".join(lines) + "\n", encoding="utf-8")
        n_flagged = sum(1 for p in done
                        if not _safe_ok(p))
        log.info("upload queue: %s  (%d queued, %d flagged/skipped)", qf, len(done) - n_flagged, n_flagged)


def _safe_ok(p) -> bool:
    import json as _json
    try:
        return bool(_json.loads(p.with_suffix(".json").read_text(encoding="utf-8")).get("moderation", {}).get("ok", True))
    except Exception:  # noqa: BLE001
        return True


if __name__ == "__main__":
    main()
