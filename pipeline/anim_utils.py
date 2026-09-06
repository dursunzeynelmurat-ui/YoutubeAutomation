#!/usr/bin/env python3
"""anim_utils.py — the anim/ handoff contract (Phase 2 prep).

Audio2Face-3D / MetaHuman Animator produce facial animation as a UE ASSET inside
the Unreal project (an Animation Sequence or a Level Sequence), not as a loose
file. This module defines the bridge between the filesystem pipeline and those
in-editor assets: a small JSON "sidecar" per clip, living in anim/<name>.json,
that records which baked UE asset drives the presenter's face for that clip.

Phase 3's render.py will consume this:
    anim = load_animation(config, name)
    if anim:  render using anim["ue_asset"]   else:  render the idle presenter
matching PIPELINE.md §9 ("render.py consumes whatever animation exists in anim/;
if none, it renders the idle presenter").

No Unreal dependency — this is pure filesystem/JSON and is fully testable today.

CLI:
    python pipeline/anim_utils.py --scaffold <name> [--audio audio/<name>.wav]
    python pipeline/anim_utils.py --check <name>
    python pipeline/anim_utils.py --list
where <name> is the clip base name (the .wav stem), e.g.
    2026-09-06_your-topic-here
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from _common import get_path, load_config, setup_logging

log = setup_logging()

# Sidecar schema ------------------------------------------------------------- #
REQUIRED_KEYS = ("name", "ue_asset", "asset_type")
VALID_ASSET_TYPES = ("performance", "level_sequence", "anim_sequence")
VALID_SOURCES = ("metahuman_audio_driven", "audio2face_3d", "other")


def sidecar_path(config: dict, name: str) -> Path:
    """anim/<name>.json for a clip base name (accepts a name with or without .json/.wav)."""
    stem = Path(name).stem if name.endswith((".json", ".wav")) else name
    return get_path(config, "anim") / f"{stem}.json"


def load_animation(config: dict, name: str) -> dict | None:
    """Return the validated sidecar dict for a clip, or None if there is no usable
    animation (missing file, or a scaffold that hasn't been filled in yet)."""
    path = sidecar_path(config, name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("anim sidecar %s is unreadable (%s) — treating as no animation.",
                    path.name, exc)
        return None

    missing = [k for k in REQUIRED_KEYS if not data.get(k)]
    if missing:
        # A freshly scaffolded, not-yet-filled sidecar lands here -> treat as idle.
        log.warning("anim sidecar %s is incomplete (missing/empty: %s) — Phase 3 will "
                    "render the IDLE presenter for this clip.", path.name, ", ".join(missing))
        return None
    if data["asset_type"] not in VALID_ASSET_TYPES:
        log.warning("anim sidecar %s has invalid asset_type '%s' (expected one of %s).",
                    path.name, data["asset_type"], VALID_ASSET_TYPES)
        return None
    return data


def scaffold(config: dict, name: str, audio: str | None) -> Path:
    """Write a template sidecar for a clip (does NOT overwrite an existing one)."""
    path = sidecar_path(config, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        sys.exit(f"[fatal] sidecar already exists: {path} (edit it, or delete to re-scaffold).")

    stem = path.stem
    if audio is None:
        # Default to the conventional wav location for this clip.
        audio = f"audio/{stem}.wav"

    template = {
        "name": stem,
        "audio": audio,
        # Fill these in AFTER baking the facial animation in Unreal (see
        # docs/PHASE2_AUDIO2FACE.md). Empty ue_asset => Phase 3 renders idle.
        "ue_asset": "",                       # e.g. "/Game/AIPresenter/Anims/FA_" + stem
        "asset_type": "level_sequence",       # "level_sequence" or "anim_sequence"
        "source": "metahuman_audio_driven",   # or "audio2face_3d"
        "fps": 30,
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "notes": "Bake facial anim from the audio above in UE, then set ue_asset "
                 "to the baked asset's content path and save this file.",
    }
    path.write_text(json.dumps(template, indent=2), encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Manage anim/ sidecars (Audio2Face handoff).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--scaffold", metavar="NAME", help="write a template sidecar for a clip")
    g.add_argument("--check", metavar="NAME", help="report whether a clip has usable animation")
    g.add_argument("--list", action="store_true", help="list all sidecars and their status")
    ap.add_argument("--audio", help="audio path to record (with --scaffold)")
    ap.add_argument("--config", help="path to config.yaml")
    args = ap.parse_args()
    config = load_config(args.config)

    if args.scaffold:
        path = scaffold(config, args.scaffold, args.audio)
        log.info("scaffolded %s", path)
        log.info("Next: bake the face anim in Unreal (docs/PHASE2_AUDIO2FACE.md), then set "
                 "\"ue_asset\" in that file.")
    elif args.check:
        anim = load_animation(config, args.check)
        if anim:
            log.info("READY: '%s' -> %s (%s)", args.check, anim["ue_asset"], anim["asset_type"])
        else:
            log.info("NO ANIMATION for '%s' -> Phase 3 would render the idle presenter.",
                     args.check)
    else:  # --list
        anim_dir = get_path(config, "anim")
        sidecars = sorted(anim_dir.glob("*.json")) if anim_dir.exists() else []
        if not sidecars:
            log.info("no sidecars in %s", anim_dir)
            return
        for sc in sidecars:
            anim = load_animation(config, sc.stem)
            status = f"READY -> {anim['ue_asset']}" if anim else "idle (not baked/linked)"
            log.info("%-45s %s", sc.name, status)


if __name__ == "__main__":
    main()
