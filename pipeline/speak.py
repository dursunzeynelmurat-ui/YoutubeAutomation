#!/usr/bin/env python3
"""speak.py — approved script -> voice .wav (Phase 1, content engine).

Reads a human-APPROVED script from content/approved/ and synthesizes one
consistent voice track into audio/<name>.wav, using the TTS engine selected in
config.yaml (chatterbox or kokoro).

Safety (PIPELINE.md §8):
  * Enforces the human gate — the input file MUST live in content/approved/.
    Pointing at content/scripts/ is refused, so an unreviewed draft can never be
    voiced by accident.

VRAM (§2): the model is loaded, used, then explicitly released (del + empty_cache)
so the GPU is free for the next stage.

Language note: Turkish (tr) requires engine=chatterbox — Kokoro is English only.

Usage:
    python pipeline/speak.py --input 2026-09-06_my-topic.md
    python pipeline/speak.py --input my-topic.md --engine kokoro
    python pipeline/speak.py --input my-topic.md --language tr   # forces chatterbox path
"""
from __future__ import annotations

import argparse
import gc
import re
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from _common import get_path, load_config, resolve, setup_logging

log = setup_logging()


# --------------------------------------------------------------------------- #
# Text preparation                                                            #
# --------------------------------------------------------------------------- #
def clean_for_tts(raw: str) -> str:
    """Strip markdown / frontmatter so only spoken words remain.

    The disclaimer (plain text at the top) is intentionally KEPT and spoken.
    """
    text = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)   # HTML comment metadata
    lines = [ln for ln in text.splitlines() if ln.strip() != "---"]  # md separators
    text = "\n".join(lines)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)       # headings
    text = re.sub(r"[*_`>]", "", text)                              # md emphasis/quotes
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, max_chars: int = 500) -> list[str]:
    """Group sentences into chunks under max_chars (keeps TTS inputs bounded)."""
    sentences = re.split(r"(?<=[.!?…])\s+", text.replace("\n", " "))
    chunks, current = [], ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if len(current) + len(s) + 1 <= max_chars:
            current = f"{current} {s}".strip()
        else:
            if current:
                chunks.append(current)
            current = s
    if current:
        chunks.append(current)
    return chunks or [text]


# --------------------------------------------------------------------------- #
# Engines — each returns a mono float32 numpy array at the given sample rate    #
# and releases its model/VRAM before returning.                               #
# --------------------------------------------------------------------------- #
def _release(model) -> None:
    try:
        import torch
        del model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001 — cleanup must never crash the run
        gc.collect()


def synth_chatterbox(text: str, language: str, cfg: dict, sample_rate: int) -> tuple[np.ndarray, int]:
    import torch
    device = cfg.get("device", "cuda")
    if device == "cuda" and not torch.cuda.is_available():
        log.warning("CUDA not available — falling back to CPU (slow).")
        device = "cpu"

    ref = cfg.get("voice_sample") or ""
    ref_path = str(resolve(ref)) if ref else None
    if ref_path and not Path(ref_path).exists():
        log.warning("voice_sample '%s' not found — using default voice.", ref_path)
        ref_path = None

    gen_kwargs = {}
    if ref_path:
        gen_kwargs["audio_prompt_path"] = ref_path
    if "exaggeration" in cfg:
        gen_kwargs["exaggeration"] = cfg["exaggeration"]
    if "cfg_weight" in cfg:
        gen_kwargs["cfg_weight"] = cfg["cfg_weight"]

    if language == "tr":
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        log.info("loading Chatterbox multilingual model on %s (language=tr)…", device)
        model = ChatterboxMultilingualTTS.from_pretrained(device=device)
        gen_kwargs["language_id"] = "tr"
    else:
        from chatterbox.tts import ChatterboxTTS
        log.info("loading Chatterbox model on %s…", device)
        model = ChatterboxTTS.from_pretrained(device=device)

    sr = getattr(model, "sr", sample_rate)
    pieces = []
    chunks = chunk_text(text)
    for i, chunk in enumerate(chunks, 1):
        log.info("  synth chunk %d/%d (%d chars)", i, len(chunks), len(chunk))
        wav = model.generate(chunk, **gen_kwargs)          # torch tensor (1, N)
        pieces.append(wav.squeeze(0).detach().cpu().numpy())

    _release(model)
    return np.concatenate(pieces), sr


