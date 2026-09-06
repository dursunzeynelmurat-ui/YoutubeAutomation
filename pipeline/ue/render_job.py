"""render_job.py — runs INSIDE Unreal Engine's Python (Phase 3).

Launched by pipeline/render.py via:
    UnrealEditor-Cmd.exe <uproject> -ExecutePythonScript=<this file>
with the job parameters passed as a JSON file path in the AIPRESENTER_JOB env var.

Uses ONLY the standard library + the `unreal` module (UE's embedded Python does
not have this project's venv packages).

Modes (job["mode"]):
    list_cameras : print CineCameraActor labels found in the level (discovery)
    plate        : render one frame of the empty set -> renders/plate.png
    presenter    : render the presenter pass (PNG sequence, alpha) through the
                   locked camera, driven by a baked Level Sequence (or idle if none)

⚠️ The Movie Render Queue API specifics below target UE 5.8 and MUST be validated
on your machine against your actual presenter scene. Alpha output additionally
requires project setting: Rendering > PostProcessing > "Enable Alpha Channel
Support in Post Processing" = Linear Color (r.PostProcessing.PropagateAlpha).
See docs/PHASE3_RENDER.md.
"""
import json
import os
import sys

import unreal


def load_job():
    path = os.environ.get("AIPRESENTER_JOB")
    if not path or not os.path.exists(path):
        unreal.log_error(f"[AIPresenter] job file missing: {path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def quit_editor(code=0):
    if code:
        unreal.log_error(f"[AIPresenter] exiting with error code {code}")
    unreal.SystemLibrary.quit_editor()


# --------------------------------------------------------------------------- #
def do_list_cameras(job):
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    les.load_level(job["level"])
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    cams = [a for a in actor_sub.get_all_level_actors()
            if isinstance(a, unreal.CineCameraActor)]
    unreal.log(f"[AIPresenter] {len(cams)} CineCameraActor(s) in {job['level']}:")
    for a in cams:
        unreal.log(f"[AIPresenter]   label='{a.get_actor_label()}'  name='{a.get_name()}'")
    if not cams:
        unreal.log_warning("[AIPresenter] no CineCameraActor found — add and lock one.")
    quit_editor(0)


# --------------------------------------------------------------------------- #
def _base_config(job, with_alpha):
    """Build a MoviePipelinePrimaryConfig for the given job."""
    config = unreal.MoviePipelinePrimaryConfig()

    out = config.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting)
    out.output_directory = unreal.DirectoryPath(job["output_dir"])
    w, h = job["resolution"]
    out.output_resolution = unreal.IntPoint(int(w), int(h))
    out.output_frame_rate = unreal.FrameRate(int(job.get("fps", 30)), 1)
    out.override_existing_output = True
    # Plate is a single still; presenter is a numbered sequence (clean 0000.png names).
    out.file_name_format = "plate" if job["mode"] == "plate" else "{frame_number}"

    # Force the exact frame range instead of the sequence's default (which showed
    # up as [0,800)). render_frames is 1 for the plate, audio-length for presenter.
    render_frames = int(job.get("render_frames") or 0)
    if render_frames > 0:
        out.use_custom_playback_range = True
        out.custom_start_frame = 0
        out.custom_end_frame = render_frames

    # --- render pass: Path Tracer (photoreal) or standard deferred ---
    path_tracer = bool(job.get("path_tracer", False))
    if path_tracer:
        config.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPass_PathTracer)
        unreal.log("[AIPresenter] render pass = PATH TRACER (photoreal)")
    else:
        config.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPassBase)

    config.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_PNG)

    # --- console variables (alpha + path-tracer quality) ---
    cvars = {}
    if with_alpha:
        cvars["r.PostProcessing.PropagateAlpha"] = 1.0
    if path_tracer:
        cvars["r.PathTracing.Denoiser"] = 1.0                       # denoise -> clean at low spp
        cvars["r.PathTracing.MaxBounces"] = float(job.get("pt_max_bounces", 4))
        cvars["r.PathTracing.SamplesPerPixel"] = float(job.get("pt_spp", 64))
    elif job.get("lumen_hardware_rt", False):
        # Use RTX hardware ray tracing for Lumen GI + reflections (better realism).
        cvars["r.Lumen.HardwareRayTracing"] = 1.0
        cvars["r.Lumen.HardwareRayTracing.LightingMode"] = 2.0      # hit-lighting (higher quality)
        cvars["r.Lumen.Reflections.HardwareRayTracing"] = 1.0
    if cvars:
        cvs = config.find_or_add_setting_by_class(unreal.MoviePipelineConsoleVariableSetting)
        entries = []
        for k, v in cvars.items():
            e = unreal.MoviePipelineConsoleVariableEntry()
            e.set_editor_property("name", k)
            e.set_editor_property("value", v)
            e.set_editor_property("is_enabled", True)
            entries.append(e)
        try:
            cvs.set_editor_property("console_variables", entries)
        except Exception:
            cvs.set_editor_property("start_console_commands",
                                    [f"{k} {v}" for k, v in cvars.items()])
        unreal.log(f"[AIPresenter] cvars set: {cvars}")

    aa = config.find_or_add_setting_by_class(unreal.MoviePipelineAntiAliasingSetting)
    # Path tracer accumulates via spatial samples; give it plenty (denoiser cleans up).
    aa.spatial_sample_count = int(job.get("pt_spatial_samples", 36)) if path_tracer \
        else int(job.get("spatial_samples", 1))
    aa.temporal_sample_count = int(job.get("temporal_samples", 1))
    aa.engine_warm_up_count = int(job.get("warmup_frames", 32))
    aa.render_warm_up_count = int(job.get("warmup_frames", 32))

    config.find_or_add_setting_by_class(unreal.MoviePipelineCameraSetting)
    return config


