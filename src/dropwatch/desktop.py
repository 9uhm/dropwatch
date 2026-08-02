"""Desktop integration: opening browser windows, the tray icon, and detaching.

All of it is best-effort by design. A machine with no browser, no notification
area, or no console must still watch streams — so every function here degrades to
a log line rather than raising into the watch loop.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser  # noqa: F401 — patched by tests via this module
from typing import TYPE_CHECKING, Any

from . import paths
from .log import get_logger

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

log = get_logger("desktop")

TWITCH_BASE = "https://www.twitch.tv/"


# --------------------------------------------------------------------- browser


def open_url(url: str, *, what: str = "page") -> bool:
    """Open a URL in the default browser. Never raises."""
    try:
        opened = webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 — a browser failure is not fatal
        log.debug("could not open %s (%s): %s", what, url, exc)
        return False
    if not opened:
        log.debug("no browser available to open %s", what)
    return opened


def open_channel(login: str) -> bool:
    return open_url(f"{TWITCH_BASE}{login}", what="twitch channel")


# -------------------------------------------------------------------- detaching


def detach_and_exit(argv: list[str], *, host: str, port: int) -> int:
    """Relaunch this program without a console, then return an exit code.

    Windows keeps a child alive after its parent's console closes only if the
    child has no console of its own, hence DETACHED_PROCESS. Without
    CREATE_NEW_PROCESS_GROUP a Ctrl-C in the original window would also reach the
    detached copy, which defeats the point.

    The child's output goes nowhere, so its failures are invisible — a port
    already in use would otherwise be reported here as a successful launch. So
    this waits for the dashboard to answer before claiming anything.
    """
    if os.name != "nt":
        log.error("--detach is implemented for Windows only")
        return 1

    # CREATE_NO_WINDOW *only* — deliberately not DETACHED_PROCESS.
    #
    # Win32 treats those two as mutually exclusive, and when both are passed
    # DETACHED_PROCESS wins. That means "do not inherit the parent's console",
    # which makes a console-subsystem binary allocate a brand new console — so the
    # supposedly headless child pops up an empty black window. CREATE_NO_WINDOW
    # gives it a console with no window at all, which is what headless means here.
    #
    # CREATE_NEW_PROCESS_GROUP keeps a Ctrl-C in the launching window from also
    # reaching the background copy.
    creationflags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    )

    command = _relaunch_command(argv)
    try:
        child = subprocess.Popen(  # noqa: S603 — argv is our own, not user input
            command,
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            cwd=str(paths.ROOT),
            env=_child_env(),
        )
    except OSError as exc:
        log.error("could not detach: %s", exc)
        return 1

    if not _wait_for_dashboard(host, port, child):
        print(
            f"\n  Started pid {child.pid}, but nothing is answering on "
            f"http://{host}:{port}/ .\n"
            "  It most likely failed to bind that port. Check for another copy:\n"
            "      dropwatch stop\n"
            "  ...then run `dropwatch serve` in this window to see the error.\n",
            file=sys.stderr,
        )
        return 1

    write_pid(child.pid)
    print(f"\n  Running in the background (pid {child.pid}).")
    print(f"  Dashboard  http://{host}:{port}/")
    print("  This window can be closed.\n")
    print("  Stop it from the tray icon, or with:  dropwatch stop\n")
    return 0


def _wait_for_dashboard(
    host: str, port: int, child: subprocess.Popen[bytes], timeout: float = 20.0
) -> bool:
    """Poll until the detached child's dashboard accepts a connection."""
    import socket
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return False  # it exited outright
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.4)
    return False


#: PyInstaller advertises its unpack directory to child processes through these.
#: Inherited, the bootloader *reuses the parent's* extraction directory instead of
#: making its own — so when the parent exits and cleans up, the detached child's
#: bundled files disappear from under it and every asset read starts failing.
#: Only reproducible in the frozen-and-detached combination, which is the one
#: people actually run.
_PYI_ENV_KEYS = ("_MEIPASS2", "_PYI_ARCHIVE_FILE", "_PYI_APPLICATION_HOME_DIR",
                 "_PYI_PARENT_PROCESS_LEVEL", "_PYI_SPLASH_IPC")