def synth_kokoro(text: str, language: str, cfg: dict, sample_rate: int) -> tuple[np.ndarray, int]:
    if language != "en":
        sys.exit(
            f"[fatal] Kokoro is English-only but language='{language}'. "
            f"Use engine=chatterbox for Turkish (set tts.engine or pass --engine chatterbox)."
        )
    from kokoro import KPipeline
    from _common import rotate_pick
    device = cfg.get("device", "cuda")
    # Voice: explicit --voice locks it; else rotate through cfg['voices'] in turns; else single voice.
    if cfg.get("voice_locked"):
        voice = cfg.get("voice", "af_heart")
    elif cfg.get("voices"):
        voice = rotate_pick("kokoro_voice", list(cfg["voices"]))
    else:
        voice = cfg.get("voice", "af_heart")
    speed = float(cfg.get("speed", 1.0))
    log.info("loading Kokoro pipeline (voice=%s, speed=%.2f)…", voice, speed)
    try:
        pipeline = KPipeline(lang_code="a", device=device)   # 'a' = American English
    except TypeError:
        pipeline = KPipeline(lang_code="a")                  # older kokoro w/o device kw

    # Optional pause inserted between Kokoro segments (~sentences) for pacing/drama.
    gap_ms = int(cfg.get("segment_gap_ms", 0) or 0)
    gap = np.zeros(int(24000 * gap_ms / 1000), dtype=np.float32) if gap_ms > 0 else None
    pieces = []
    for _, _, audio in pipeline(text, voice=voice, speed=speed):
        arr = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
        pieces.append(np.asarray(arr, dtype=np.float32))
        if gap is not None:
            pieces.append(gap)
    _release(pipeline)
    if not pieces:
        sys.exit("[fatal] Kokoro produced no audio.")
    return np.concatenate(pieces), 24000   # Kokoro emits 24 kHz


ENGINES = {"chatterbox": synth_chatterbox, "kokoro": synth_kokoro}


# --------------------------------------------------------------------------- #
def resolve_approved_input(config: dict, raw: str) -> Path:
    """Resolve --input to a file INSIDE content/approved/ (enforces the §8 gate)."""
    approved = get_path(config, "approved").resolve()
    scripts = get_path(config, "scripts").resolve()

    candidate = Path(raw)
    path = candidate if candidate.is_absolute() else (approved / candidate)
    path = path.resolve()

    if not path.exists():
        # Helpful nudge if the file is still sitting unreviewed in scripts/.
        in_scripts = (scripts / Path(raw).name)
        if in_scripts.exists():
            sys.exit(
                f"[fatal] '{Path(raw).name}' is still in content/scripts/ (not reviewed).\n"
                f"        Approve it first:\n"
                f'            copy "{in_scripts}" "{approved}"\n'
                f"        then re-run speak.py."
            )
        sys.exit(f"[fatal] input not found in content/approved/: {path}")

    try:
        path.relative_to(approved)
    except ValueError:
        sys.exit(
            f"[fatal] refusing to voice a file outside content/approved/ (§8 human gate):\n"
            f"        {path}\n"
            f"        Only approved scripts may be voiced."
        )
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Synthesize an approved script to a .wav.")
    ap.add_argument("--input", required=True,
                    help="approved script filename (in content/approved/) or path")
    ap.add_argument("--engine", choices=list(ENGINES), help="override tts.engine")
    ap.add_argument("--language", help="override content.language (en|tr)")
    ap.add_argument("--voice", help="override the engine's voice / reference sample")
    ap.add_argument("--output", help="output .wav path (default audio/<name>.wav)")
    ap.add_argument("--config", help="path to config.yaml")
    args = ap.parse_args()

    config = load_config(args.config)
    in_path = resolve_approved_input(config, args.input)

    language = (args.language or config["content"]["language"]).lower()
    if language not in ("en", "tr"):
        sys.exit(f"[fatal] unsupported language '{language}' (use en or tr).")

    engine = args.engine or config["tts"]["engine"]
    if engine not in ENGINES:
        sys.exit(f"[fatal] unknown tts engine '{engine}' (chatterbox|kokoro).")

    sample_rate = config["tts"].get("sample_rate", 24000)
    engine_cfg = dict(config["tts"].get(engine, {}))
    if args.voice:   # --voice overrides ref sample (chatterbox) or voice id (kokoro)
        engine_cfg["voice_sample" if engine == "chatterbox" else "voice"] = args.voice
        engine_cfg["voice_locked"] = True   # explicit voice skips rotation

    text = clean_for_tts(in_path.read_text(encoding="utf-8"))
    if not text:
        sys.exit(f"[fatal] no speakable text in {in_path}")

    log.info("engine=%s  language=%s  input=%s  (~%d words)",
             engine, language, in_path.name, len(text.split()))
    start = time.time()
    audio, sr = ENGINES[engine](text, language, engine_cfg, sample_rate)

    audio_dir = get_path(config, "audio")
    audio_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.output).resolve() if args.output else (audio_dir / f"{in_path.stem}.wav")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    audio = np.asarray(audio, dtype=np.float32)
    sf.write(str(out_path), audio, sr)

    dur = len(audio) / sr
    log.info("wrote %s  (%.1fs audio, %d Hz) in %.0fs", out_path, dur, sr, time.time() - start)


if __name__ == "__main__":
    main()