AUTO_SEQ_DIR = "/Game/AIPresenter/Auto"


def _find_camera(job):
    """Return the CineCameraActor whose label matches job['camera_actor'] (or None)."""
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    label = job.get("camera_actor", "")
    for a in actor_sub.get_all_level_actors():
        if isinstance(a, unreal.CineCameraActor) and a.get_actor_label() == label:
            return a
    return None


def _find_metahuman_face():
    """Return (metahuman_actor, face_component) — the actor with a SkeletalMesh
    component named like 'Face' (MetaHuman). Skips reconstructed 'TRASH_' comps."""
    for a in unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors():
        for c in a.get_components_by_class(unreal.SkeletalMeshComponent):
            n = c.get_name()
            if n.upper().startswith("TRASH"):
                continue
            if "face" in n.lower():
                return a, c
    return None, None


def _ensure_face_post_process_enabled():
    """Ensure RigLogic post-process is ON for the placed MetaHuman Face (curve-based
    facial anim needs it). Undoes any earlier disable. Runtime change carries into
    the MRQ PIE render."""
    mh, face = _find_metahuman_face()
    if face is None:
        unreal.log_warning("[AIPresenter] no Face component found to enable post-process")
        return
    try:
        face.set_editor_property("disable_post_process_blueprint", False)
        unreal.log(f"[AIPresenter] RigLogic post-process ENABLED on {mh.get_actor_label()}/Face")
    except Exception as exc:
        unreal.log_warning(f"[AIPresenter] could not enable post-process: {exc}")


