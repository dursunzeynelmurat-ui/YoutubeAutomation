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

# upload-only by default; playlist create/add needs the broader youtube scope.
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
FULL_SCOPE = "https://www.googleapis.com/auth/youtube"


def _scopes(config: dict) -> list[str]:
    return [FULL_SCOPE] if config.get("youtube", {}).get("make_playlists") else [UPLOAD_SCOPE]


def get_service(config: dict):
    """Authorize via OAuth (cached token) and return a YouTube API client."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    yt = config["youtube"]
    scopes = _scopes(config)
    secret = resolve(yt["client_secret"])
    token = resolve(yt["token_file"])
    if not secret.exists():
        sys.exit(f"[fatal] {secret} not found. Create a Google Cloud Desktop OAuth client and "
                 f"download it here (see docs/PHASE4_UPLOAD.md).")

    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            log.info("no valid token — opening your browser for Google consent…")
            creds = InstalledAppFlow.from_client_secrets_file(str(secret), scopes).run_local_server(port=0)
        token.write_text(creds.to_json(), encoding="utf-8")
        log.info("saved token: %s", token)
    return build("youtube", "v3", credentials=creds)


def _sidecar_for(config, name: str, video: Path | None = None) -> Path:
    """Prefer the video's own sibling .json (long videos live in output/longform/),
    else fall back to output/shorts/<name>.json."""
    if video is not None and video.with_suffix(".json").exists():
        return video.with_suffix(".json")
    return get_path(config, "output") / "shorts" / f"{name}.json"


def load_meta(config: dict, name: str, args, video: Path | None = None) -> dict:
    """Title/description/tags from the SEO sidecar, overridable by CLI flags."""
    meta = {}
    sidecar = _sidecar_for(config, name, video)
    if sidecar.exists():
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    yt = config["youtube"]
    title = args.title or meta.get("title") or name
    if yt.get("title_suffix") and yt["title_suffix"].strip().lower() not in title.lower():
        title = (title + yt["title_suffix"])[:100]
    description = args.description or meta.get("description", "")
    tags = (meta.get("tags") or []) + yt.get("default_tags", [])
    return {"title": title[:100], "description": description, "tags": list(dict.fromkeys(tags))[:30]}


def _map_path(config) -> Path:
    return get_path(config, "output") / "shorts" / ".uploads.json"


def _load_map(config) -> dict:
    p = _map_path(config)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"playlists": {}, "videos": {}}


def _save_map(config, mapd: dict):
    _map_path(config).write_text(json.dumps(mapd, indent=2, ensure_ascii=False), encoding="utf-8")


def link_series_and_playlist(service, config, meta: dict, video_id: str):
    """Group a multi-part story into a playlist and cross-link consecutive parts.
    Best-effort: any API hiccup is logged, never fails the upload. Needs make_playlists + FULL_SCOPE."""
    yt = config["youtube"]
    rid = meta.get("reddit_id")
    parts = int(meta.get("parts", 1) or 1)
    part = int(meta.get("part", 1) or 1)
    if not rid or parts < 2:
        return
    mapd = _load_map(config)
    mapd["videos"].setdefault(rid, {})[str(part)] = video_id

    if yt.get("make_playlists") and meta.get("playlist"):
        name = meta["playlist"]
        try:
            pid = mapd["playlists"].get(name)
            if not pid:
                pid = service.playlists().insert(
                    part="snippet,status",
                    body={"snippet": {"title": name, "description": "Full Reddit story, in parts."},
                          "status": {"privacyStatus": yt.get("playlist_privacy", "public")}}).execute()["id"]
                mapd["playlists"][name] = pid
                log.info("  created playlist: %s", name)
            service.playlistItems().insert(
                part="snippet",
                body={"snippet": {"playlistId": pid,
                                  "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute()
            log.info("  added to playlist '%s' (part %d/%d)", name, part, parts)
        except Exception as exc:  # noqa: BLE001
            log.warning("  playlist step failed: %s", exc)

    # link the PREVIOUS part's description to this newer part (parts upload in order)
    prev = mapd["videos"].get(rid, {}).get(str(part - 1))
    if prev:
        try:
            items = service.videos().list(part="snippet", id=prev).execute().get("items", [])
            if items:
                snip = items[0]["snippet"]
                link = f"https://youtu.be/{video_id}"
                if link not in snip.get("description", ""):
                    snip["description"] = (snip.get("description", "") + f"\n\n▶ Part {part}: {link}").strip()
                    service.videos().update(part="snippet", body={"id": prev, "snippet": snip}).execute()
                    log.info("  linked Part %d -> Part %d in description", part - 1, part)
        except Exception as exc:  # noqa: BLE001
            log.warning("  series-link step failed: %s", exc)

    _save_map(config, mapd)


def upload_one(service, config, name: str, video: Path, args, privacy: str) -> str | None:
    """Upload one video (private/unlisted/public) + playlist/series linking. Returns video id."""
    from googleapiclient.http import MediaFileUpload
    yt = config["youtube"]
    meta = load_meta(config, name, args, video)
    # Promo funnel: inject the OG long video's URL into the teaser's description.
    sc = {}
    scf = _sidecar_for(config, name, video)
    if scf.exists():
        try:
            sc = json.loads(scf.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            sc = {}
    if sc.get("promo_for"):
        ogid = (_load_map(config).get("clips") or {}).get(sc["promo_for"])
        link = f"https://youtu.be/{ogid}" if ogid else "our channel (link in pinned comment)"
        meta["description"] = meta["description"].replace("{OG_LINK}", link)
    meta["description"] = meta["description"].replace("{OG_LINK}", "our channel")  # safety
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
    # Record clip -> video id so promos can link back to their OG long video.
    if vid:
        try:
            mapd = _load_map(config); mapd.setdefault("clips", {})[name] = vid; _save_map(config, mapd)
        except Exception as exc:  # noqa: BLE001
            log.warning("clip-id record skipped: %s", exc)
    if vid and sc.get("reddit_id"):      # multi-part story playlist/series linking
        try:
            link_series_and_playlist(service, config, sc, vid)
        except Exception as exc:  # noqa: BLE001
            log.warning("playlist/series linking skipped: %s", exc)
    return vid


def _remove_from_queue(qf: Path, name: str):
    lines = qf.read_text(encoding="utf-8").splitlines()
    qf.write_text("\n".join(ln for ln in lines if ln.strip() != name) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Upload a video to YouTube (private by default).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--input", help="clip base name (uploads output/shorts/<name>.mp4 + sidecar)")
    g.add_argument("--file", help="explicit video file path")
    g.add_argument("--queue", action="store_true",
                   help="upload every clip listed in output/upload_queue.txt, in order (resumable)")
    ap.add_argument("--queue-file", help="override queue path (default output/upload_queue.txt)")
    ap.add_argument("--privacy", choices=["private", "unlisted", "public"],
                    help="visibility (default from config = private). 'public' is explicit & deliberate.")
    ap.add_argument("--title", help="override title")
    ap.add_argument("--description", help="override description")
    ap.add_argument("--config", help="path to config.yaml")
    args = ap.parse_args()

    config = load_config(args.config)
    yt = config["youtube"]
    privacy = args.privacy or yt.get("privacy_status", "private")
    if privacy == "public":
        log.warning("PUBLIC upload requested explicitly — this will be visible to everyone.")
    service = get_service(config)

    if args.queue:
        qf = resolve(args.queue_file) if args.queue_file else get_path(config, "output") / "upload_queue.txt"
        if not qf.exists():
            sys.exit(f"[fatal] no upload queue at {qf} (run rerender.py or a batch first).")
        names = [ln.strip() for ln in qf.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.startswith("#")]
        if not names:
            sys.exit("[fatal] upload queue is empty (all entries commented out?).")
        log.info("QUEUE: %d clip(s) to upload (privacy=%s)", len(names), privacy)
        uploaded = 0
        for name in names:
            video = get_path(config, "output") / "shorts" / f"{name}.mp4"
            if not video.exists():
                log.warning("  missing, skipping: %s", name)
                continue
            try:
                if upload_one(service, config, name, video, args, privacy):
                    uploaded += 1
                    _remove_from_queue(qf, name)   # persist progress → resumable
            except Exception as exc:  # noqa: BLE001
                log.error("  upload failed for %s: %s (left in queue)", name, exc)
        log.info("QUEUE DONE: uploaded %d clip(s). Review in YouTube Studio before publishing.", uploaded)
        return

    if args.input:
        name = Path(args.input).stem
        video = get_path(config, "output") / "shorts" / f"{name}.mp4"
    else:
        video = Path(args.file).resolve()
        name = video.stem
    if not video.exists():
        sys.exit(f"[fatal] video not found: {video}")
    upload_one(service, config, name, video, args, privacy)
    log.info("Review it in YouTube Studio; make it public there when you're happy.")


if __name__ == "__main__":
    main()
