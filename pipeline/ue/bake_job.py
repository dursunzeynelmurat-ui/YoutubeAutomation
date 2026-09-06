"""bake_job.py — headless MetaHuman facial bake (runs INSIDE Unreal).

Given a voice .wav, produces a facial AnimSequence by:
  1. importing the wav as a SoundWave,
  2. duplicating a reference MetaHuman Performance (reuses its identity/config),
  3. swapping in the new audio and PROCESSING it (blocking solve),
  4. exporting the facial Animation Sequence.

Params come from a JSON file named by the AIPRESENTER_JOB env var:
  { "audio_file", "clip", "ref_performance", "anim_out", "timeout" }

Uses stdlib + `unreal` only. Emits [AIPresenter] markers for the launcher.
"""
import json
import os
import sys

import unreal


def log(m): unreal.log(f"[AIPresenter] {m}")
def quit(code=0): unreal.SystemLibrary.quit_editor()


def main():
    path = os.environ.get("AIPRESENTER_JOB")
    if not path or not os.path.exists(path):
        unreal.log_error(f"[AIPresenter] job file missing: {path}"); quit(1); return
    job = json.load(open(path, encoding="utf-8"))
    clip = job["clip"]
    log(f"bake start clip={clip}")

    # 1) import the wav as a SoundWave -> /Game/Audio/<clip>
    task = unreal.AssetImportTask()
    task.filename = job["audio_file"]
    task.destination_path = "/Game/Audio"
    task.destination_name = clip
    task.automated = True
    task.replace_existing = True
    task.save = True
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    sound_path = f"/Game/Audio/{clip}"
    sound = unreal.EditorAssetLibrary.load_asset(sound_path)
    log(f"imported sound {sound_path} -> {bool(sound)}")
    if not sound:
        unreal.log_error("[AIPresenter] sound import failed"); quit(1); return

    # 2) duplicate the reference Performance (keeps its identity + audio input type)
    ref = job["ref_performance"]
    perf_path = f"/Game/AUTO_Perf_{clip}"
    if unreal.EditorAssetLibrary.does_asset_exist(perf_path):
        unreal.EditorAssetLibrary.delete_asset(perf_path)
    perf = unreal.EditorAssetLibrary.duplicate_asset(ref, perf_path)
    if perf is None:
        perf = unreal.EditorAssetLibrary.load_asset(perf_path)
    if perf is None:
        unreal.log_error(f"[AIPresenter] could not duplicate {ref}"); quit(1); return
    log(f"duplicated performance -> {perf_path}")

    # 3) swap audio + process (blocking so the script waits for the solve)
    try:
        perf.set_editor_property("audio", sound)
    except Exception as e:
        log(f"set audio warn: {e}")
    try:
        perf.set_blocking_processing(True)
    except Exception as e:
        log(f"set_blocking warn: {e}")
    if not perf.can_process():
        unreal.log_error("[AIPresenter] performance can_process()=False (identity/audio issue)")
        quit(1); return
    log("processing (blocking solve)…")
    perf.start_pipeline()
    log(f"processed frames = {perf.get_number_of_processed_frames()}; "
        f"has_anim = {perf.contains_animation_data()}")

    # 4) export the facial AnimSequence
    U = unreal.MetaHumanPerformanceExportUtils
    settings = U.get_export_animation_sequence_settings(perf)
    for p, v in (("show_export_dialog", False), ("export_face", True),
                 ("export_body", False)):
        try: settings.set_editor_property(p, v)
        except Exception: pass
    anim = U.export_animation_sequence(perf, settings)
    anim_path = None
    if anim is not None:
        anim_path = anim.get_path_name().split(".")[0]
    else:
        guess = f"/Game/AS_AUTO_Perf_{clip}"
        if unreal.EditorAssetLibrary.does_asset_exist(guess):
            anim_path = guess
    if not anim_path:
        unreal.log_error("[AIPresenter] export produced no AnimSequence"); quit(1); return
    unreal.EditorAssetLibrary.save_asset(anim_path)
    log(f"BAKE OK anim={anim_path}")
    quit(0)


main()