def _make_camera_sequence(job, num_frames, anim_path=None):
    """Create/overwrite a transient LevelSequence with a camera-cut bound to the
    configured camera. If anim_path (an AnimSequence) is given, also bind the
    MetaHuman's Face component and play that facial animation on it (lip-sync).
    Returns the sequence content path."""
    cam = _find_camera(job)
    if cam is None:
        raise RuntimeError(f"camera actor '{job.get('camera_actor')}' not found in level "
                           f"(run: python pipeline/render.py --list-cameras)")
    fps = int(job.get("fps", 30))
    name = f"AUTO_{job['mode']}_{job.get('name', 'plate')}"
    asset_path = f"{AUTO_SEQ_DIR}/{name}"
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        unreal.EditorAssetLibrary.delete_asset(asset_path)

    unreal.log(f"[AIPresenter] step: create sequence {asset_path} ({num_frames} frames @ {fps}fps)")
    seq = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        name, AUTO_SEQ_DIR, unreal.LevelSequence, unreal.LevelSequenceFactoryNew())
    seq.set_display_rate(unreal.FrameRate(fps, 1))
    seq.set_playback_start(0)
    seq.set_playback_end(int(num_frames))

    unreal.log("[AIPresenter] step: bind camera + camera-cut")
    binding = seq.add_possessable(cam)
    cut_track = seq.add_track(unreal.MovieSceneCameraCutTrack)
    cut = cut_track.add_section()
    cut.set_start_frame(0)
    cut.set_end_frame(int(num_frames))
    binding_id = unreal.MovieSceneObjectBindingID()
    binding_id.set_editor_property("guid", binding.get_id())
    cut.set_camera_binding_id(binding_id)

    if anim_path:
        anim = unreal.EditorAssetLibrary.load_asset(anim_path)
        mh, face = _find_metahuman_face()
        if anim and mh and face:
            unreal.log(f"[AIPresenter] step: bind facial anim {anim_path} on {mh.get_actor_label()}/Face")
            seq.add_possessable(mh)                       # actor binding (parent)
            comp_binding = seq.add_possessable(face)      # Face component binding
            at = comp_binding.add_track(unreal.MovieSceneSkeletalAnimationTrack)
            asec = at.add_section()
            asec.set_start_frame(0)
            asec.set_end_frame(int(num_frames))
            asec.params.set_editor_property("animation", anim)
            # UE 5.8 fix: "Force Custom Mode" makes the face anim actually drive the
            # MetaHuman in Sequencer/MRQ (known 5.8 bug otherwise). Try known shapes.
            applied = False
            for target, prop in ((asec.params, "force_custom_mode"),
                                 (asec, "force_custom_mode"),
                                 (asec.params, "swap_root_bone"),  # never; placeholder skip
                                 (asec, "b_force_custom_mode")):
                if prop == "swap_root_bone":
                    continue
                try:
                    target.set_editor_property(prop, True)
                    applied = True
                    break
                except Exception:
                    pass
            unreal.log(f"[AIPresenter] facial anim bound; force_custom_mode applied={applied}")
        else:
            unreal.log_warning(f"[AIPresenter] facial anim NOT applied "
                               f"(anim={bool(anim)}, metahuman={bool(mh)}, face={bool(face)}) "
                               f"— rendering idle.")

    unreal.EditorAssetLibrary.save_asset(asset_path)
    return asset_path


# Kept at MODULE scope so Python does not garbage-collect them before the async
# render finishes (a local executor/callback silently never fires its delegate).
_EXECUTOR = None


def _on_render_finished(executor, success):
    unreal.log(f"[AIPresenter] render finished (success={success})")
    quit_editor(0 if success else 2)


def _run_queue(job, sequence, *, with_alpha):
    global _EXECUTOR
    subsystem = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    queue = subsystem.get_queue()
    queue.delete_all_jobs()
    mrq_job = queue.allocate_new_job(unreal.MoviePipelineExecutorJob)
    mrq_job.job_name = job.get("name", job["mode"])
    mrq_job.map = unreal.SoftObjectPath(job["level"])
    mrq_job.sequence = unreal.SoftObjectPath(sequence)
    mrq_job.set_configuration(_base_config(job, with_alpha=with_alpha))

    unreal.log("[AIPresenter] step: submit MRQ queue")
    _EXECUTOR = unreal.MoviePipelinePIEExecutor()
    _EXECUTOR.on_executor_finished_delegate.add_callable(_on_render_finished)
    if hasattr(_EXECUTOR, "on_executor_errored_delegate"):
        _EXECUTOR.on_executor_errored_delegate.add_callable(
            lambda ex, pipeline, is_fatal, msg: (
                unreal.log_error(f"[AIPresenter] render errored: {msg}"), quit_editor(2)))
    subsystem.render_queue_with_executor_instance(_EXECUTOR)
    # Control returns to UE's tick loop; _on_render_finished quits the editor.


