# AI Presenter — automation pipeline

Fully-local, gameplay-background short-form video pipeline (no GPU render). One
script+voice+caption engine drives **two channels**:

**A. Finance brainrot shorts** ([config.yaml](config.yaml)):
```
brainrot.py --topic "..."   # generate (rotating hooks) → voice (rotating am_adam/am_liam)
                            → word captions over gameplay → 1080x1920 short + SEO sidecar
upload.py --input <clip>    # → YouTube (private by default)
```

**B. Reddit-story shorts** ([config.reddit.yaml](config.reddit.yaml)) — narrated AITA/
nosleep/TIFU stories with a Reddit post-card, moods, multi-part splitting, and a
Subscribe/🔔 CTA. Full guide: [docs/REDDIT_CHANNEL.md](docs/REDDIT_CHANNEL.md).
```
redditstory.py --auto --count 3 --config config.reddit.yaml
```

Everything runs locally; the only network use is first-time model downloads, the
Reddit RSS fetch, and the YouTube upload. Upload details: [docs/PHASE4_UPLOAD.md](docs/PHASE4_UPLOAD.md).

---

## Machine target

- Windows, **RTX 5060 Laptop, 8 GB VRAM** (Blackwell `sm_120`), 32 GB RAM.
- The 8 GB limit is the key design fact: this is a **sequential** pipeline — one
  heavy model resident at a time, and each stage releases its VRAM on exit.

---

## Project layout note

Code + venv: `C:\Users\dursu\AIPresenter\automation\` — kept **outside** OneDrive on
purpose so the multi-GB `.venv/`, `output/`, and model caches aren't sync-churned.

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

## Brainrot shorts (the fast path)

```bat
:: one short from a topic (generate → voice → captions → gameplay → SEO)
python pipeline/brainrot.py --topic "Why index funds usually beat stock picking"
:: batch a file of topics (one per line)
python pipeline/brainrot.py --topics topics.txt
:: upload a finished short (private by default)
python pipeline/upload.py --input <clip>
```

Gameplay loops go in `content/gameplay/` (licensed / no-copyright only). Each short
gets an SEO sidecar (`.json`/`.txt`: title, description with CTA + hashtags +
disclaimer, tags).

## Reddit-story channel

See **[docs/REDDIT_CHANNEL.md](docs/REDDIT_CHANNEL.md)** for the full second-channel
guide (RSS fetch, moods, multi-part, post-card, music/SFX, playlists, scheduler,
weekly compilation, and the private-upload queue).

## Publishing

`upload.py` uses the YouTube Data API v3 and uploads **private by default**
(`--privacy public` is explicit and deliberate). One-time OAuth setup:
[docs/PHASE4_UPLOAD.md](docs/PHASE4_UPLOAD.md).
