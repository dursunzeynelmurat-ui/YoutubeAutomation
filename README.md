# AI Presenter — automation pipeline

Fully-local content pipeline for a finance-niche channel. **All 4 phases work.**
Two product tracks share the same script+voice engine:

**A. Brainrot shorts (fast, high-volume — no GPU render):**
```
brainrot.py --topic "..."   # generate (rotating hooks) → voice (rotating am_adam/am_liam)
                            → word captions over gameplay → 1080x1920 short + SEO sidecar
upload.py --input <clip>    # → YouTube (private by default)
```
**B. MetaHuman presenter (premium, slow — headless UE render + lip-sync):**
```
generate.py → approve → speak.py → bake.py → render.py → composite.py → shorts.py → upload.py
```
Everything runs locally; the only network use is first-time model downloads and
the YouTube upload. Per-phase details: [docs/PHASE2_AUDIO2FACE.md](docs/PHASE2_AUDIO2FACE.md),
[docs/PHASE3_RENDER.md](docs/PHASE3_RENDER.md), [docs/PHASE4_UPLOAD.md](docs/PHASE4_UPLOAD.md).

**The lip-sync path is the fiddly part — it's documented in full in
[docs/PHASE2_AUDIO2FACE.md](docs/PHASE2_AUDIO2FACE.md)** (including the UE 5.8
"Force Custom Mode" fix that `render.py` applies automatically).

---

## Machine target

- Windows, **RTX 5060 Laptop, 8 GB VRAM** (Blackwell `sm_120`), 32 GB RAM.
- The 8 GB limit is the key design fact: this is a **sequential** pipeline — one
  heavy model resident at a time, and each stage releases its VRAM on exit.

---

## Project layout note

This `automation/` folder lives **outside** the Unreal project on purpose. The
UE project is OneDrive-synced:

- **UE project:** `C:\Users\dursu\OneDrive\Documents\Unreal Projects\AIPresenter\AIPresenter.uproject`
- **Automation code:** `C:\Users\dursu\AIPresenter\automation\` (this folder — NOT in OneDrive)

Keeping them separate stops OneDrive from syncing the multi-GB `.venv`, `renders/`,
`output/`, and model caches. Phase 3's `render.py` reaches the project via
`unreal.project_path` in [config.yaml](config.yaml) — the code does not need to sit
inside the project. (Note: that project folder also contains a second
`ArchVisRT.uproject`; the pipeline uses `AIPresenter.uproject`.)

## Install (do the steps in this order)

From `automation/`:

```bat
:: 1. Create the virtual environment (Python 3.11)
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip

:: 2. Install PyTorch FIRST from the CUDA 12.8 index (required for RTX 5060 / Blackwell)
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128

:: 3. Install the rest
pip install -r requirements.txt

:: 4. Verify the GPU is visible to PyTorch — MUST print True
python -c "import torch; print(torch.cuda.is_available())"
```

> **If step 4 prints `False`:** a TTS package likely pulled in a non-Blackwell
> torch. Reinstall the CUDA 12.8 build over it:
> `pip install --force-reinstall torch torchaudio --index-url https://download.pytorch.org/whl/cu128`

### Ollama models

```bat
ollama pull qwen2.5:14b-instruct   :: primary (~9 GB; uses CPU offload on 8 GB — slow but fine for batch)
ollama pull llama3.1:8b            :: fallback (already installed on this machine)
```

### Kokoro extra (only if you use the Kokoro TTS engine)

Kokoro needs **espeak-ng** for words outside its dictionary. Install it from
<https://github.com/espeak-ng/espeak-ng/releases> (Windows installer), or skip it
if you only use Chatterbox.

---

## Configuration

All tunables live in [`config.yaml`](config.yaml) — no hardcoded paths in scripts.
Key settings:

| Setting | Meaning |
|---|---|
| `llm.primary` / `llm.fallback` | Ollama models; fallback used automatically if primary is missing |
| `content.language` | `en` or `tr` (override per run with `--language`) |
| `content.disclaimers` | the "not financial advice" text (per language) prepended to every script |
| `tts.engine` | `chatterbox` (English **+ Turkish**) or `kokoro` (English only, lighter) |
| `tts.chatterbox.voice_sample` | optional reference `.wav` for voice cloning |

