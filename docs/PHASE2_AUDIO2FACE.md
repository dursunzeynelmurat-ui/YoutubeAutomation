# Phase 2 — Facial animation (manual, in Unreal)

Phase 2 is **done by you inside Unreal Engine 5.8**, not by pipeline code
(PIPELINE.md §7). This document is the checklist. The pipeline's only code role is
the **`anim/` handoff contract** (see the last section) that Phase 3's `render.py`
will consume.

**Goal:** turn an approved voice `.wav` (from `audio/`) into a **baked facial
animation asset** on the MetaHuman, so Phase 3 can render it headlessly and
deterministically through the locked camera.

> ⚠️ Everything here runs **locally** (no cloud). Verify exact menu names against
> your installed plugin versions — NVIDIA's ACE UI shifts between releases.

## What is manual vs. automated

- **One-time setup (manual, done once):** enabling plugins, building/placing the
  MetaHuman, wiring the face AnimBP (A2F path), placing + locking the camera,
  dressing the set. Editor GUI work that can't be scripted — but you do it once.
- **Per-clip audio → facial-anim bake (automated in Phase 3):** importing the wav,
  running the performance/solve, exporting the sequence, writing the sidecar. This
  runs **headless via the `unreal` Python API** alongside `render.py` — you do NOT
  do it by hand per video.

**The step-by-step below is a one-time manual dry-run** so you can confirm the rig
produces good lip-sync before Phase 3 automates it. Caveat: the `unreal` API cleanly
handles audio import and sequence export/render, but the "Process/solve" step has
weaker Python coverage in some UE versions — Phase 3 will script whatever 5.8
exposes and fall back to a manual bake only where it genuinely can't.

---

## Two ways to do it — pick one

| | **A. Epic MetaHuman Animator — Audio-Driven Animation** | **B. NVIDIA Audio2Face-3D (ACE plugin)** |
|---|---|---|
| Fit for this pipeline | ★ **Recommended** — offline, bakes straight to an asset | Works, but streaming-oriented (drives the face live) |
| Runs locally | Yes, fully offline | Yes, on-device models |
| Output | Animation Sequence **or** Level Sequence (directly exportable) | Live ARKit curves; baking to a sequence is extra work |
| Extra installs | MetaHuman Animator plugin (ships with UE) | NVIDIA ACE plugin **+** Audio2Face-3D Models plugin |
| Why it matters | MRQ needs a baked asset to render unattended; A is built for that | Great for real-time, less so for batch rendering |

The spec names Audio2Face-3D, so **B** is documented for completeness — but for a
**batch, headless, twice-a-week render**, **A** is the cleaner path because it
produces a bakeable asset from a wav in one offline step. Choose per your needs;
both feed the same `anim/` contract below.

---

## Path A — Epic MetaHuman Animator (recommended)

Requires **UE 5.6+** (you're on 5.8) with the **MetaHuman Animator** plugin
enabled. Runs offline on your machine.

1. **Import the voice wav** into the project as a **SoundWave** (drag
   `audio/<name>.wav` into the Content Browser).
2. Content Browser → right-click → **MetaHuman Animator ▸ MetaHuman Performance**.
3. Open the Performance asset, in **Details**:
   - **Input Type = Audio**
   - **Audio** = your imported SoundWave
   - **Visualization Mesh** = your MetaHuman face
   - (optional) adjust head movement, blinks, mood.
4. Click **Process** to generate the facial animation.
5. **Export**:
   - **Export Animation Sequence** → an Anim Sequence asset, **or**
   - **Export Level Sequence** (assign your MetaHuman blueprint) — best for MRQ.
6. Note the exported asset's **content path** (e.g.
   `/Game/AIPresenter/Anims/FA_<name>`) — you'll put it in the sidecar.

---

## Path B — NVIDIA Audio2Face-3D via the ACE Unreal plugin

Install **both**: the core **NVIDIA ACE** plugin (`NV_ACE_Reference`) and the
**Audio2Face-3D Models** plugin. Start from NVIDIA's ACE sample project, which
ships a pre-built MetaHuman mapping.

1. Import a MetaHuman (Quixel Bridge) and place it in the level.
2. Enable **Show Plugin Content** in the Content Browser.
3. Edit the character blueprint → select the **Face** component → open **Face_AnimBP**.
4. In the AnimGraph, add the **Apply ACE Face Animations** node **before** the
   ARKit pose-mapping node (`mh_arkit_mapping_pose` / `mh_arkit_mapping_pose_A2F`).
5. Change the **Pose Asset** to **`mh_arkit_mapping_pose_A2F`**.
6. (Recommended) enable linear interpolation, set blend-out to avoid face popping,
   and bypass the default MouthClose block.
7. Add an **ACE Audio Curve Source** component to the character to feed audio+curves.
8. To use it in the batch render, **bake** the resulting performance to a Level
   Sequence (record the ACE-driven face to Sequencer), then note that sequence's
   content path for the sidecar.

Reference docs are linked at the bottom.

---

