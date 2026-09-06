# Phase 3 — Render & assembly

Turns an approved clip into a finished 16:9 long and 9:16 shorts:

```
render.py (UE, presenter pass + alpha)  ->  composite.py (ffmpeg, over plate)  ->  shorts.py (ffmpeg + whisper)
```

`composite.py` and `shorts.py` are **built and tested**. `render.py` is built but
**depends on a presenter scene that doesn't exist in your project yet** — the
`AIPresenter` project is currently the ArchViz template. The one-time UE setup
below is yours to do; after that the whole phase runs from the command line.

---

## One-time UE setup (manual, once)

1. **Create the presenter level.** A map at **`/Game/Maps/Presenter`** (matches
   `config.yaml → unreal.presenter_level`) with:
   - your `NewMetaHumanCharacter` seated at a desk, lit;
   - a **CineCameraActor**, framed on the presenter, **locked** (transform not
     animated) — this is the "locked camera" the render uses;
   - **tag the presenter actor** with the tag **`presenter`** (Actor ▸ Tags) so
     `render.py --plate` can hide it when baking the empty background.
2. **Enable alpha** (required for a transparent presenter pass):
   Project Settings ▸ Rendering ▸ **Default Settings ▸ Alpha Output = ON**, then
   restart the editor. (In UE 5.5+ this checkbox — "Alfa Çıktısı" in Turkish —
   replaced the older `r.PostProcessing.PropagateAlpha` enum. Leave *Mobile Alpha
   Output* off.)
3. **Confirm the camera's label** and put it in `config.yaml → unreal.camera_actor`:
   ```bat
   python pipeline/render.py --list-cameras
   ```

> ⚠️ `pipeline/ue/render_job.py` uses the UE 5.8 Movie Render Queue Python API.
> The API is version-sensitive — validate the first render and tweak if a setting
> name differs in your build. Alpha PNG export in particular depends on step 2.

---

## Steady-state run order (per clip, from the command line)

Prereq once: **bake the background plate** (empty furnished set):
```bat
python pipeline/render.py --plate            :: -> renders/plate.png (render once)
```

Then per approved clip `<name>` (the wav stem):
```bat
:: 0. (Phase 2) link the baked facial anim so the face moves; else idle:
python pipeline/anim_utils.py --check <name>

:: 1. render the presenter pass with alpha (headless UE; frees VRAM on exit)
python pipeline/render.py --input <name>     :: -> renders/presenter/<name>/*.png (RGBA)

:: 2. composite presenter over the plate + mux the voice track
python pipeline/composite.py --input <name>  :: -> output/longs/<name>.mp4
::    optional graphics overlays:
::    python pipeline/composite.py --input <name> --graphics lower_third.png --graphics chart.png

:: 3. cut captioned 9:16 shorts from the long
python pipeline/shorts.py --input <name>     :: -> output/shorts/<name>_shortNN.mp4
```

---

## Render mode: scene (default) vs. transparent

`config.yaml → render.transparent_bg` controls this:

- **`false` (default, working):** the presenter is rendered **in the room** — that
  frame IS the final footage. `composite.py` just muxes the voice track (and any
  `--graphics` overlays on top). Reliable; proven end-to-end.
- **`true` (optional, not working yet):** presenter-only on a transparent
  background for compositing over `plate.png`. Blocked by a UE 5.8 headless alpha
  issue — MRQ won't emit alpha to PNG despite `PropagateAlpha` being forced every
  way (runtime cvar, `.ini`, `-dpcvars=…=2`). Revisit via the MRQ **GUI** render
  (to isolate headless-only) or **EXR** output. Until then, keep scene mode.

## Notes

- **VRAM (§2):** each `render.py` call is a separate `UnrealEditor-Cmd` process
  that exits when done, so the GPU is fully released before compositing. whisper in
  `shorts.py` runs on CUDA if available (verified working on the RTX 5060) and
  falls back to CPU automatically.
- **Idle fallback (§9):** if a clip has no baked facial animation linked in its
  `anim/<name>.json`, `render.py` renders the idle presenter instead of failing.
- **Graphics/charts:** pass PNG overlays to `composite.py --graphics`; they're
  layered over the presenter in order. (Data-driven chart generation can come later.)
- **Shorts selection** is currently a simple even spread of transcript windows;
  swap in an LLM ranker later if you want "best moment" selection.

## Sources
- [UE Movie Render Queue — Python/scripting](https://dev.epicgames.com/documentation/en-us/unreal-engine/rendering-high-quality-frames-with-movie-render-queue-in-unreal-engine)
- [UE — alpha/PropagateAlpha in post processing](https://dev.epicgames.com/documentation/en-us/unreal-engine/customizing-the-post-process-alpha-channel-in-unreal-engine)