def do_plate(job):
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    les.load_level(job["level"])
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    for a in actor_sub.get_all_level_actors():          # hide presenter -> empty set
        if a.actor_has_tag("presenter"):
            a.set_actor_hidden_in_game(True)             # MRQ renders the game view
    sequence = _make_camera_sequence(job, num_frames=1)
    job["render_frames"] = 1
    unreal.log("[AIPresenter] rendering plate (opaque, single frame)…")
    _run_queue(job, sequence, with_alpha=False)


def _hide_environment_for_alpha(job):
    """Hide set geometry so the presenter pass is the MetaHuman on transparency.
    Keeps lights, sky, camera, and anything tagged 'presenter'/'keep'. Hides
    static meshes, foliage, landscape (the room/props)."""
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    # Geometry AND the visible sky/atmosphere/fog/clouds (these fill the background
    # opaque even with Alpha Output on). Lights (Directional/Sky/Point/etc.) are NOT
    # hidden, so the presenter stays lit; the emptied background becomes transparent.
    hide_types = (unreal.StaticMeshActor, unreal.InstancedFoliageActor, unreal.Landscape,
                  unreal.SkyAtmosphere, unreal.ExponentialHeightFog, unreal.VolumetricCloud)
    hidden = 0
    for a in actor_sub.get_all_level_actors():
        if a.actor_has_tag("presenter") or a.actor_has_tag("keep"):
            continue
        if isinstance(a, hide_types):
            a.set_actor_hidden_in_game(True)
            hidden += 1
    unreal.log(f"[AIPresenter] hid {hidden} set/sky actor(s) for the alpha presenter pass")


def _export_ls_from_performance(perf_path):
    """Export a Level Sequence from a processed MetaHuman Performance asset (Epic's
    exporter — drives the face correctly), save it, and return its content path."""
    perf = unreal.EditorAssetLibrary.load_asset(perf_path)
    if perf is None:
        raise RuntimeError(f"performance asset not found: {perf_path}")
    U = unreal.MetaHumanPerformanceExportUtils
    settings = U.get_export_level_sequence_settings(perf)
    # OFF: things we don't want (we add our own camera; no audio/video/depth planes).
    for prop in ("show_export_dialog", "export_camera", "export_audio_track",
                 "export_video_track", "export_depth_track", "export_image_plane",
                 "export_depth_mesh"):
        try:
            settings.set_editor_property(prop, False)
        except Exception:
            pass
    # ON: the MetaHuman + the tracks that actually carry the facial animation.
    for prop in ("export_identity", "export_control_rig_track", "export_transform_track"):
        try:
            settings.set_editor_property(prop, True)
        except Exception:
            pass
    unreal.log(f"[AIPresenter] step: export Level Sequence from performance {perf_path}")
    seq = U.export_level_sequence(perf, settings)
    ls_path = None
    if seq is not None:
        ls_path = seq.get_path_name().split(".")[0]
    else:  # some setting combos return None but still create the asset
        guess = "/Game/LS_" + perf.get_name()
        if unreal.EditorAssetLibrary.does_asset_exist(guess):
            ls_path = guess
    if not ls_path or not unreal.EditorAssetLibrary.does_asset_exist(ls_path):
        raise RuntimeError("export_level_sequence produced no asset")
    unreal.EditorAssetLibrary.save_asset(ls_path)

    # Log what the exported sequence actually contains (diagnostic).
    ls = unreal.EditorAssetLibrary.load_asset(ls_path)
    try:
        sp, po = ls.get_spawnables(), ls.get_possessables()
        unreal.log(f"[AIPresenter] exported {ls_path}: spawnables={len(sp)} possessables={len(po)}")
        for b in list(sp) + list(po):
            tr = [type(t).__name__ for t in b.get_tracks()]
            kids = []
            try:
                kids = [f"{c.get_display_name()}:{[type(t).__name__ for t in c.get_tracks()]}"
                        for c in b.get_child_possessables()]
            except Exception:
                pass
            unreal.log(f"[AIPresenter]   binding '{b.get_display_name()}' tracks={tr} children={kids}")
    except Exception as exc:
        unreal.log_warning(f"[AIPresenter] could not introspect exported LS: {exc}")
    return ls_path


