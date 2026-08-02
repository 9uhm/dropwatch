# PyInstaller spec for dropwatch.
#
# Build:  .venv/Scripts/python -m PyInstaller dropwatch.spec --noconfirm
# Output: dist/dropwatch.exe  (single file, no Python install needed)
#
# The exe keeps its state *beside itself* -- data/, config.toml, .env -- because
# paths.app_root() returns the executable's directory when frozen. The dashboard
# HTML rides inside the bundle instead, since it's read-only.

from PyInstaller.utils.hooks import collect_submodules

# Anything the dashboard loads at runtime has to be listed here. stats.js is
# fetched by the page rather than inlined, so omitting it yields a frozen build
# that serves the dashboard with silently missing charts.
# The .lint.js / .test.js files are dev tooling and deliberately not shipped.
datas = [
    ("ui/dashboard.html", "ui"),
    ("ui/stats.js", "ui"),
    ("ui/console.html", "ui"),
    ("config.example.toml", "."),
    # Windows needs a real file for the window/taskbar icon, so unlike the tray
    # -- which draws its own at runtime -- this one has to ride in the bundle.
    ("packaging/dropwatch.ico", "."),
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
    # The tray. pystray picks its backend at import time, so the Windows one has
    # to be named explicitly or the frozen build finds no backend and falls back
    # to "tray unavailable" -- which looks like a platform problem, not a build one.
    "pystray",
    "pystray._win32",
    "PIL.Image",
    "PIL.ImageDraw",
    # The app window. pywebview picks its backend at import time the same way
    # pystray does, so the WinForms/WebView2 one is named explicitly; without it
    # a frozen build reports "no window available" and silently falls back to the
    # browser, which looks like a missing WebView2 runtime rather than a bad spec.
    #
    # pythonnet's own files (Python.Runtime.dll, the clr_loader runtime configs)
    # come from the bundled hooks for `clr`, `clr_loader` and `webview` — they
    # cannot be listed here because they are data, not importable modules.
    "webview",
    "webview.platforms.winforms",
    "clr",
    "clr_loader",
]

excludes = [
    # Nothing in the app touches these; excluding them roughly halves the exe.
    "tkinter",
    "unittest",
    "pytest",
    "pydoc",
    "doctest",
    "email.test",
    "numpy",
    "discord",  # phase 5 -- not wired up yet, no need to ship it
    # PIL is deliberately NOT excluded: pystray needs it to build the tray icon.
    # Only the two image modules below are reachable, so the rest is trimmed via
    # PIL.ImageShow etc. staying unimported rather than by excluding the package.
    "PIL.ImageQt",
    "PIL.ImageTk",
    "PIL.ImageShow",
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
    icon="packaging/dropwatch.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX trips a lot of antivirus heuristics; not worth it
    # Windowed subsystem: Windows never creates a console for this process, so
    # there is never one to flash, hide, or leave sitting behind the app.
    #
    # The obvious alternative -- console=True with hide_console="hide-early" --
    # was tried and does not hold up. It hides whatever GetConsoleWindow()
    # returns, which is the classic conhost window; when Windows Terminal is the
    # default terminal host (the default on Windows 11) the visible window belongs
    # to WindowsTerminal.exe instead, so there is nothing for us to hide and the
    # console stays on screen. It "works" on a conhost machine and fails on the
    # next one, which is the worst kind of working.
    #
    # CLI output survives this: main() calls attach_parent_console(), which
    # borrows the terminal's console when there is one. See log.py.
    console=False,
    # Keep the windowed traceback dialog: with no console, a crash before logging
    # is configured would otherwise be completely silent.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
