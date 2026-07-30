# PyInstaller spec for dropwatch.
#
# Build:  .venv/Scripts/python -m PyInstaller dropwatch.spec --noconfirm
# Output: dist/dropwatch.exe  (single file, no Python install needed)
#
# The exe keeps its state *beside itself* -- data/, config.toml, .env -- because
# paths.app_root() returns the executable's directory when frozen. The dashboard
# HTML rides inside the bundle instead, since it's read-only.

from PyInstaller.utils.hooks import collect_submodules

datas = [
    ("ui/dashboard.html", "ui"),
    ("ui/console.html", "ui"),
    ("config.example.toml", "."),
]

hiddenimports = [
    # pydantic/pydantic-settings resolve a lot dynamically, so let the hook
    # collector find them rather than guessing at module names.
    *collect_submodules("pydantic"),
    *collect_submodules("pydantic_settings"),
    # aiohttp's WebSocket path pulls these in lazily.
    "aiohttp",
    "multidict",
    "yarl",
    "propcache",
    "aiohappyeyeballs",
    # tomllib backs the TOML settings source; sqlite3 backs the store.
    "tomllib",
    "sqlite3",
]

excludes = [
    # Nothing in the app touches these; excluding them roughly halves the exe.
    "tkinter",
    "unittest",
    "pytest",
    "pydoc",
    "doctest",
    "email.test",
    "PIL",
    "numpy",
    "discord",  # phase 5 -- not wired up yet, no need to ship it
]

a = Analysis(
    # Not src/dropwatch/__main__.py: PyInstaller runs the entry script as __main__
    # with no package context, so its relative imports would fail at startup.
    ["packaging/entry.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="dropwatch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX trips a lot of antivirus heuristics; not worth it
    console=True,       # the log output *is* the interface without Discord
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