## The `anim/` handoff contract (this is the pipeline's part)

Because the baked animation lives as a **UE asset** (not a file), each clip gets a
tiny **sidecar** `anim/<name>.json` that tells `render.py` which asset to use.
`<name>` is the clip base name = the wav stem (e.g. `2026-09-06_your-topic-here`).

Managed by [`pipeline/anim_utils.py`](../pipeline/anim_utils.py):

```bat
:: 1. create a template sidecar for a clip (before or after baking)
python pipeline/anim_utils.py --scaffold 2026-09-06_your-topic-here

:: 2. after baking in UE, open anim/2026-09-06_your-topic-here.json and set:
::      "ue_asset":   "/Game/AIPresenter/Anims/FA_2026-09-06_your-topic-here"
::      "asset_type": "level_sequence"   (or "anim_sequence")
::      "source":     "metahuman_audio_driven"  (or "audio2face_3d")

:: 3. confirm the pipeline sees it
python pipeline/anim_utils.py --check 2026-09-06_your-topic-here
python pipeline/anim_utils.py --list
```

**Sidecar schema**

| field | meaning |
|---|---|
| `name` | clip base name (wav stem) |
| `audio` | path to the source wav (default `audio/<name>.wav`) |
| `ue_asset` | **content path** of the baked UE asset; empty ⇒ render idle |
| `asset_type` | `level_sequence` or `anim_sequence` |
| `source` | `metahuman_audio_driven` or `audio2face_3d` |
| `fps` | frame rate the anim was baked at (default 30) |
| `notes` | free text |

**Contract for Phase 3:** `render.py` calls `anim_utils.load_animation(config,
name)`. If it returns a dict → render with `ue_asset`. If it returns `None`
(no sidecar, empty `ue_asset`, or bad data) → **render the idle presenter**
(PIPELINE.md §9). So an un-baked clip degrades gracefully instead of failing.

---

## Sources

- [NVIDIA/Audio2Face-3D (GitHub)](https://github.com/NVIDIA/Audio2Face-3D)
- [ACE Unreal Plugin — Character Animation](https://archive.docs.nvidia.com/ace/ace-unreal-plugin/2.5/ace-unreal-plugin-animation.html)
- [Epic — Audio-Driven Animation (MetaHuman)](https://dev.epicgames.com/documentation/en-us/metahuman/audio-driven-animation)
- [Epic community: Setup NVIDIA ACE Audio2Face with MetaHuman](https://dev.epicgames.com/community/learning/tutorials/33Vd/unreal-engine-how-to-setup-nvidia-ace-audio2face-with-metahuman)

---

## ✅ WORKING lip-sync workflow (verified on this project, UE 5.8)

This is the path that actually renders a talking MetaHuman here. The character is
a new-style **MetaHuman Character**; its facial animation is **curve-based**
(RigLogic control curves), which needs specific handling the pipeline now does
automatically.

**One-time per clip (manual, in the editor):**
1. Import the clip's voice `.wav` as a SoundWave.
2. Content Browser ▸ **MetaHuman Animator ▸ MetaHuman Performance**; Input Type =
   **Audio**, assign the SoundWave, target the MetaHuman face; click **Process**
   (confirm the preview face moves).
3. **Export ▸ Export Animation Sequence** (NOT Level Sequence — audio-driven
   performances export an AnimSequence of face curves). Note its path, e.g.
   `/Game/AS_<clip>`.

**Link it (command line):**
```bat
python pipeline/anim_utils.py --scaffold <clip>
:: then set in anim/<clip>.json:
::   "ue_asset":   "/Game/AS_<clip>"
::   "asset_type": "anim_sequence"
```

**Render (fully automatic):** `python pipeline/render.py --input <clip>` then
builds a camera sequence, binds the AnimSequence to the MetaHuman **Face**
component, and — critically — does two things that make the face actually animate:
- **Enables RigLogic** (`disable_post_process_blueprint = False`) so the control
  curves drive the face.
- Sets **"Force Custom Mode"** on the facial animation section.

### The key gotcha — "Force Custom Mode" (UE 5.8 bug)
In **UE 5.8** a MetaHuman facial AnimSequence added to Sequencer/MRQ **does not
animate** unless the animation section's **Force Custom Mode** is enabled. This is
a confirmed 5.8 regression. `render_job.py` sets it programmatically
(`asec.params.force_custom_mode = True`). Symptom if missing: face renders frozen
in a neutral pose even though the AnimSequence plays fine in its own asset editor.

Refs:
- <https://forums.unrealengine.com/t/unreal-engine-5-8-metahuman-face-animation-not-working-in-sequencer/2738622>
- <https://forums.unrealengine.com/t/metahuman-facial-animation-doesnt-work-in-ue5-8-0-sequencer/2730105>

### Also learned
- Audio-driven Performances can't **Export Level Sequence** with a control-rig
  track (greyed out) — use **Export Animation Sequence**.
- `AS_*` face anims are **curves**, not baked bones — so RigLogic must stay ON
  (do NOT disable the Face post-process blueprint).
