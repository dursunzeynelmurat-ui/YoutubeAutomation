#!/usr/bin/env python3
"""upload.py — upload a finished video to YouTube (Phase 4).

Uploads output/shorts/<name>.mp4 (or --file) using the SEO sidecar
(<name>.json: title/description/tags) via the YouTube Data API v3.

Safety (PIPELINE.md §8):
  * Uploads are PRIVATE by default. Going public requires an explicit
    `--privacy public` on the command line — never automatic.
  * No secrets in code: OAuth uses a local client_secret.json + token.json,
    both git-ignored. First run opens your browser for consent (YOU authorize
    it in your Google account); the token is cached for later runs.

Setup (one time): see docs/PHASE4_UPLOAD.md to create the Google Cloud OAuth
client and download client_secret.json into automation/.

Usage:
    python pipeline/upload.py --input <name>                 # private (default)
    python pipeline/upload.py --input <name> --privacy unlisted
    python pipeline/upload.py --input <name> --privacy public  # explicit, deliberate
    python pipeline/upload.py --file path/to.mp4 --title "..." --description "..."
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _common import get_path, load_config, resolve, setup_logging

log = setup_logging()

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def get_service(config: dict):
    """Authorize via OAuth (cached token) and return a YouTube API client."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    yt = config["youtube"]
    secret = resolve(yt["client_secret"])
    token = resolve(yt["token_file"])
    if not secret.exists():
        sys.exit(f"[fatal] {secret} not found. Create a Google Cloud Desktop OAuth client and "
                 f"download it here (see docs/PHASE4_UPLOAD.md).")

    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            log.info("no valid token — opening your browser for Google consent…")
            creds = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES).run_local_server(port=0)
        token.write_text(creds.to_json(), encoding="utf-8")
        log.info("saved token: %s", token)
    return build("youtube", "v3", credentials=creds)


def load_meta(config: dict, name: str, args) -> dict:
    """Title/description/tags from the SEO sidecar, overridable by CLI flags."""
    meta = {}
    sidecar = get_path(config, "output") / "shorts" / f"{name}.json"
    if sidecar.exists():
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    yt = config["youtube"]
    title = args.title or meta.get("title") or name
    if yt.get("title_suffix") and yt["title_suffix"].strip().lower() not in title.lower():
        title = (title + yt["title_suffix"])[:100]
    description = args.description or meta.get("description", "")
    tags = (meta.get("tags") or []) + yt.get("default_tags", [])
    return {"title": title[:100], "description": description, "tags": list(dict.fromkeys(tags))[:30]}


def main() -> None:
    ap = argparse.ArgumentParser(description="Upload a video to YouTube (private by default).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--input", help="clip base name (uploads output/shorts/<name>.mp4 + sidecar)")
    g.add_argument("--file", help="explicit video file path")
    ap.add_argument("--privacy", choices=["private", "unlisted", "public"],
                    help="visibility (default from config = private). 'public' is explicit & deliberate.")
    ap.add_argument("--title", help="override title")
    ap.add_argument("--description", help="override description")
    ap.add_argument("--config", help="path to config.yaml")
    args = ap.parse_args()

    config = load_config(args.config)
    yt = config["youtube"]

    if args.input:
        name = Path(args.input).stem
        video = get_path(config, "output") / "shorts" / f"{name}.mp4"
    else:
        video = Path(args.file).resolve()
        name = video.stem
    if not video.exists():
        sys.exit(f"[fatal] video not found: {video}")

    meta = load_meta(config, name, args)
    privacy = args.privacy or yt.get("privacy_status", "private")
    if privacy == "public":
        log.warning("PUBLIC upload requested explicitly — this will be visible to everyone.")

    from googleapiclient.http import MediaFileUpload
    service = get_service(config)
    body = {
        "snippet": {"title": meta["title"], "description": meta["description"],
                    "tags": meta["tags"], "categoryId": str(yt.get("category_id", "22"))},
        "status": {"privacyStatus": privacy,
                   "selfDeclaredMadeForKids": bool(yt.get("made_for_kids", False))},
    }
    log.info("uploading %s  (privacy=%s)", video.name, privacy)
    log.info("  title: %s", meta["title"])
    media = MediaFileUpload(str(video), chunksize=-1, resumable=True, mimetype="video/*")
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)

    resp = None
    while resp is None:
        status, resp = request.next_chunk()
        if status:
            log.info("  upload %d%%", int(status.progress() * 100))
    vid = resp.get("id")
    log.info("done ✓  https://youtu.be/%s  (privacy=%s)", vid, privacy)
    log.info("Review it in YouTube Studio; make it public there when you're happy.")


if __name__ == "__main__":
    main()