def _child_env() -> dict[str, str]:
    """Our environment with PyInstaller's bootloader hand-off removed."""
    env = dict(os.environ)
    for key in _PYI_ENV_KEYS:
        env.pop(key, None)
    # Belt and braces: future releases may add more of these.
    for key in [k for k in env if k.startswith("_PYI_")]:
        env.pop(key, None)
    return env


def _relaunch_command(argv: list[str]) -> list[str]:
    """Rebuild our own command line with --detach stripped out."""
    args = [a for a in argv if a != "--detach"]
    if paths.FROZEN:
        return [sys.executable, *args]
    # Re-enter via -m so the package is imported properly rather than run as a
    # loose script, which would break the relative imports in __main__.
    return [sys.executable, "-m", "dropwatch", *args]


# ------------------------------------------------------------------- pid file


def write_pid(pid: int | None = None) -> None:
    try:
        paths.ensure_data_dir()
        paths.PID_FILE.write_text(str(pid if pid is not None else os.getpid()), "utf-8")
    except OSError as exc:
        log.debug("could not write pid file: %s", exc)


def read_pid() -> int | None:
    try:
        return int(paths.PID_FILE.read_text("utf-8").strip())
    except (OSError, ValueError):
        return None


def clear_pid() -> None:
    try:
        paths.PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def process_alive(pid: int) -> bool:
    """Whether a pid is running. Used to tell a stale pid file from a live one."""
    if os.name == "nt":
        try:
            out = subprocess.run(  # noqa: S603
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate(pid: int) -> bool:
    if os.name == "nt":
        try:
            result = subprocess.run(  # noqa: S603
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0
    try:
        os.kill(pid, 15)
    except OSError:
        return False
    return True


# ----------------------------------------------------------------------- tray


#: The mark's palette, matching the dashboard's own tokens.
_INK = (11, 15, 22, 255)
_ACCENT = (169, 112, 255, 255)
_ACCENT_DIM = (109, 69, 168, 255)


def icon_image(size: int = 64) -> Any:
    """Draw the app mark: a violet drop on a dark rounded square.

    Drawn rather than loaded so the tray never depends on an asset surviving the
    bundle, and rendered proportionally at whatever size is asked for so the 16px
    version keeps its shape instead of being a smeared downscale of the 256px one.
    ``packaging/make_icon.py`` bakes the same drawing into the .ico that Windows
    needs for the executable and the title bar.

    Everything here is deliberately one shape and one colour. At 16px — the tray,
    where this is seen most — anything more resolves to a smudge.
    """
    from PIL import Image, ImageDraw

    # Supersample: PIL has no antialiasing for polygons, and the drop's tip is a
    # diagonal that looks visibly stepped without it.
    ss = 8 if size <= 64 else 4
    px = size * ss
    image = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    pad = px * 0.055
    draw.rounded_rectangle(
        [pad, pad, px - pad, px - pad], radius=px * 0.22, fill=_INK, outline=_ACCENT_DIM,
        width=max(1, int(px * 0.012)),
    )

    # A teardrop: a circle for the bulb, a triangle for the tip, sharing a chord.
    # Proportions chosen so the silhouette still reads as a drop at 16px, where a
    # narrower classic teardrop turns into a featureless blob.
    cx = px / 2
    bulb_r = px * 0.215
    bulb_cy = px * 0.605
    tip_y = px * 0.235
    # Where the triangle's sides meet the circle tangentially — computed rather
    # than eyeballed so the join stays smooth at every size.
    dy = bulb_cy - tip_y
    sin_t = bulb_r / dy
    cos_t = (1 - sin_t * sin_t) ** 0.5
    join_dx = bulb_r * cos_t
    join_dy = bulb_r * sin_t

    draw.ellipse(
        [cx - bulb_r, bulb_cy - bulb_r, cx + bulb_r, bulb_cy + bulb_r], fill=_ACCENT
    )
    draw.polygon(
        [
            (cx, tip_y),
            (cx - join_dx, bulb_cy - join_dy),
            (cx + join_dx, bulb_cy - join_dy),
        ],
        fill=_ACCENT,
    )

    return image.resize((size, size), Image.LANCZOS)


#: Kept as the old private name so nothing that imported it breaks.
_icon_image = icon_image


class Tray:
    """System-tray icon. Optional, and silently absent if unsupported.

    pystray owns a native message loop, so it runs on its own thread and hands
    work back to the event loop with ``run_coroutine_threadsafe``. Calling into
    asyncio directly from a menu callback would corrupt the loop.
    """

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        dashboard_url: str,
        on_pause: Callable[[], Any],
        on_resume: Callable[[], Any],
        on_quit: Callable[[], Any],
        current_channel: Callable[[], str | None],
        is_paused: Callable[[], bool],
        on_show: Callable[[], Any] | None = None,
        on_hide: Callable[[], Any] | None = None,
        is_hidden: Callable[[], bool] | None = None,
    ) -> None:
        self._loop = loop
        self._url = dashboard_url
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_quit = on_quit
        self._channel = current_channel
        self._paused = is_paused
        #: Set only when a native window exists. Without them the menu falls back
        #: to opening the dashboard in a browser, which is all ``serve`` can do.
        self._on_show = on_show
        self._on_hide = on_hide
        self._is_hidden = is_hidden
        self._icon: Any = None
        self._thread: threading.Thread | None = None

    @property
    def windowed(self) -> bool:
        return self._on_show is not None

    def _submit(self, factory: Callable[[], Any]) -> None:
        """Run a coroutine on the watcher's loop from this native thread."""
        import asyncio as _asyncio

        try:
            _asyncio.run_coroutine_threadsafe(factory(), self._loop)
        except Exception as exc:  # noqa: BLE001 — a menu click must not crash the tray
            log.debug("tray action failed: %s", exc)

    def start(self) -> bool:
        try:
            from pystray import Icon, Menu, MenuItem
        except Exception as exc:  # noqa: BLE001 — optional dependency
            log.info("tray unavailable (%s); running without it", exc)
            return False

        def toggle(_icon: Any, _item: Any) -> None:
            self._submit(self._on_resume if self._paused() else self._on_pause)

        def open_dash(_icon: Any, _item: Any) -> None:
            """The default action: raise our own window if we have one."""
            if self._on_show is not None:
                try:
                    self._on_show()
                except Exception as exc:  # noqa: BLE001 — a menu click must not crash
                    log.debug("could not show the window: %s", exc)
                return
            open_url(self._url, what="dashboard")

        def hide_window(_icon: Any, _item: Any) -> None:
            if self._on_hide is None:
                return
            try:
                self._on_hide()
            except Exception as exc:  # noqa: BLE001
                log.debug("could not hide the window: %s", exc)

        def open_twitch(_icon: Any, _item: Any) -> None:
            login = self._channel()
            if login:
                open_channel(login)

        def quit_(icon: Any, _item: Any) -> None:
            self._submit(self._on_quit)
            icon.stop()

        # Double-clicking the icon fires the default item, so that has to be the
        # one that brings the app back — it's the gesture everyone tries first.
        window_items: tuple[Any, ...] = ()
        if self.windowed:
            window_items = (
                MenuItem("Hide window", hide_window,
                         enabled=lambda _: not (self._is_hidden and self._is_hidden())),
            )

        menu = Menu(
            MenuItem(lambda _: f"dropwatch — {self._channel() or 'idle'}", open_dash,
                     default=True, enabled=True),
            Menu.SEPARATOR,
            MenuItem("Open dashboard" if not self.windowed else "Show window", open_dash),
            *window_items,
            MenuItem(lambda _: f"Open twitch.tv/{self._channel()}" if self._channel()
                     else "Open Twitch (no target)", open_twitch,
                     enabled=lambda _: self._channel() is not None),
            Menu.SEPARATOR,
            MenuItem(lambda _: "Resume watching" if self._paused() else "Pause watching",
                     toggle),
            MenuItem("Quit", quit_),
        )

        try:
            self._icon = Icon("dropwatch", _icon_image(), "dropwatch", menu)
        except Exception as exc:  # noqa: BLE001
            log.info("could not create tray icon (%s); running without it", exc)
            return False

        self._thread = threading.Thread(
            target=self._run, name="tray", daemon=True
        )
        self._thread.start()
        log.info("tray icon active")
        return True

    def _run(self) -> None:
        try:
            self._icon.run()
        except Exception as exc:  # noqa: BLE001 — never take the process down
            log.debug("tray loop ended: %s", exc)

    def refresh(self) -> None:
        """Re-render the menu after state changes, so labels stay truthful."""
        if self._icon is not None:
            try:
                self._icon.update_menu()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
            self._icon = None
