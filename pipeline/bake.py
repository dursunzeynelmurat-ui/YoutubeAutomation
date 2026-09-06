#!/usr/bin/env python3
"""bake.py — headless MetaHuman facial bake launcher (Phase 2, automated).

Given an approved clip's voice wav, drives Unreal headless to import the audio,
process a MetaHuman Performance (blocking solve), and export a facial
AnimSequence — then writes the anim/<clip>.json sidecar automatically so
render.py picks it up. No manual editor step.

Usage:
    python pipeline/bake.py --input <clip>

Reference performance (reused for its identity/config) comes from
config.yaml unreal.ref_performance (default /Game/NewMetaHumanPerformance).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from _common import get_path, load_config, setup_logging

log = setup_logging()


def main() -> None:
    ap = argparse.ArgumentParser(description="Headless MetaHuman facial bake.")
    ap.add_argument("--input", required=True, help="clip base name (wav stem)")
    ap.add_argument("--ref", help="reference Performance asset (override config)")
    ap.add_argument("--config", help="path to config.yaml")
    args = ap.parse_args()

    config = load_config(args.config)
    u = config["unreal"]
    name = Path(args.input).stem
    audio = get_path(config, "audio") / f"{name}.wav"
    if not audio.exists():
        sys.exit(f"[fatal] audio not found: {audio} — run speak.py first.")

    engine = Path(u["engine_path"])
    exe = engine / "Engine" / "Binaries" / "Win64" / "UnrealEditor-Cmd.exe"
    uproject = Path(u["project_path"])
    for p in (exe, uproject):
        if not p.exists():
            sys.exit(f"[fatal] not found: {p}")
    ref = args.ref or u.get("ref_performance", "/Game/NewMetaHumanPerformance")

    renders = get_path(config, "renders"); renders.mkdir(parents=True, exist_ok=True)
    fd, job_path = tempfile.mkstemp(prefix="bake_job_", suffix=".json", dir=renders)
    os.close(fd)
    Path(job_path).write_text(json.dumps({
        "clip": name, "audio_file": str(audio), "ref_performance": ref,
    }), encoding="utf-8")

    ue_script = Path(__file__).resolve().parent / "ue" / "bake_job.py"
    args_ue = [str(exe), str(uproject), f"-ExecCmds=py {ue_script}",
               "-unattended", "-nosplash", "-nopause", "-stdout", "-FullStdOutLogOutput"]
    env = dict(os.environ, AIPRESENTER_JOB=job_path)
    log.info("baking facial animation for '%s' (headless UE: import → process → export)…", name)
    try:
        proc = subprocess.run(args_ue, env=env, timeout=7200, capture_output=True,
                              text=True, errors="replace")
        out = (proc.stdout or "") + (proc.stderr or "")
    finally:
        try: Path(job_path).unlink(missing_ok=True)
        except OSError: pass

    logs = renders / "logs"; logs.mkdir(exist_ok=True)
    (logs / f"bake_{name}_{dt.datetime.now():%Y%m%d_%H%M%S}.log").write_text(out, encoding="utf-8", errors="replace")
    for ln in out.splitlines():
        if "[AIPresenter]" in ln:
            log.info("UE| %s", ln.split("[AIPresenter]", 1)[1].strip())

    anim_path = None
    for ln in out.splitlines():
        if "BAKE OK anim=" in ln:
            anim_path = ln.split("BAKE OK anim=", 1)[1].strip()
            break
    if not anim_path:
        sys.exit("[fatal] bake did not report an AnimSequence — see the UE log above.")

    # write the sidecar so render.py uses it
    sidecar = get_path(config, "anim") / f"{name}.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps({
        "name": name, "audio": f"audio/{name}.wav", "ue_asset": anim_path,
        "asset_type": "anim_sequence", "source": "metahuman_audio_driven",
        "fps": u.get("render_fps", 30),
        "created": dt.datetime.now().isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")
    log.info("baked ✓  anim=%s", anim_path)
    log.info("sidecar written: %s", sidecar)
    log.info("next:  python pipeline/render.py --input %s", name)


if __name__ == "__main__":
    main()