**Language ↔ engine:** Turkish requires `chatterbox`. Kokoro will refuse `tr`.
For Turkish scripts you may also prefer an Ollama model tuned for Turkish, e.g.
`--model aya-expanse:8b` or `--model "hf.co/ytu-ce-cosmos/Turkish-Llama-8b-Instruct-v0.1-GGUF:Q4_K_M"`.

---

## Running Phase 1

Activate the venv first (`.venv\Scripts\activate`), then run from `automation/`.

### 1. Generate a script draft

```bat
python pipeline/generate.py --topic "Why index funds usually beat stock picking"
```

- Reads any files you dropped in `content/data/` (`.txt/.md/.csv/.json`) and feeds
  them to the model as the **only** allowed source of figures (accuracy gate). With
  no data files it stays conceptual and avoids specific numbers.
- Writes a dated draft to `content/scripts/YYYY-MM-DD_<slug>.md` with the
  disclaimer prepended.
- **Does not approve anything.** Options: `--language tr`, `--model <name>`,
  `--data-dir <path>`.

### 2. Approve it (human gate)

Review the draft. When you're happy, copy it into `content/approved/`:

```bat
copy "content\scripts\2026-09-06_why-index-funds-usually-beat-stock-picking.md" "content\approved\"
```

Nothing gets voiced until it lives in `content/approved/`.

### 3. Produce the voice `.wav`

```bat
python pipeline/speak.py --input 2026-09-06_why-index-funds-usually-beat-stock-picking.md
```

- `--input` takes the filename (looked up in `content/approved/`) or a full path.
  A file still in `content/scripts/` is refused with instructions to approve it.
- Writes `audio/<name>.wav`. Options: `--engine kokoro|chatterbox`, `--language tr`,
  `--voice <id-or-ref.wav>`, `--output <path>`.

The first run of either TTS engine downloads its model weights from Hugging Face
(one-time). 14b generation and Chatterbox both load/unload the GPU per run.

---

## Safety rules enforced (PIPELINE.md §8)

- **Scripts are never auto-approved.** `generate.py` only ever writes to
  `content/scripts/`; `speak.py` only reads from `content/approved/`.
- **Every script carries the "not financial advice" disclaimer** (prepended at
  generation, and spoken as part of the voice track).
- **Data-grounded:** the model is fed real data from `content/data/` and told not
  to invent figures.
- No secrets in code; `.gitignore` covers `.venv`, tokens, and generated media.

---

## Phase 2 — Facial animation (manual, in Unreal)

The in-editor wiring is a **manual** job (PIPELINE.md §7). The full checklist —
Epic MetaHuman Animator (recommended) vs. NVIDIA Audio2Face-3D — is in
[docs/PHASE2_AUDIO2FACE.md](docs/PHASE2_AUDIO2FACE.md).

The pipeline's part is the **`anim/` handoff contract**: each clip gets a sidecar
`anim/<name>.json` naming the baked UE animation asset, managed by
[`pipeline/anim_utils.py`](pipeline/anim_utils.py):

```bat
python pipeline/anim_utils.py --scaffold <name>   :: <name> = the wav stem
:: bake the face anim in Unreal, then set "ue_asset" in anim/<name>.json
python pipeline/anim_utils.py --check <name>       :: READY, or idle-fallback
python pipeline/anim_utils.py --list
```

Phase 3's `render.py` will call `anim_utils.load_animation()`; a clip with no
baked asset degrades gracefully to the **idle presenter** (§9).

## Phase 3 — Render & assembly

Full guide: [docs/PHASE3_RENDER.md](docs/PHASE3_RENDER.md). Run order per clip:

```bat
python pipeline/render.py --plate            :: once: bake the background plate
python pipeline/render.py --input <name>     :: presenter pass w/ alpha (headless UE)
python pipeline/composite.py --input <name>  :: presenter over plate + voice -> output/longs/
python pipeline/shorts.py --input <name>     :: captioned 9:16 shorts -> output/shorts/
```

- `composite.py` (ffmpeg) and `shorts.py` (ffmpeg + faster-whisper, GPU-accelerated)
  are **built and tested**.
- `render.py` is built but needs a **presenter scene** you create in UE first
  (seated MetaHuman + locked camera + alpha enabled) — see the Phase 3 doc.
  Discover the camera name with `python pipeline/render.py --list-cameras`.

## Not built yet

- **Phase 4 — Publish.** `upload.py` (YouTube Data API v3, uploads **private** by
  default). The `youtube:` block in `config.yaml` is a placeholder until then.
