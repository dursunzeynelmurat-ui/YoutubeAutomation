#!/usr/bin/env python3
"""doctor.py — health check for the pipeline. Prints what's OK and what's missing."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from _common import get_path, load_config, resolve, setup_logging

log = setup_logging()


def _ok(b):
    return "OK  " if b else "MISSING "


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument("--config"); args = ap.parse_args()
    config = load_config(args.config)
    log.info("=== Scroll & Told — system check ===")
    log.info("python: %s", sys.version.split()[0])

    # torch / CUDA
    try:
        import torch
        log.info("torch %s | CUDA available: %s | %s", torch.__version__, torch.cuda.is_available(),
                 torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU only")
    except Exception as exc:  # noqa: BLE001
        log.info("torch: NOT importable (%s)", exc)

    # ffmpeg / ffprobe
    for tool in ("ffmpeg", "ffprobe"):
        log.info("%s %s", _ok(shutil.which(tool)), tool + (" found" if shutil.which(tool) else " NOT on PATH"))

    # ollama
    try:
        import requests
        host = config["llm"]["ollama_host"]
        r = requests.get(f"{host}/api/tags", timeout=5)
        names = [m["name"] for m in r.json().get("models", [])]
        log.info("OK   ollama reachable at %s (%d models)", host, len(names))
        for want in (config["llm"]["primary"], config["llm"]["fallback"], config["llm"].get("seo_model", "")):
            if want:
                hit = any(want.split(":")[0] in n for n in names)
                log.info("   %s model %s", _ok(hit), want)
    except Exception as exc:  # noqa: BLE001
        log.info("MISSING  ollama not reachable (%s) — start the Ollama app", exc)

    # local image/video models
    for mid in ("stabilityai--sdxl-turbo", "stabilityai--stable-video-diffusion-img2vid-xt"):
        p = Path("models") / f"models--{mid}"
        log.info("%s model cache: %s", _ok(p.exists()), mid.replace("--", "/"))

    # secrets / token
    yt = config["youtube"]
    for f in (yt.get("client_secret", ""), yt.get("token_file", "")):
        if f:
            log.info("%s youtube %s", _ok(resolve(f).exists()), f)

    # assets
    for key in ("gameplay", "music", "sfx", "ambient"):
        if key in config.get("paths", {}):
            d = get_path(config, key)
            n = sum(1 for _ in d.rglob("*.*")) if d.exists() else 0
            log.info("   %s/: %d file(s)%s", key, n, "  (add some!)" if n == 0 and key in ("gameplay", "music") else "")

    # disk
    try:
        total, used, free = shutil.disk_usage(".")
        log.info("disk free: %.1f GB", free / 1e9)
    except Exception:  # noqa: BLE001
        pass
    log.info("=== done ===")


if __name__ == "__main__":
    main()