def _prepare_baked_sequence(job, baked_ls_path):
    """Duplicate the baked Level Sequence (which already drives the MetaHuman face
    correctly, via Epic's export) and add a camera-cut bound to PresenterCam, so
    MRQ renders it through our locked camera. Returns the duplicate's path."""
    cam = _find_camera(job)
    if cam is None:
        raise RuntimeError(f"camera actor '{job.get('camera_actor')}' not found")
    seq = unreal.EditorAssetLibrary.load_asset(baked_ls_path)
    if seq is None:
        raise RuntimeError(f"could not load baked sequence {baked_ls_path}")

    # Remove any camera-cut tracks we added on a previous run (keep it idempotent).
    for t in list(seq.get_tracks()):
        if isinstance(t, unreal.MovieSceneCameraCutTrack):
            seq.remove_track(t)

    unreal.log(f"[AIPresenter] step: add PresenterCam camera-cut to baked sequence {baked_ls_path}")
    cut_track = seq.add_track(unreal.MovieSceneCameraCutTrack)
    cut = cut_track.add_section()
    cut.set_start_frame(seq.get_playback_start())
    cut.set_end_frame(seq.get_playback_end())
    binding = seq.add_possessable(cam)
    bid = unreal.MovieSceneObjectBindingID()
    bid.set_editor_property("guid", binding.get_id())
    cut.set_camera_binding_id(bid)
    unreal.EditorAssetLibrary.save_asset(baked_ls_path)
    return baked_ls_path


def do_presenter(job):
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    les.load_level(job["level"])
    transparent = bool(job.get("transparent_bg", False))
    if transparent:
        _hide_environment_for_alpha(job)                 # presenter-only -> transparent bg
    else:
        unreal.log("[AIPresenter] scene mode: rendering presenter IN the room (opaque)")
    perf = job.get("performance", "")                    # a MetaHuman Performance asset
    baked_ls = job.get("sequence", "")                   # a ready-made Level Sequence
    anim_seq = job.get("anim_sequence", "")              # an AnimSequence to play on the face
    if perf:
        baked_ls = _export_ls_from_performance(perf)     # export a face-driving Level Sequence
    if baked_ls:
        # Level Sequence drives the face; add our PresenterCam camera cut.
        sequence = _prepare_baked_sequence(job, baked_ls)
    else:                                                # build camera seq (+ face anim)
        num_frames = int(job.get("num_frames") or job.get("fps", 30) * 10)
        if anim_seq:
            _ensure_face_post_process_enabled()          # RigLogic must be ON for curve anim
        sequence = _make_camera_sequence(job, num_frames=num_frames, anim_path=anim_seq or None)
        job["render_frames"] = num_frames                # force exact length (matches the voice)
    unreal.log(f"[AIPresenter] rendering presenter pass for '{job.get('name')}' "
               f"(transparent={transparent})…")
    _run_queue(job, sequence, with_alpha=transparent)


# --------------------------------------------------------------------------- #
def main():
    job = load_job()
    mode = job.get("mode")
    unreal.log(f"[AIPresenter] render_job mode={mode}")
    try:
        if mode == "list_cameras":
            do_list_cameras(job)
        elif mode == "plate":
            do_plate(job)
        elif mode == "presenter":
            do_presenter(job)
        else:
            unreal.log_error(f"[AIPresenter] unknown mode: {mode}")
            quit_editor(1)
    except Exception as exc:  # noqa: BLE001
        unreal.log_error(f"[AIPresenter] render_job failed: {exc}")
        quit_editor(1)


main()
