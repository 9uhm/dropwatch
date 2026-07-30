"""Filesystem layout. Everything mutable lives under ``data/``.

Two roots, and the distinction matters once this is packaged as an ``.exe``:

* :data:`ROOT` is where *mutable, user-owned* files live — ``config.toml``,
  ``.env``, ``data/``. Frozen, that's the directory holding the executable, so
  someone can edit config next to the exe and have it take effect.
* :func:`bundle_dir` is where *read-only bundled assets* live — the dashboard
  HTML. Frozen, PyInstaller unpacks those to a temp directory that is deleted on
  exit, so nothing writable may ever live there.

Getting these backwards yields an exe that either cannot find its own web page or
silently discards the user's tokens and config on every run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: True when running from a PyInstaller bundle.
FROZEN = getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def app_root() -> Path:
    """Directory holding user-owned state. Override with ``DROPWATCH_HOME``."""
    env = os.environ.get("DROPWATCH_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if FROZEN:
        # Beside the .exe — not the temp unpack dir, which vanishes on exit.
        return Path(sys.executable).resolve().parent
    # src/dropwatch/paths.py -> src/dropwatch -> src -> root
    return Path(__file__).resolve().parents[2]


def bundle_dir() -> Path:
    """Directory holding read-only assets shipped with the app."""
    if FROZEN:
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]  # noqa: SLF001
    return Path(__file__).resolve().parents[2]


ROOT = app_root()
BUNDLE = bundle_dir()
DATA_DIR = ROOT / "data"
CONFIG_FILE = ROOT / "config.toml"
ENV_FILE = ROOT / ".env"
DB_PATH = DATA_DIR / "dropwatch.db"
TOKEN_PATH = DATA_DIR / "tokens.json"


def ui_file(name: str) -> Path:
    """Locate a UI asset, preferring an editable copy beside the app.

    That ordering lets a packaged build be re-skinned by dropping a ``ui/`` folder
    next to the exe, with no rebuild.
    """
    override = ROOT / "ui" / name
    if override.is_file():
        return override
    return BUNDLE / "ui" / name


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
