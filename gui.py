#!/usr/bin/env python3
"""Scroll & Told — Studio: a desktop control panel for the whole pipeline.

Tkinter (ships with Python — no extra installs). Every feature is a tab; each action
runs the matching pipeline script as a subprocess and streams its output to the log.

Run it with the project's venv Python so subprocesses inherit the right interpreter:
    Windows:  .venv\\Scripts\\python gui.py
    macOS:    ./.venv/bin/python gui.py
"""
from __future__ import annotations

import os
import platform
import queue
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

BASE = Path(__file__).resolve().parent
PY = sys.executable                      # the venv python running this GUI
IS_WIN = platform.system() == "Windows"


def _open_path(p: Path):
    p = Path(p)
    try:
        if IS_WIN:
            os.startfile(str(p))                       # noqa: S606
        elif platform.system() == "Darwin":
            subprocess.run(["open", str(p)])
        else:
            subprocess.run(["xdg-open", str(p)])
    except Exception as exc:  # noqa: BLE001
        messagebox.showerror("Open", f"Could not open {p}: {exc}")


class Studio:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.jobs: deque = deque()
        self.q: queue.Queue = queue.Queue()
        root.title("Scroll & Told — Studio")
        root.geometry("1040x760")

        self.config_var = tk.StringVar(value="config.reddit.yaml")

        top = ttk.Frame(root, padding=(10, 8)); top.pack(fill="x")
        ttk.Label(top, text="Config:").pack(side="left")
        ttk.Entry(top, textvariable=self.config_var, width=26).pack(side="left", padx=6)
        ttk.Button(top, text="config.reddit.yaml", width=18,
                   command=lambda: self.config_var.set("config.reddit.yaml")).pack(side="left")
        ttk.Button(top, text="config.yaml (finance)", width=20,
                   command=lambda: self.config_var.set("config.yaml")).pack(side="left", padx=4)

        self.nb = ttk.Notebook(root); self.nb.pack(fill="both", expand=True, padx=10, pady=6)
        self._tab_longform()
        self._tab_shorts()
        self._tab_promos()
        self._tab_addstory()
        self._tab_finance()
        self._tab_fetch()
        self._tab_compile()
        self._tab_upload()
        self._tab_schedule()
        self._tab_library()
        self._tab_doctor()
        self._tab_folders()

        # log + controls
        bottom = ttk.Frame(root, padding=(10, 6)); bottom.pack(fill="both", expand=False)
        bar = ttk.Frame(bottom); bar.pack(fill="x")
        self.status = ttk.Label(bar, text="Idle", foreground="#2a7"); self.status.pack(side="left")
        self.prog = ttk.Progressbar(bar, mode="indeterminate", length=160); self.prog.pack(side="left", padx=12)
        ttk.Button(bar, text="Stop", command=self.stop).pack(side="right")
        ttk.Button(bar, text="Clear log", command=self._clear).pack(side="right", padx=6)
        self.log = tk.Text(bottom, height=15, bg="#0f0f12", fg="#d8d8dc", insertbackground="#d8d8dc",
                           wrap="word", font=("Consolas" if IS_WIN else "Menlo", 9))
        self.log.pack(fill="both", expand=True, pady=(6, 0))
        self._log("Scroll & Told Studio ready.\nPick a tab, set options, and hit Run. "
                  "Output streams here.\n")
        self.root.after(120, self._poll)

    # ---- process runner ---------------------------------------------------
    def _script(self, name, *args):
        return [PY, str(BASE / "pipeline" / name), *args, "--config", self.config_var.get()]

    def run(self, cmd, label):
        """Queue a job; it starts immediately if idle, else runs after the current ones."""
        self.jobs.append((cmd, label))
        if len(self.jobs) > 1 or self.proc is not None:
            self._log(f"[queued: {label}  ({len(self.jobs)} waiting)]\n")
        self._pump()

    def _pump(self):
        if self.proc is not None or not self.jobs:
            return
        cmd, label = self.jobs.popleft()
        self._log(f"\n{'='*70}\n▶ {label}\n$ {' '.join(str(c) for c in cmd)}\n{'='*70}\n")
        self.status.configure(text=f"Running: {label}", foreground="#e0a13a")
        try:
            self.prog.start(12)
        except Exception:  # noqa: BLE001
            pass
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"; env["HF_HUB_DISABLE_TELEMETRY"] = "1"
        try:
            self.proc = subprocess.Popen(cmd, cwd=str(BASE), stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[failed to start: {exc}]\n"); self.proc = None; self._idle(); self._pump(); return
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _idle(self):
        self.status.configure(text="Idle" if not self.jobs else f"{len(self.jobs)} queued", foreground="#2a7")
        try:
            self.prog.stop()
        except Exception:  # noqa: BLE001
            pass

    def _reader(self, proc):
        for line in iter(proc.stdout.readline, ""):
            self.q.put(line)
        proc.stdout.close(); proc.wait()
        self.q.put(("__DONE__", proc.returncode))

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple) and item[0] == "__DONE__":
                    self._log(f"\n[finished · exit {item[1]}]\n")
                    self.proc = None
                    self._idle()
                    try:
                        self.root.bell()
                    except Exception:  # noqa: BLE001
                        pass
                    self._pump()          # start the next queued job, if any
                else:
                    self._log(item)
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def stop(self):
        if self.jobs:
            self._log(f"\n[cleared {len(self.jobs)} queued job(s)]\n"); self.jobs.clear()
        if self.proc is not None:
            try:
                self.proc.terminate(); self._log("\n[stop requested]\n")
            except Exception:  # noqa: BLE001
                pass

    def _log(self, text):
        self.log.insert("end", text); self.log.see("end")

    def _clear(self):
        self.log.delete("1.0", "end")

    # ---- tabs -------------------------------------------------------------
    def _tab_longform(self):
        f = ttk.Frame(self.nb, padding=12); self.nb.add(f, text="Long-form (Horror)")
        ttk.Label(f, text="16:9 narrated horror + AI visuals + motion. Auto-cuts promo shorts after.",
                  foreground="#888").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        self.lf_mode = tk.StringVar(value="auto")
        for i, (v, t) in enumerate([("auto", "Auto (pick from feeds)"), ("id", "By story id"),
                                    ("script", "From saved script")]):
            ttk.Radiobutton(f, text=t, variable=self.lf_mode, value=v).grid(row=1, column=i, sticky="w")
        ttk.Label(f, text="id / script path:").grid(row=2, column=0, sticky="w", pady=6)
        self.lf_arg = ttk.Entry(f, width=48); self.lf_arg.grid(row=2, column=1, columnspan=2, sticky="w")
        ttk.Button(f, text="Browse", command=lambda: self._browse(self.lf_arg)).grid(row=2, column=3)
        self.lf_nohero = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Stills only (skip SVD motion — much faster)",
                        variable=self.lf_nohero).grid(row=3, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Button(f, text="Run long-form", command=self._run_longform).grid(row=4, column=0, pady=10, sticky="w")

    def _run_longform(self):
        m = self.lf_mode.get(); arg = self.lf_arg.get().strip()
        cmd = self._script("longform.py")
        if m == "auto":
            cmd += ["--auto"]
        elif m == "id":
            if not arg:
                return messagebox.showwarning("Long-form", "Enter a story id.")
            cmd += ["--id", arg]
        else:
            if not arg:
                return messagebox.showwarning("Long-form", "Choose a saved script.")
            cmd += ["--script", arg]
        if self.lf_nohero.get():
            cmd += ["--no-hero"]
        self.run(cmd, "Long-form render")

    def _tab_shorts(self):
        f = ttk.Frame(self.nb, padding=12); self.nb.add(f, text="Reddit Shorts")
        ttk.Label(f, text="9:16 narrated Reddit-story shorts (multi-part, post card).",
                  foreground="#888").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        self.sh_mode = tk.StringVar(value="auto")
        ttk.Radiobutton(f, text="Auto", variable=self.sh_mode, value="auto").grid(row=1, column=0, sticky="w")
        ttk.Radiobutton(f, text="By id", variable=self.sh_mode, value="id").grid(row=1, column=1, sticky="w")
        ttk.Label(f, text="count:").grid(row=2, column=0, sticky="w", pady=6)
        self.sh_count = tk.Spinbox(f, from_=1, to=20, width=5); self.sh_count.grid(row=2, column=1, sticky="w")
        ttk.Label(f, text="id (if By id):").grid(row=3, column=0, sticky="w")
        self.sh_id = ttk.Entry(f, width=30); self.sh_id.grid(row=3, column=1, columnspan=2, sticky="w")
        self.sh_upload = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Upload each (PRIVATE)", variable=self.sh_upload).grid(row=4, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Button(f, text="Run shorts", command=self._run_shorts).grid(row=5, column=0, pady=10, sticky="w")

    def _run_shorts(self):
        cmd = self._script("redditstory.py")
        if self.sh_mode.get() == "auto":
            cmd += ["--auto", "--count", str(self.sh_count.get())]
        else:
            if not self.sh_id.get().strip():
                return messagebox.showwarning("Shorts", "Enter a story id.")
            cmd += ["--id", self.sh_id.get().strip()]
        if self.sh_upload.get():
            cmd += ["--upload"]
        self.run(cmd, "Reddit shorts")

    def _tab_promos(self):
        f = ttk.Frame(self.nb, padding=12); self.nb.add(f, text="Promo Shorts")
        ttk.Label(f, text="Cut punchy teaser shorts from a finished long video (funnel).",
                  foreground="#888").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(f, text="Long video:").grid(row=1, column=0, sticky="w")
        self.pr_clip = ttk.Combobox(f, width=48, values=self._longform_clips()); self.pr_clip.grid(row=1, column=1, sticky="w")
        ttk.Button(f, text="↻", width=3, command=lambda: self.pr_clip.configure(values=self._longform_clips())).grid(row=1, column=2)
        ttk.Label(f, text="count:").grid(row=2, column=0, sticky="w", pady=6)
        self.pr_count = tk.Spinbox(f, from_=1, to=10, width=5); self.pr_count.delete(0, "end"); self.pr_count.insert(0, "4")
        self.pr_count.grid(row=2, column=1, sticky="w")
        ttk.Button(f, text="Cut promos", command=self._run_promos).grid(row=3, column=0, pady=10, sticky="w")

    def _longform_clips(self):
        d = BASE / "output" / "longform"
        return [p.stem for p in d.glob("*.mp4")] if d.exists() else []

    def _run_promos(self):
        clip = self.pr_clip.get().strip()
        if not clip:
            return messagebox.showwarning("Promos", "Pick a long video.")
        self.run(self._script("promo.py", "--input", clip, "--count", str(self.pr_count.get())), "Promo shorts")

    def _tab_addstory(self):
        f = ttk.Frame(self.nb, padding=12); self.nb.add(f, text="Add Story")
        ttk.Label(f, text="Paste a story to render by id (works without fetching).",
                  foreground="#888").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(f, text="id:").grid(row=1, column=0, sticky="w")
        self.as_id = ttk.Entry(f, width=20); self.as_id.grid(row=1, column=1, sticky="w")
        ttk.Label(f, text="subreddit:").grid(row=1, column=2, sticky="e")
        self.as_sub = ttk.Combobox(f, width=16, values=["nosleep", "creepypasta", "LetsNotMeet",
                                   "shortscarystories", "scarystories", "AmItheAsshole", "tifu"])
        self.as_sub.set("nosleep"); self.as_sub.grid(row=1, column=3, sticky="w")
        ttk.Label(f, text="title:").grid(row=2, column=0, sticky="w", pady=6)
        self.as_title = ttk.Entry(f, width=70); self.as_title.grid(row=2, column=1, columnspan=3, sticky="w")
        ttk.Label(f, text="story text:").grid(row=3, column=0, sticky="nw", pady=6)
        self.as_text = tk.Text(f, width=80, height=12, wrap="word"); self.as_text.grid(row=3, column=1, columnspan=3, sticky="w")
        ttk.Button(f, text="Save story", command=self._save_story).grid(row=4, column=1, sticky="w", pady=8)

    def _save_story(self):
        sid = self.as_id.get().strip(); title = self.as_title.get().strip()
        body = self.as_text.get("1.0", "end").strip()
        if not (sid and title and len(body.split()) >= 30):
            return messagebox.showwarning("Add Story", "Need id, title, and a story of 30+ words.")
        tmp = BASE / "content" / "stories" / f"_{sid}.txt"
        tmp.parent.mkdir(parents=True, exist_ok=True); tmp.write_text(body, encoding="utf-8")
        self.run(self._script("add_story.py", "--id", sid, "--title", title,
                              "--subreddit", self.as_sub.get(), "--textfile", str(tmp)), "Add story")

    def _tab_finance(self):
        f = ttk.Frame(self.nb, padding=12); self.nb.add(f, text="Finance Shorts")
        ttk.Label(f, text="Finance brainrot shorts (uses config.yaml). Set Config to config.yaml.",
                  foreground="#888").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(f, text="topic:").grid(row=1, column=0, sticky="w")
        self.fin_topic = ttk.Entry(f, width=60); self.fin_topic.grid(row=1, column=1, columnspan=3, sticky="w")
        ttk.Label(f, text="or topics file:").grid(row=2, column=0, sticky="w", pady=6)
        self.fin_file = ttk.Entry(f, width=50); self.fin_file.grid(row=2, column=1, columnspan=2, sticky="w")
        ttk.Button(f, text="Browse", command=lambda: self._browse(self.fin_file)).grid(row=2, column=3)
        self.fin_upload = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Upload each (PRIVATE)", variable=self.fin_upload).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Button(f, text="Run finance shorts", command=self._run_finance).grid(row=4, column=0, pady=10, sticky="w")

    def _run_finance(self):
        cmd = [PY, str(BASE / "pipeline" / "brainrot.py")]
        if self.fin_file.get().strip():
            cmd += ["--topics", self.fin_file.get().strip()]
        elif self.fin_topic.get().strip():
            cmd += ["--topic", self.fin_topic.get().strip()]
        else:
            return messagebox.showwarning("Finance", "Enter a topic or a topics file.")
        if self.fin_upload.get():
            cmd += ["--upload"]
        cmd += ["--config", self.config_var.get()]
        self.run(cmd, "Finance shorts")

    def _tab_fetch(self):
        f = ttk.Frame(self.nb, padding=12); self.nb.add(f, text="Fetch / Sources")
        ttk.Label(f, text="Preview candidate stories from the RSS feeds in your config.",
                  foreground="#888").grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Button(f, text="List candidates", command=lambda: self.run(
            self._script("reddit_fetch.py", "--list"), "List candidates")).grid(row=1, column=0, sticky="w")
        ttk.Button(f, text="Edit subreddits (open config)", command=lambda: _open_path(BASE / self.config_var.get())
                   ).grid(row=1, column=1, padx=8)

    def _tab_compile(self):
        f = ttk.Frame(self.nb, padding=12); self.nb.add(f, text="Compilation")
        ttk.Label(f, text="Stitch the week's shorts into one long-form compilation.",
                  foreground="#888").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(f, text="days:").grid(row=1, column=0, sticky="w")
        self.cp_days = tk.Spinbox(f, from_=1, to=60, width=5); self.cp_days.delete(0, "end"); self.cp_days.insert(0, "7")
        self.cp_days.grid(row=1, column=1, sticky="w")
        ttk.Button(f, text="Build compilation", command=lambda: self.run(
            self._script("compile_weekly.py", "--days", str(self.cp_days.get())), "Weekly compilation")
                   ).grid(row=2, column=0, pady=10, sticky="w")

    def _tab_upload(self):
        f = ttk.Frame(self.nb, padding=12); self.nb.add(f, text="Upload / YouTube")
        ttk.Label(f, text="Uploads are PRIVATE by default. First run opens the browser for consent.",
                  foreground="#888").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Button(f, text="Authorize YouTube (OAuth)", command=lambda: self.run(
            self._script("upload.py", "--auth-only"), "YouTube authorize")).grid(row=1, column=0, sticky="w")
        ttk.Separator(f, orient="horizontal").grid(row=2, column=0, columnspan=4, sticky="ew", pady=10)
        self.up_mode = tk.StringVar(value="queue")
        ttk.Radiobutton(f, text="Upload queue (all pending)", variable=self.up_mode, value="queue").grid(row=3, column=0, sticky="w")
        ttk.Radiobutton(f, text="Single clip (name)", variable=self.up_mode, value="input").grid(row=4, column=0, sticky="w")
        ttk.Radiobutton(f, text="Video file", variable=self.up_mode, value="file").grid(row=5, column=0, sticky="w")
        self.up_arg = ttk.Entry(f, width=52); self.up_arg.grid(row=4, column=1, columnspan=2, sticky="w")
        ttk.Button(f, text="Browse", command=lambda: self._browse(self.up_arg)).grid(row=5, column=1, sticky="w")
        ttk.Label(f, text="privacy:").grid(row=6, column=0, sticky="w", pady=6)
        self.up_priv = ttk.Combobox(f, width=12, values=["private", "unlisted", "public"]); self.up_priv.set("private")
        self.up_priv.grid(row=6, column=1, sticky="w")
        ttk.Label(f, text="schedule public at:").grid(row=7, column=0, sticky="w", pady=6)
        self.up_sched = ttk.Entry(f, width=20); self.up_sched.grid(row=7, column=1, sticky="w")
        ttk.Label(f, text="YYYY-MM-DDTHH:MM (local, optional)", foreground="#888").grid(row=7, column=2, sticky="w")
        ttk.Button(f, text="Upload", command=self._run_upload).grid(row=8, column=0, pady=10, sticky="w")

    def _run_upload(self):
        m = self.up_mode.get(); cmd = self._script("upload.py")
        if m == "queue":
            cmd += ["--queue"]
        elif m == "input":
            if not self.up_arg.get().strip():
                return messagebox.showwarning("Upload", "Enter a clip name.")
            cmd += ["--input", self.up_arg.get().strip()]
        else:
            if not self.up_arg.get().strip():
                return messagebox.showwarning("Upload", "Choose a video file.")
            cmd += ["--file", self.up_arg.get().strip()]
        priv = self.up_priv.get()
        if priv == "public" and not messagebox.askyesno("PUBLIC", "Upload PUBLIC (visible to everyone)?"):
            return
        cmd += ["--privacy", priv]
        sched = self.up_sched.get().strip()
        if sched:
            cmd += ["--publish-at", sched]
        self.run(cmd, f"Upload ({m})")

    def _tab_schedule(self):
        f = ttk.Frame(self.nb, padding=12); self.nb.add(f, text="Schedule")
        ttk.Label(f, text="Windows Task Scheduler: produce stories daily (run_daily.bat).",
                  foreground="#888").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(f, text="time (HH:MM):").grid(row=1, column=0, sticky="w")
        self.sc_time = ttk.Entry(f, width=8); self.sc_time.insert(0, "09:00"); self.sc_time.grid(row=1, column=1, sticky="w")
        ttk.Label(f, text="count:").grid(row=1, column=2, sticky="e")
        self.sc_count = tk.Spinbox(f, from_=1, to=10, width=5); self.sc_count.delete(0, "end"); self.sc_count.insert(0, "3")
        self.sc_count.grid(row=1, column=3, sticky="w")
        ttk.Button(f, text="Register daily task", command=self._sched_create).grid(row=2, column=0, pady=10, sticky="w")
        ttk.Button(f, text="Remove task", command=self._sched_delete).grid(row=2, column=1, sticky="w")
        ttk.Separator(f, orient="horizontal").grid(row=3, column=0, columnspan=4, sticky="ew", pady=8)
        ttk.Label(f, text="One-click Daily Run (queued back-to-back):", foreground="#888").grid(row=4, column=0, columnspan=3, sticky="w")
        ttk.Button(f, text="▶ Full daily now (1 long-form + promos, then 2 shorts)",
                   command=self._run_daily_full).grid(row=5, column=0, columnspan=3, pady=8, sticky="w")

    def _run_daily_full(self):
        self.run(self._script("longform.py", "--auto"), "Daily: long-form (+promos+thumb)")
        self.run(self._script("redditstory.py", "--auto", "--count", "2"), "Daily: 2 reddit shorts")

    def _sched_create(self):
        if not IS_WIN:
            return messagebox.showinfo("Schedule", "Task Scheduler is Windows-only. On macOS use cron/launchd.")
        tr = f'"{BASE / "run_daily.bat"}" {self.sc_count.get()}'
        self.run(["schtasks", "/Create", "/TN", "ScrollAndTold", "/TR", tr, "/SC", "DAILY",
                  "/ST", self.sc_time.get(), "/F"], "Register daily task")

    def _sched_delete(self):
        if not IS_WIN:
            return messagebox.showinfo("Schedule", "Windows-only.")
        self.run(["schtasks", "/Delete", "/TN", "ScrollAndTold", "/F"], "Remove daily task")

    def _tab_folders(self):
        f = ttk.Frame(self.nb, padding=12); self.nb.add(f, text="Folders / Config")
        items = [("Long-form output", "output/longform"), ("Shorts output", "output/shorts"),
                 ("Compilations", "output/compilations"), ("Thumbnails", "output/thumbnails"),
                 ("Gameplay", "content/gameplay"), ("Music", "content/music"),
                 ("SFX", "content/sfx"), ("Ambient", "content/ambient"), ("Stories", "content/stories")]
        for i, (label, rel) in enumerate(items):
            ttk.Button(f, text=label, width=22, command=lambda r=rel: _open_path(BASE / r)).grid(
                row=i // 2, column=i % 2, sticky="w", padx=6, pady=4)
        r = (len(items) + 1) // 2
        ttk.Separator(f, orient="horizontal").grid(row=r, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Button(f, text="Edit config.reddit.yaml", width=22,
                   command=lambda: _open_path(BASE / "config.reddit.yaml")).grid(row=r + 1, column=0, sticky="w", padx=6)
        ttk.Button(f, text="Edit config.yaml", width=22,
                   command=lambda: _open_path(BASE / "config.yaml")).grid(row=r + 1, column=1, sticky="w", padx=6)

    def _tab_library(self):
        f = ttk.Frame(self.nb, padding=12); self.nb.add(f, text="Library / SEO")
        ttk.Label(f, text="List what you've made / what's pending, and (re)generate SEO.",
                  foreground="#888").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Button(f, text="Long-form videos", width=18,
                   command=lambda: self._list_dir("output/longform", "*.mp4")).grid(row=1, column=0, sticky="w", pady=2)
        ttk.Button(f, text="Shorts", width=18,
                   command=lambda: self._list_dir("output/shorts", "*.mp4")).grid(row=1, column=1, sticky="w")
        ttk.Button(f, text="Compilations", width=18,
                   command=lambda: self._list_dir("output/compilations", "*.mp4")).grid(row=1, column=2, sticky="w")
        ttk.Button(f, text="Upload queue", width=18,
                   command=lambda: self._show_file("output/upload_queue.txt")).grid(row=2, column=0, sticky="w", pady=2)
        ttk.Button(f, text="Used stories", width=18,
                   command=lambda: self._show_file("content/stories/used.txt")).grid(row=2, column=1, sticky="w")
        ttk.Button(f, text="Scheduled task", width=18, command=self._show_task).grid(row=2, column=2, sticky="w")
        ttk.Separator(f, orient="horizontal").grid(row=3, column=0, columnspan=4, sticky="ew", pady=10)
        ttk.Label(f, text="Generate title & description for a clip:").grid(row=4, column=0, columnspan=3, sticky="w")
        self.seo_clip = ttk.Combobox(f, width=50, values=self._all_clips()); self.seo_clip.grid(row=5, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Button(f, text="↻", width=3, command=lambda: self.seo_clip.configure(values=self._all_clips())).grid(row=5, column=2)
        ttk.Button(f, text="Generate SEO", command=self._run_seo).grid(row=6, column=0, sticky="w", pady=6)
        ttk.Button(f, text="Generate thumbnail", command=self._run_thumb).grid(row=6, column=1, sticky="w")
        ttk.Label(f, text="or a topic:").grid(row=7, column=0, sticky="w")
        self.seo_topic = ttk.Entry(f, width=50); self.seo_topic.grid(row=7, column=1, sticky="w")
        ttk.Button(f, text="SEO from topic", command=self._run_seo_topic).grid(row=8, column=0, sticky="w", pady=4)

    def _all_clips(self):
        out = []
        for sub in ("longform", "shorts"):
            d = BASE / "output" / sub
            if d.exists():
                out += sorted(p.stem for p in d.glob("*.mp4"))
        return out

    def _list_dir(self, rel, pattern):
        d = BASE / rel
        files = sorted(d.glob(pattern)) if d.exists() else []
        self._log(f"\n── {rel} ({len(files)} file[s]) ──\n")
        for p in files:
            self._log(f"  {p.name}  ({p.stat().st_size // 1048576} MB)\n")
        if not files:
            self._log("  (none)\n")

    def _show_file(self, rel):
        p = BASE / rel
        self._log(f"\n── {rel} ──\n")
        self._log((p.read_text(encoding="utf-8") if p.exists() else "(not found)") + "\n")

    def _show_task(self):
        if not IS_WIN:
            self._log("\n(Scheduled tasks are Windows-only.)\n"); return
        self.run(["schtasks", "/Query", "/TN", "ScrollAndTold", "/V", "/FO", "LIST"], "Query scheduled task")

    def _run_seo(self):
        clip = self.seo_clip.get().strip()
        if not clip:
            return messagebox.showwarning("SEO", "Pick a clip.")
        self.run(self._script("seo.py", "--input", clip), "Generate SEO")

    def _run_seo_topic(self):
        t = self.seo_topic.get().strip()
        if not t:
            return messagebox.showwarning("SEO", "Enter a topic.")
        self.run(self._script("seo.py", "--topic", t), "SEO from topic")

    def _run_thumb(self):
        clip = self.seo_clip.get().strip()
        if not clip:
            return messagebox.showwarning("Thumbnail", "Pick a clip.")
        self.run(self._script("thumb.py", "--input", clip), "Generate thumbnail")

    def _tab_doctor(self):
        f = ttk.Frame(self.nb, padding=12); self.nb.add(f, text="Doctor")
        ttk.Label(f, text="Check that everything's wired: torch/CUDA, ffmpeg, Ollama + models, "
                          "image/video model cache, YouTube secret/token, assets, disk.",
                  foreground="#888").grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Button(f, text="Run system check", command=lambda: self.run(
            self._script("doctor.py"), "System check")).grid(row=1, column=0, sticky="w")

    # ---- helpers ----------------------------------------------------------
    def _browse(self, entry):
        p = filedialog.askopenfilename(initialdir=str(BASE))
        if p:
            entry.delete(0, "end"); entry.insert(0, p)


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:  # noqa: BLE001
        pass
    Studio(root)
    root.mainloop()


if __name__ == "__main__":
    main()
