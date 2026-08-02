"""Filesystem layout. Everything mutable lives under ``data/``.

Two roots, and the distinction matters once this is packaged as an ``.exe``:

* :data:`ROOT` is where *mutable, user-owned* files live — ``config.toml``,
  ``.env``, ``data/``. Frozen, that defaults to ``%LOCALAPPDATA%\\dropwatch``, so
  the executable is a file you can keep on the Desktop and move or replace freely
  without taking your tokens and history with it. See :func:`app_root` for the
  portable escape hatch.
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


#: Dropped beside the executable to opt back in to keeping state there. For a
#: USB stick or a folder you want to be able to move wholesale.
PORTABLE_MARKER = "dropwatch.portable"


def user_data_dir() -> Path:
    """The per-user state directory this platform expects an app to use."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "dropwatch"
        return Path.home() / "AppData" / "Local" / "dropwatch"
    # XDG on everything else, with the spec's own fallback.
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "dropwatch"
    return Path.home() / ".local" / "share" / "dropwatch"


def app_root() -> Path:
    """Directory holding user-owned state.

    Resolution order, most explicit first:

    1. ``DROPWATCH_HOME`` — an outright instruction.
    2. **Portable mode**, when the executable's folder holds a
       ``dropwatch.portable`` marker or an existing ``data/``. The second case is
       what keeps an upgrade from silently abandoning the tokens and history of
       an install that has been running beside its exe.
    3. ``%LOCALAPPDATA%\\dropwatch`` — the default, so the exe itself is just a
       file you can leave on the Desktop, move, or delete without losing state.
       Writing beside the exe also fails outright under Program Files, and puts
       a database in whatever folder someone happened to download it to.

    Running from a source checkout is unchanged: state stays in the repo, where
    development expects it.
    """
    env = os.environ.get("DROPWATCH_HOME")
    if env:
        return Path(env).expanduser().resolve()

    if not FROZEN:
        # src/dropwatch/paths.py -> src/dropwatch -> src -> root
        return Path(__file__).resolve().parents[2]

    beside_exe = Path(sys.executable).resolve().parent
    if (beside_exe / PORTABLE_MARKER).exists() or (beside_exe / "data").is_dir():
        return beside_exe
    return user_data_dir()


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
#: Where a single-account install kept its token. Still read once, to migrate it
#: into ``accounts/``; never written again.
TOKEN_PATH = DATA_DIR / "tokens.json"
#: One file per Twitch account, named for its login.
ACCOUNTS_DIR = DATA_DIR / "accounts"
#: Written by a detached run so `dropwatch stop` can find it again.
PID_FILE = DATA_DIR / "dropwatch.pid"


def ui_file(name: str) -> Path:
    """Locate a UI asset, preferring an editable copy beside the app.

    That ordering lets a packaged build be re-skinned by dropping a ``ui/`` folder
    next to the exe, with no rebuild.
    """
    override = ROOT / "ui" / name
    if override.is_file():
        return override
    return BUNDLE / "ui" / name


def icon_file() -> Path | None:
    """The .ico for the window and taskbar, or ``None`` if it wasn't shipped.

    Windows wants a *file* for a window icon, so unlike the tray — which draws
    its own — this one has to survive into the bundle. Returning None rather than
    a missing path keeps a stripped build from failing at window creation.

    Note the search order is over :data:`BUNDLE`, not :data:`ROOT`: this is a
    read-only shipped asset, and ROOT is wherever the user's state lives — which
    ``DROPWATCH_HOME`` can point anywhere at all. Looking there first found
    nothing, and the window silently fell back to wearing python.exe's icon.
    """
    for candidate in (
        ROOT / "dropwatch.ico",              # user override, beside the exe
        BUNDLE / "dropwatch.ico",            # frozen: datas puts it at the root
        BUNDLE / "packaging" / "dropwatch.ico",  # source tree
    ):
        if candidate.is_file():
            return candidate
    return None


def ensure_root() -> Path:
    """Create the state directory, seeding a config on first run.

    With state in ``%LOCALAPPDATA%`` the folder is not one anybody browses to by
    accident, so leaving it empty-but-for-a-database makes it look like scratch
    space. Copying the commented example in gives whoever opens it something to
    edit, and matches what living beside the exe used to provide for free.
    """
    ROOT.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        example = BUNDLE / "config.example.toml"
        if example.is_file():
            try:
                CONFIG_FILE.write_text(example.read_text("utf-8"), encoding="utf-8")
            except OSError:
                pass  # defaults work fine without it
    return ROOT


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def ensure_accounts_dir() -> Path:
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    return ACCOUNTS_DIR


def token_path(account: str) -> Path:
    """Where one account's tokens live.

    The name is a Twitch login, which is always ``[A-Za-z0-9_]`` — but it arrives
    from a network response, and this builds a filesystem path, so it is checked
    rather than trusted. A login containing a separator would otherwise write
    outside ``accounts/``.
    """
    if not is_valid_account_name(account):
        raise ValueError(f"unusable account name: {account!r}")
    return ACCOUNTS_DIR / f"{account.lower()}.json"


def is_valid_account_name(name: str) -> bool:
    import re

    return bool(re.fullmatch(r"[A-Za-z0-9_]{1,40}", name or ""))
