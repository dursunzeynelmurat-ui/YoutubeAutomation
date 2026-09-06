#!/usr/bin/env python3
"""render.py — headless Unreal render launcher (Phase 3).

This is the VENV-SIDE launcher. It validates inputs, resolves the baked facial
animation for a clip (via the anim/ sidecar), writes a job spec to JSON, and
launches UnrealEditor-Cmd.exe headless to run pipeline/ue/render_job.py INSIDE
Unreal's own Python (which has the `unreal` module but not this venv's packages).

Why split? UE's embedded Python can't import pyyaml/_common, so all parameters
are passed as a plain-JSON job file (read via the AIPRESENTER_JOB env var).

VRAM (§2): each render is a separate UnrealEditor-Cmd process that exits when
done, fully releasing the GPU before the next stage.

Modes:
    python pipeline/render.py --list-cameras          # discover camera labels in the level
    python pipeline/render.py --plate                 # render the empty set once -> renders/plate.png
    python pipeline/render.py --input <name>          # render presenter pass (alpha) for a clip

<name> is the clip base name (wav stem), e.g. 2026-09-06_your-topic-here.

NOTE: the in-UE render logic (render_job.py) depends on your presenter scene
existing (a seated-MetaHuman level + a locked camera + alpha enabled). See
docs/PHASE3_RENDER.md. Until that scene exists, --list-cameras is the useful mode.
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

from _common import get_path, load_config, resolve, setup_logging
from anim_utils import load_animation

log = setup_logging()

# UE editor often returns a non-zero exit code on a clean headless shutdown, so we
# judge success by markers / expected output, not by the exit code alone.
SUCCESS_MARKER = "render finished (success=True)"


def cmd_exe(config: dict) -> Path:
    engine = config["unreal"].get("engine_path", "")
    if not engine:
        sys.exit("[fatal] unreal.engine_path not set in config.yaml")
    exe = Path(engine) / "Engine" / "Binaries" / "Win64" / "UnrealEditor-Cmd.exe"
    if not exe.exists():
        sys.exit(f"[fatal] UnrealEditor-Cmd.exe not found at {exe}")
    return exe


def uproject(config: dict) -> str:
    p = Path(config["unreal"]["project_path"])
    if not p.exists():
        sys.exit(f"[fatal] .uproject not found: {p}")
    return str(p)


def run_ue(config: dict, job: dict, *, need_gpu: bool) -> tuple[int, str]:
    """Write the job JSON, launch UnrealEditor-Cmd, capture+log output.

    Returns (exit_code, combined_stdout). The full UE log is also saved under
    renders/logs/ for debugging. Callers judge success from markers/output, not
    the exit code (UE headless often returns non-zero on a clean quit).
    """
    job_dir = get_path(config, "renders")
    job_dir.mkdir(parents=True, exist_ok=True)
    fd, job_path = tempfile.mkstemp(prefix="render_job_", suffix=".json", dir=job_dir)
    os.close(fd)   # close the OS handle so Windows lets us delete it later
    job_file = Path(job_path)
    job_file.write_text(json.dumps(job, indent=2), encoding="utf-8")

    ue_script = Path(__file__).resolve().parent / "ue" / "render_job.py"
    if not ue_script.exists():
        sys.exit(f"[fatal] missing UE job script: {ue_script}")

    args = [str(cmd_exe(config)), uproject(config)]
    if need_gpu:
        # Async MRQ render: -ExecCmds runs the script but does NOT auto-exit the
        # editor (unlike -ExecutePythonScript), so the render can finish and our
        # on_finished callback quits the editor itself.
        args.append(f'-ExecCmds=py {ue_script}')   # path has no spaces; inner quotes break UE parsing
        # Force the read-only alpha cvar at startup (=2 keeps alpha through the
        # tonemapper). -dpcvars applies before the editor can overwrite the .ini.
        args.append("-dpcvars=r.PostProcessing.PropagateAlpha=2")
    else:
        # Synchronous discovery: the script quits the editor itself.
        args.append(f'-ExecutePythonScript={ue_script}')
    args += ["-unattended", "-nosplash", "-nopause", "-stdout", "-FullStdOutLogOutput"]
    if not need_gpu:
        args.append("-nullrhi")   # discovery mode doesn't need the GPU/RHI

    env = dict(os.environ, AIPRESENTER_JOB=str(job_file))
    log.info("launching Unreal (%s) — this boots the editor headless, please wait…", job["mode"])
    try:
        proc = subprocess.run(args, env=env, timeout=job.get("timeout", 7200),
                              capture_output=True, text=True, errors="replace")
        out = (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
        log.error("Unreal timed out after %ss.", job.get("timeout"))
        rc = 124
    finally:
        try:
            job_file.unlink(missing_ok=True)
        except OSError:
            pass  # cleanup must never mask the render result

    # Save full log + echo the pipeline's own markers.
    logs = job_dir / "logs"
    logs.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = logs / f"{job['mode']}_{stamp}.log"
    log_path.write_text(out, encoding="utf-8", errors="replace")
    for line in out.splitlines():
        if "[AIPresenter]" in line:
            log.info("UE| %s", line.split("[AIPresenter]", 1)[1].strip())
    log.info("(full UE log: %s)", log_path)
    return rc, out


def main() -> None:
    ap = argparse.ArgumentParser(description="Headless Unreal render launcher (Phase 3).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--input", metavar="NAME", help="render presenter pass for a clip (wav stem)")
    g.add_argument("--plate", action="store_true", help="render the empty set once -> renders/plate.png")
    g.add_argument("--list-cameras", action="store_true", help="list camera actor labels in the level")
    ap.add_argument("--max-frames", type=int, help="cap frames rendered (quick quality tests)")
    ap.add_argument("--config", help="path to config.yaml")
    args = ap.parse_args()

    config = load_config(args.config)
    u = config["unreal"]
    r = config["render"]
    renders_dir = get_path(config, "renders")

    if args.list_cameras:
        job = {"mode": "list_cameras", "level": u["presenter_level"], "timeout": 1200}
        _, out = run_ue(config, job, need_gpu=False)
        cams = [ln.split("label=", 1)[1] for ln in out.splitlines()
                if "[AIPresenter]" in ln and "label=" in ln]
        if cams:
            log.info("found %d camera(s). Set the presenter one in config.yaml -> unreal.camera_actor:", len(cams))
            for c in cams:
                log.info("   %s", c.strip())
            sys.exit(0)
        log.error("no cameras found — check the level path and that cameras exist.")
        sys.exit(1)

    if args.plate:
        plate_file = resolve(r["plate_file"])
        job = {
            "mode": "plate",
            "level": u["presenter_level"],
            "camera_actor": u["camera_actor"],
            "resolution": r["resolution"],
            "output_dir": str(renders_dir),
            "plate_file": str(plate_file),
            "fps": u.get("render_fps", 30),
            "timeout": 3600,
        }
        rc, out = run_ue(config, job, need_gpu=True)
        ok = SUCCESS_MARKER in out or list(renders_dir.glob("plate*.png"))
        if ok:
            log.info("plate rendered under %s (rename the frame to %s if needed).",
                     renders_dir, plate_file.name)
            sys.exit(0)
        log.error("plate render failed (exit %d) — see the UE log above.", rc)
        sys.exit(1)

    # --- presenter render for a clip -----------------------------------------
    name = Path(args.input).stem
    anim = load_animation(config, name)
    sequence = ""       # a ready-made Level Sequence to render directly
    anim_sequence = ""  # an AnimSequence to play on the MetaHuman's face (lip-sync)
    performance = ""    # a MetaHuman Performance asset (pipeline exports the LS from it)
    if anim and anim.get("asset_type") == "performance":
        performance = anim["ue_asset"]
        log.info("using MetaHuman Performance (auto-export lip-sync): %s", performance)
    elif anim and anim.get("asset_type") == "level_sequence":
        sequence = anim["ue_asset"]
        log.info("using baked LEVEL sequence: %s", sequence)
    elif anim and anim.get("asset_type") == "anim_sequence":
        anim_sequence = anim["ue_asset"]
        log.info("using baked FACIAL animation (lip-sync): %s", anim_sequence)
    else:
        log.warning("no baked animation for '%s' — rendering IDLE presenter (§9). "
                    "Bake facial anim and link it via anim_utils.py to animate the face.", name)

    # Render length matches the voice track, so composite lines up 1:1.
    fps = u.get("render_fps", 30)
    num_frames = 0
    audio = get_path(config, "audio") / f"{name}.wav"
    if not sequence and audio.exists():
        try:
            import soundfile as sf
            num_frames = max(1, round(sf.info(str(audio)).duration * fps))
            log.info("render length from %s: %d frames (%.1fs @ %dfps)",
                     audio.name, num_frames, num_frames / fps, fps)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read audio duration (%s) — render_job will use a default.", exc)
    if args.max_frames and num_frames:
        num_frames = min(num_frames, args.max_frames)
        log.info("capping to %d frames (--max-frames)", num_frames)

    out_dir = get_path(config, "renders") / "presenter" / name
    job = {
        "mode": "presenter",
        "level": u["presenter_level"],
        "camera_actor": u["camera_actor"],
        "sequence": sequence,
        "anim_sequence": anim_sequence,
        "performance": performance,
        "num_frames": num_frames,
        "transparent_bg": bool(r.get("transparent_bg", False)),
        "path_tracer": bool(r.get("path_tracer", False)),
        "lumen_hardware_rt": bool(r.get("lumen_hardware_rt", False)),
        "pt_spatial_samples": r.get("pt_spatial_samples", 36),
        "pt_spp": r.get("pt_spp", 64),
        "pt_max_bounces": r.get("pt_max_bounces", 4),
        "resolution": r["resolution"],
        "output_dir": str(out_dir),
        "output_format": r.get("output_format", "png"),
        "fps": fps,
        "warmup_frames": r.get("warmup_frames", 32),
        "spatial_samples": r.get("spatial_samples", 1),
        "temporal_samples": r.get("temporal_samples", 4),
        "name": name,
        "timeout": 14400,
    }
    rc, out = run_ue(config, job, need_gpu=True)
    pngs = list(out_dir.glob("*.png")) if out_dir.exists() else []
    if pngs or SUCCESS_MARKER in out:
        log.info("render OK — %d frame(s) in %s", len(pngs), out_dir)
        if not pngs:
            log.warning("success reported but no PNGs found — check output_dir / alpha settings.")
        sys.exit(0)
    log.error("render failed (exit %d, no frames) — see the UE log above "
              "(scene/camera/alpha setup).", rc)
    sys.exit(1)


if __name__ == "__main__":
    main()
