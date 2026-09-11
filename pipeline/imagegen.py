#!/usr/bin/env python3
"""imagegen.py — local SDXL-Turbo still generation for the long-form visual track.

Blackwell (RTX 5060, 8 GB): fp16 + model CPU-offload keeps SDXL-Turbo within budget.
The pipeline is loaded once, used for all prompts, then released (frees VRAM for ffmpeg
/ the next stage), matching the rest of the pipeline's sequential VRAM discipline.
"""
from __future__ import annotations

import gc
import os
import subprocess
import tempfile
from pathlib import Path

from _common import setup_logging

log = setup_logging()

# Dark cinematic horror look, applied to every prompt for cohesion.
STYLE = ("dark cinematic horror still, moody volumetric lighting, deep shadows, muted "
         "desaturated colors, eerie atmosphere, film grain, photorealistic, highly detailed, "
         "35mm, shallow depth of field")
NEG = ("text, watermark, signature, logo, caption, bright cheerful, cartoon, anime, "
       "low quality, blurry, deformed, extra limbs, meme")

# For stills that will be ANIMATED (SVD): keep them simple and faceless. SVD morphs
# fine detail and especially human faces into uncanny artifacts, so hero-shot images
# use a wide, low-detail, no-close-up-face composition (distant figures/silhouettes ok).
MOTION_STYLE = ("wide atmospheric establishing shot, distant subject, environment and scenery focus, "
                "simple composition, minimal fine detail, soft focus, empty space")
MOTION_NEG = ("close-up face, facial features, portrait, detailed face, human face, people in "
              "foreground, hands, fingers, intricate detail, busy composition")


def _release(pipe):
    try:
        import torch
        del pipe
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        gc.collect()


def generate_images(prompts, out_dir: Path, cfg: dict, seed: int = 1234,
                    motion_safe: bool = False) -> list[Path]:
    """Generate one PNG per prompt with SDXL-Turbo. Returns the written paths (in order).
    motion_safe=True → wide, low-detail, faceless composition for images destined for SVD."""
    import torch
    from diffusers import AutoPipelineForText2Image

    models_dir = cfg.get("models_dir", "models")
    os.environ.setdefault("HF_HOME", str(Path(models_dir).resolve()))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")   # avoid flaky xet CAS backend (SVD download errors)
    out_dir.mkdir(parents=True, exist_ok=True)

    w = int(cfg.get("image_w", 1024)); h = int(cfg.get("image_h", 576))
    steps = int(cfg.get("image_steps", 4)); guidance = float(cfg.get("image_guidance", 0.0))
    model = cfg.get("image_model", "stabilityai/sdxl-turbo")

    log.info("loading %s (fp16, CPU-offload) — first run downloads weights…", model)
    pipe = AutoPipelineForText2Image.from_pretrained(
        model, torch_dtype=torch.float16, variant="fp16", cache_dir=models_dir)
    try:
        pipe.enable_model_cpu_offload()          # keeps 8 GB VRAM safe
    except Exception:  # noqa: BLE001
        pipe.to("cuda" if torch.cuda.is_available() else "cpu")
    try:
        pipe.set_progress_bar_config(disable=True)
    except Exception:  # noqa: BLE001
        pass

    style = f"{MOTION_STYLE}, {STYLE}" if motion_safe else STYLE
    neg = f"{NEG}, {MOTION_NEG}" if motion_safe else NEG
    paths = []
    for i, prompt in enumerate(prompts, 1):
        g = torch.Generator(device="cpu").manual_seed(seed + i)
        full = f"{style}, {prompt.strip()}"          # style FIRST so CLIP's 77-token cap can't drop it
        img = pipe(prompt=full, negative_prompt=neg, num_inference_steps=steps,
                   guidance_scale=guidance, width=w, height=h, generator=g).images[0]
        p = out_dir / f"scene_{i:03d}.png"
        img.save(p)
        paths.append(p)
        log.info("  image %d/%d -> %s", i, len(prompts), p.name)

    _release(pipe)
    return paths


def animate_image(image_path: Path, out_mp4: Path, cfg: dict, seed: int = 42) -> Path:
    """Phase 2 (D): animate a still into a short motion clip with Stable Video Diffusion.
    Heavy on 8 GB — fp16 + CPU-offload + small decode chunks. First run downloads SVD weights.
    Returns the encoded mp4 path. Raises on failure (caller falls back to a Ken Burns still)."""
    import torch
    from diffusers import StableVideoDiffusionPipeline
    from diffusers.utils import load_image

    models_dir = cfg.get("models_dir", "models")
    os.environ.setdefault("HF_HOME", str(Path(models_dir).resolve()))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")   # avoid flaky xet CAS backend (SVD download errors)
    model = cfg.get("svd_model", "stabilityai/stable-video-diffusion-img2vid-xt")
    nframes = int(cfg.get("svd_frames", 25))
    fps = int(cfg.get("svd_fps", 8))
    motion = int(cfg.get("svd_motion", 90))
    noise = float(cfg.get("svd_noise", 0.02))
    chunk = int(cfg.get("svd_decode_chunk", 2))
    w = int(cfg.get("image_w", 1024)); h = int(cfg.get("image_h", 576))

    log.info("loading SVD (%s) — first run downloads weights…", model)
    pipe = StableVideoDiffusionPipeline.from_pretrained(model, torch_dtype=torch.float16,
                                                        variant="fp16", cache_dir=models_dir)
    pipe.enable_model_cpu_offload()
    try:
        pipe.unet.enable_forward_chunking()
    except Exception:  # noqa: BLE001
        pass

    img = load_image(str(image_path)).resize((w, h))
    gen_ = torch.manual_seed(seed)
    frames = pipe(img, decode_chunk_size=chunk, num_frames=nframes, motion_bucket_id=motion,
                  noise_aug_strength=noise, generator=gen_).frames[0]
    _release(pipe)

    # encode frames -> mp4 via ffmpeg (avoids extra imageio deps)
    tmp = Path(tempfile.mkdtemp(prefix="svd_"))
    try:
        for i, fr in enumerate(frames):
            fr.save(tmp / f"f_{i:03d}.png")
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["ffmpeg", "-y", "-framerate", str(fps), "-i", str(tmp / "f_%03d.png"),
               "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(out_mp4)]
        if subprocess.run(cmd, capture_output=True).returncode != 0:
            raise RuntimeError("ffmpeg encode of SVD frames failed")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    log.info("  animated %s -> %s (%d frames @ %dfps)", image_path.name, out_mp4.name, len(frames), fps)
    return out_mp4
