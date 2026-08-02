"""Desktop application shell — a real window around the local dashboard.

The app people actually want is a thing you double-click: it opens, it keeps
running when you close it, and double-clicking again brings it back. That needs
three pieces that the ``serve`` command has no use for, which is why they live
here rather than in :mod:`dropwatch.web`.

**A window, not a browser tab.** Closing a tab cannot mean "keep running, drop
out of the taskbar, stay in the tray" — the page has no say in any of it. A
WebView2 window can: the close is cancelled and the form hidden, which on Windows
removes the taskbar button as a side effect of ``Visible = false``. The page
served inside it is byte-for-byte the one ``serve`` serves.

**Two loops on two threads.** WebView2 and asyncio both want to own the process.
The native message pump has to be on the main thread — WinForms will not run
anywhere else — so the watcher, the dashboard and the tray get a dedicated thread
with their own event loop, and the two sides talk through thread-safe calls only.
Every method here is annotated with the thread it may be called from; getting that
wrong produces hangs that reproduce once a week and never under a debugger.

**Degrading to a browser.** WebView2 ships with Edge, so it is present on any
current Windows — but "present on a normal machine" is not "present". Without it
the app still runs, still watches, still keeps its tray icon, and opens the
dashboard in the default browser instead of a window.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from typing import TYPE_CHECKING, Any

from . import paths
from .app import App
from .desktop import Tray, clear_pid, open_channel, open_url, write_pid
from .events import Event, EventType
from .log import get_logger
from .web import Dashboard, ShellHooks

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

log = get_logger("gui")

TITLE = "dropwatch"
DEFAULT_SIZE = (1180, 840)
MIN_SIZE = (860, 560)
#: Matches the dashboard's own background, so there is no white flash on open.
BACKGROUND = "#0B0F16"

#: Window styles. Setting ``FormBorderStyle.None`` for a frameless window also
#: drops the resize border and the maximise box, which takes Aero Snap and
#: edge-dragging with it. Putting these two bits back leaves the frame invisible
#: — the page draws its own — while the default window procedure keeps handling
#: hit-testing, so the window resizes and snaps like any other.
_GWL_STYLE = -16
_WS_THICKFRAME = 0x00040000
_WS_MAXIMIZEBOX = 0x00010000


#: Identifies the app to the Windows shell. Without one, a window hosted by
#: python.exe is grouped under *Python* on the taskbar and wears python.exe's
#: icon, whatever icon the window itself sets — the shell resolves the taskbar
#: icon through the AppUserModelID, not the window. Frozen, the exe supplies its
#: own, but setting this explicitly makes both cases behave identically.
APP_ID = "dropwatch.app"


def _set_app_id(app_id: str = APP_ID) -> None:
    """Claim our own taskbar identity. Windows only; never raises."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception as exc:  # noqa: BLE001 — cosmetic; never worth failing over
        log.debug("could not set the app id: %s", exc)


