"""Shared helpers for the AI Presenter pipeline (config loading, paths, logging).

Kept tiny and dependency-light so each pipeline script stays standalone-runnable
(`python pipeline/generate.py`) per PIPELINE.md §6.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import yaml

# automation/ is the parent of pipeline/. All config paths resolve against it.
AUTOMATION_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = AUTOMATION_DIR / "config.yaml"


def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger("pipeline")


def load_config(config_path: str | None = None) -> dict:
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    if not path.exists():
        sys.exit(f"[fatal] config not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def resolve(path_str: str) -> Path:
    """Resolve a config path (relative -> under automation/, absolute kept as-is)."""
    p = Path(path_str)
    return p if p.is_absolute() else (AUTOMATION_DIR / p)


def get_path(config: dict, key: str) -> Path:
    """Look up config['paths'][key] and resolve it to an absolute Path."""
    try:
        return resolve(config["paths"][key])
    except KeyError:
        sys.exit(f"[fatal] paths.{key} missing from config.yaml")


def rotate_pick(name: str, options: list):
    """Round-robin over `options`, persisting the position in .state/<name> so
    successive runs cycle 'in turns'. Falls back to options[0] on any error."""
    if not options:
        return None
    if len(options) == 1:
        return options[0]
    state_dir = AUTOMATION_DIR / ".state"
    state_dir.mkdir(exist_ok=True)
    f = state_dir / f"{name}.txt"
    try:
        idx = int(f.read_text().strip()) if f.exists() else 0
    except (OSError, ValueError):
        idx = 0
    choice = options[idx % len(options)]
    try:
        f.write_text(str((idx + 1) % len(options)))
    except OSError:
        pass
    return choice