def _restore_resize_border(pid: int) -> bool:
    """Re-enable resizing on our frameless window. Windows only; never raises."""
    import ctypes
    from ctypes import wintypes

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except OSError:
        return False

    # Explicit argtypes are not optional here: HWND is pointer-sized, and letting
    # ctypes guess truncates every handle to 32 bits on a 64-bit build. The
    # symptom is a function that succeeds against a window that isn't ours.
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_longlong
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
    user32.SetWindowLongPtrW.restype = ctypes.c_longlong

    targets: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _collect(hwnd: int, _: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            targets.append(hwnd)
        return True

    try:
        user32.EnumWindows(ctypes.cast(_collect, ctypes.c_void_p), 0)
        for hwnd in targets:
            style = user32.GetWindowLongPtrW(hwnd, _GWL_STYLE)
            user32.SetWindowLongPtrW(
                hwnd, _GWL_STYLE, style | _WS_THICKFRAME | _WS_MAXIMIZEBOX
            )
    except Exception as exc:  # noqa: BLE001 — a fixed-size window still works
        log.debug("could not restore the resize border: %s", exc)
        return False
    return bool(targets)

#: How long to wait for the watcher, dashboard and tray to come up before giving
#: up on the launch. Generous: the first run has a cold SQLite file to create and
#: a token to validate against Twitch.
STARTUP_TIMEOUT = 90.0


class Runtime:
    """The watcher, dashboard and tray, on a private event loop and thread.

    Constructed on the main thread; everything else happens on the runtime
    thread. The only members the main thread may touch after :meth:`start` are
    :attr:`url`, :attr:`error`, and :meth:`request_stop`.
    """

    def __init__(
        self,
        args: argparse.Namespace,
        *,
        host: str,
        port: int,
        hooks: ShellHooks,
        on_stopped: Callable[[], None],
    ) -> None:
        self._args = args
        self._hooks = hooks
        self._on_stopped = on_stopped
        self.host = host
        self.port = port
        self.url = ""
        self.error: str | None = None
        #: Set once the dashboard is listening *or* the launch has failed. The
        #: main thread blocks on this, so every exit path must set it.
        self.ready = threading.Event()

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_signal: asyncio.Event | None = None
        self._app: App | None = None
        #: One task per account, keyed by account name. A dict rather than a
        #: list because accounts come and go while running — signing one in
        #: from the window adds a task without disturbing the others.
        self._watch_tasks: dict[str, asyncio.Task[None]] = {}
        self._tray: Tray | None = None

    # ------------------------------------------------------------ main thread

    def start(self, timeout: float = STARTUP_TIMEOUT) -> bool:
        """Boot the runtime and block until it is serving. Main thread."""
        self._thread = threading.Thread(target=self._thread_main, name="runtime", daemon=True)
        self._thread.start()
        if not self.ready.wait(timeout):
            self.error = f"the watcher did not start within {timeout:.0f}s"
        return self.error is None and bool(self.url)

    def request_stop(self) -> None:
        """Ask the runtime to shut down. Safe from any thread, and idempotent."""
        loop, signal = self._loop, self._stop_signal
        if loop is None or signal is None:
            return
        with contextlib.suppress(RuntimeError):  # loop already closed
            loop.call_soon_threadsafe(signal.set)

    def join(self, timeout: float = 30.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # --------------------------------------------------------- runtime thread

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._amain())
        except Exception as exc:  # noqa: BLE001 — must reach the main thread as text
            self.error = str(exc)
            log.exception("the runtime failed")
        finally:
            # Belt and braces: a crash before _amain got as far as setting this
            # would otherwise leave the main thread waiting out the full timeout.
            self.ready.set()
            self._on_stopped()

    async def _amain(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_signal = asyncio.Event()

        async with App() as app:
            self._app = app
            dashboard = Dashboard(
                watcher=app.watcher, bus=app.bus, store=app.store, drops=app.drops,
                config=app.config_manager, auth=app.auth, hooks=self._hooks,
                app=app,
            )
            try:
                self.url = await dashboard.start(self.host, self.port)
            except OSError as exc:
                self.error = (
                    f"could not bind {self.host}:{self.port} — {exc}. "
                    "Another copy may be running; try `dropwatch stop`."
                )
                return

            write_pid()
            self._start_tray()
            self._wire_twitch_opening()

            started = self.start_watcher()
            if not started:
                # Not an error, and specifically not a reason to refuse to start:
                # the window is how someone signs in, so exiting here would leave
                # a first-time user with an exe that closes instantly.
                log.warning("no account signed in — use the window to connect one")

            self.ready.set()
            try:
                await self._stop_signal.wait()
            finally:
                await self._shutdown(dashboard)

    def start_watcher(self, account: str | None = None) -> int:
        """Start watching on every signed-in account that isn't already. Runtime thread.

        Returns how many are now running. Also the hook the dashboard calls after
        an in-app sign-in — which is why it is idempotent and takes an optional
        name rather than assuming a cold start with a fixed set of accounts.
        """
        if self._app is None:
            return 0

        wanted = [
            a for a in self._app.accounts
            if a.ready and (account is None or a.name == account)
        ]
        for acc in wanted:
            existing = self._watch_tasks.get(acc.name)
            if existing is not None and not existing.done():
                continue
            self._watch_tasks[acc.name] = asyncio.create_task(
                acc.watcher.run(), name=f"watcher:{acc.name}"
            )
            log.info("watching started for %s", acc.name)

        live = sum(1 for t in self._watch_tasks.values() if not t.done())
        if self._tray is not None:
            self._tray.refresh()
        return live

    def _wire_twitch_opening(self) -> None:
        assert self._app is not None
        app = self._app
        # Per account: with a fleet running, "only open the first one" would mean
        # the second account silently never opens, which reads as a bug.
        opened: set[str] = set()

        async def on_watch_started(event: Event) -> None:
            channel = event.get("channel")
            if not channel or not app.config.ui.open_twitch:
                return
            who = str(event.get("account") or "")
            if who in opened and not app.config.ui.reopen_twitch_on_rotate:
                return
            opened.add(who)
            open_channel(str(channel))

        app.bus.subscribe(on_watch_started, EventType.WATCH_STARTED)

    def _start_tray(self) -> None:
        assert self._app is not None
        app = self._app
        if not app.config.ui.tray:
            log.info("tray disabled by configuration")
            return

        tray = Tray(
            loop=asyncio.get_running_loop(),
            dashboard_url=self.url,
            on_pause=self._pause_all,
            on_resume=self._resume_all,
            on_quit=self._quit_from_tray,
            current_channel=self._describe_targets,
            # Paused only when *everything* is: a half-paused fleet offering
            # "Resume" is the more useful of the two readings.
            is_paused=lambda: bool(app.accounts) and all(
                a.watcher.status().paused for a in app.accounts
            ),
            on_show=self._hooks.show,
            on_hide=self._hooks.hide,
        )
        if not tray.start():
            return
        self._tray = tray
        app.bus.subscribe(
            lambda _e: tray.refresh(),
            EventType.WATCH_STARTED, EventType.TARGET_SWITCHED,
            EventType.STATE_CHANGED, EventType.WATCH_STOPPED,
        )

    def _describe_targets(self) -> str | None:
        """What the tray shows: the channel, or how many accounts are on it."""
        app = self._app
        if app is None:
            return None
        channels = [
            a.watcher.status().channel for a in app.accounts if a.watcher.status().channel
        ]
        if not channels:
            return None
        unique = sorted(set(channels))
        if len(unique) == 1:
            return unique[0] if len(channels) == 1 else f"{unique[0]} ×{len(channels)}"
        return f"{len(channels)} accounts, {len(unique)} channels"

    async def _pause_all(self) -> None:
        for account in (self._app.accounts if self._app else []):
            await account.watcher.pause()

    async def _resume_all(self) -> None:
        for account in (self._app.accounts if self._app else []):
            await account.watcher.resume()

    async def _quit_from_tray(self) -> None:
        """Tray "Quit". A coroutine because Tray submits it to this loop."""
        self.request_stop()

    async def _shutdown(self, dashboard: Dashboard) -> None:
        log.info("shutting down")
        if self._app is not None:
            for account in self._app.accounts:
                if self._watch_tasks.get(account.name) is not None:
                    await account.watcher.stop()
            for task in self._watch_tasks.values():
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._watch_tasks.clear()
        if self._tray is not None:
            self._tray.stop()
            self._tray = None
        await dashboard.stop()
        clear_pid()


class Window:
    """A WebView2 window, or a graceful admission that there isn't one.

    ``create`` and ``run`` are main-thread only. ``show``, ``hide`` and
    ``destroy`` are callable from anywhere — pywebview marshals them onto the UI
    thread itself.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._webview: Any = None
        self._window: Any = None
        self._hidden = False
        self._maximized = False
        #: Distinguishes "the user pressed X" from "we are really closing", since
        #: both arrive at the same closing handler.
        self._quitting = False
        self._running = threading.Event()
        self._styled = False

    @property
    def available(self) -> bool:
        return self._window is not None

    @property
    def hidden(self) -> bool:
        return self._hidden

    def create(self) -> bool:
        """Build the window. Returns False if this machine can't host one."""
        try:
            import webview
        except ImportError as exc:
            log.info("pywebview is not installed (%s); using the browser instead", exc)
            return False

        try:
            self._window = webview.create_window(
                TITLE,
                url=self._url,
                width=DEFAULT_SIZE[0],
                height=DEFAULT_SIZE[1],
                min_size=MIN_SIZE,
                background_color=BACKGROUND,
                # No native title bar — the page draws its own, so the chrome
                # matches the dashboard instead of sitting on top of it in a
                # different colour scheme.
                frameless=True,
                # Required for the frameless window to keep its drop shadow and,
                # with the style fix-up above, DWM's resize handling.
                shadow=True,
                resizable=True,
                # Off deliberately. easy_drag makes the *whole page* a drag
                # handle, which eats text selection and click-drag on controls.
                # Only .pywebview-drag-region moves the window instead.
                easy_drag=False,
                # Off: it puts a modal "are you sure" in front of a close that we
                # are going to reinterpret as "hide" anyway.
                confirm_close=False,
                text_select=True,
            )
        except Exception as exc:  # noqa: BLE001 — any failure means "no window"
            log.info("could not create a window (%s); using the browser instead", exc)
            self._window = None
            return False

        self._webview = webview
        self._window.events.closing += self._on_closing
        self._window.events.shown += self._on_shown
        self._window.events.maximized += self._on_maximized
        self._window.events.restored += self._on_restored
        return True

    def _on_maximized(self) -> None:
        self._maximized = True

    def _on_restored(self) -> None:
        self._maximized = False

    @property
    def maximized(self) -> bool:
        return self._maximized

    def minimize(self) -> None:
        if self._window is None:
            return
        with contextlib.suppress(Exception):
            self._window.minimize()

    def toggle_maximize(self) -> None:
        """What double-clicking a title bar does."""
        if self._window is None:
            return
        with contextlib.suppress(Exception):
            if self._maximized:
                self._window.restore()
                self._maximized = False
            else:
                self._window.maximize()
                self._maximized = True

    def _on_closing(self) -> bool:
        """Turn the close button into "hide". Returning False cancels the close.

        The hide is deferred onto a timer rather than done inline: this runs on
        the UI thread inside WinForms' own close handling, and hiding a form
        while it is deciding whether to close it is asking for trouble.
        """
        if self._quitting:
            return True
        threading.Timer(0.01, self.hide).start()
        log.info("window hidden — still watching, use the tray icon to bring it back")
        return False

    def _on_shown(self) -> None:
        self._hidden = False
        # Once only, and only after the window exists — there is nothing to
        # restyle before that, and re-running it on every un-hide is wasted work.
        if not self._styled:
            self._styled = _restore_resize_border(os.getpid())

    def run(self) -> None:
        """Run the native message loop. Blocks until the window is destroyed."""
        assert self._webview is not None
        self._running.set()
        # private_mode off with an explicit path: otherwise WebView2 builds a
        # throwaway profile on every launch, which is slow and loses any state
        # the page keeps between runs.
        storage = paths.ensure_data_dir() / "webview"
        icon = paths.icon_file()
        # Frameless means no title-bar icon, but alt-tab and the taskbar still
        # show one — and without this it is whatever icon python.exe carries.
        self._webview.start(
            private_mode=False,
            storage_path=str(storage),
            **({"icon": str(icon)} if icon else {}),
        )

    def show(self) -> None:
        if self._window is None:
            open_url(self._url, what="dashboard")
            return
        try:
            self._window.show()
            # A window hidden while minimised comes back minimised, which reads
            # as "the click did nothing".
            with contextlib.suppress(Exception):
                self._window.restore()
            self._hidden = False
        except Exception as exc:  # noqa: BLE001
            log.debug("could not show the window: %s", exc)

    def hide(self) -> None:
        if self._window is None:
            return
        try:
            self._window.hide()
            self._hidden = True
        except Exception as exc:  # noqa: BLE001
            log.debug("could not hide the window: %s", exc)

    def destroy(self) -> None:
        """Really close it, ending :meth:`run`. Safe before ``run`` starts."""
        self._quitting = True
        if self._window is None:
            return
        # Destroying before the loop is up leaves pywebview in a state where
        # start() then blocks on a window nobody can reach.
        self._running.wait(timeout=10.0)
        with contextlib.suppress(Exception):
            self._window.destroy()


def run(args: argparse.Namespace, *, host: str, port: int) -> int:
    """Run the desktop app. Must be called on the main thread.

    Returns a process exit code; blocks for the life of the application.
    """
    # Before any window exists: the shell reads this when the first one appears.
    _set_app_id()

    stopped = threading.Event()
    window: Window | None = None

    def on_runtime_stopped() -> None:
        """Runtime thread. Ends the native loop so the process can exit."""
        stopped.set()
        if window is not None:
            window.destroy()

    hooks = ShellHooks()
    runtime = Runtime(
        args, host=host, port=port, hooks=hooks, on_stopped=on_runtime_stopped,
    )

    # Bound late because the window does not exist yet, but the dashboard needs
    # the hooks at construction time. Every one tolerates window=None.
    hooks.show = lambda: window.show() if window else None
    hooks.hide = lambda: window.hide() if window else None
    hooks.minimize = lambda: window.minimize() if window else None
    hooks.maximize = lambda: window.toggle_maximize() if window else None
    hooks.quit = runtime.request_stop
    hooks.start_watching = runtime.start_watcher

    if not runtime.start():
        print(f"\n  dropwatch could not start: {runtime.error}\n")
        runtime.request_stop()
        runtime.join()
        return 1

    candidate = Window(runtime.url)
    if candidate.create():
        window = candidate
        hooks.windowed = True
        log.info("window ready at %s", runtime.url)
        try:
            candidate.run()
        except Exception:  # noqa: BLE001 — a GUI crash must still stop the watcher
            log.exception("the window loop failed")
        # Either the user chose Quit, or the window really was destroyed. Both
        # mean the app is over: the runtime is what keeps a hidden window alive.
        runtime.request_stop()
    else:
        # No window: the tray is the only control surface, so the browser gets
        # the dashboard and this thread simply waits for the runtime to end.
        open_url(runtime.url, what="dashboard")
        print(f"\n  dropwatch is running.  {runtime.url}")
        print("  No app window on this machine — using your browser instead.")
        print("  Quit from the tray icon.\n")
        stopped.wait()

    runtime.join()
    return 0
