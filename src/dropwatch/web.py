"""Local web dashboard — live logs and state from the running watcher.

Served by the bot itself on localhost. Deliberately not the published artifact:
that page is hosted on claude.ai under a strict CSP and cannot reach a process on
your machine, so anything claiming to show live local state there would be
fiction. This is the real thing.

Transport is Server-Sent Events rather than a WebSocket. The stream is one-way and
text, reconnects on its own with no code from us, and survives the bot restarting
underneath it — all of which a WebSocket would make our problem instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from aiohttp import web

from . import __version__, paths
from .config import ConfigError
from .desktop import open_channel, open_url
from .events import Event, EventType
from .log import get_logger
from .twitch.auth import AuthError

if TYPE_CHECKING:
    from collections.abc import Callable

    from .config import ConfigManager
    from .events import EventBus
    from .store import Store
    from .twitch.auth import AuthManager, DeviceFlow
    from .twitch.drops import DropsClient
    from .watcher import Watcher

log = get_logger("web")

#: How many records to keep for clients that connect late.
BACKLOG = 500

#: How long the dashboard waits for someone to type the device code into Twitch.
#:
#: Far shorter than the 30 minutes Twitch keeps the code alive, because the code
#: is only useful while it is on screen and someone is acting on it. Changing your
#: mind is the common case, and the alternative is a stale code sitting in the way
#: for half an hour claiming something is in progress.
FLOW_TIMEOUT = 120.0

#: Pages the UI may ask us to open in the *real* browser. An allowlist, because
#: "open whatever URL the request names" turns a local server into a launcher for
#: anything a stray page can reach.
OPENABLE = {
    "inventory": "https://www.twitch.tv/drops/inventory",
    "connections": "https://www.twitch.tv/settings/connections",
}


@dataclass(slots=True)
class ShellHooks:
    """Callbacks into whatever desktop shell is hosting the dashboard.

    Empty by default: ``dropwatch serve`` has no window to show or hide, and the
    page hides those controls rather than offering buttons that do nothing.
    """

    #: Whether a native window exists at all — drives the UI, not the behaviour.
    windowed: bool = False
    show: Callable[[], Any] | None = None
    hide: Callable[[], Any] | None = None
    #: The page draws its own title bar when frameless, so the buttons a native
    #: frame would have provided have to come back through here.
    minimize: Callable[[], Any] | None = None
    maximize: Callable[[], Any] | None = None
    quit: Callable[[], Any] | None = None
    #: Called once auth succeeds from inside the UI, to start a watcher that
    #: could not have been started at launch. Receives the account name.
    start_watching: Callable[..., Any] | None = None


@dataclass(slots=True)
class Record:
    """One line in the feed: a log line, a bus event, or a state transition."""

    ts: float
    kind: str  # "log" | "event" | "transition"
    level: str = "info"
    label: str = ""
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    #: Which account produced this, "" when it is the app talking rather than a
    #: watcher. The combined log is unreadable without it once two accounts run.
    account: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self))


class _FeedHandler(logging.Handler):
    """Mirrors Python log records into the dashboard feed.

    Formatting stays with the standard handlers; this only captures. It never
    raises: a dashboard that breaks logging would be worse than no dashboard.
    """

    def __init__(self, dashboard: Dashboard) -> None:
        super().__init__(logging.INFO)
        self._dashboard = dashboard

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Watchers log as "dropwatch.watcher.<account>", which is the only
            # attribution a plain log line carries.
            label = record.name.removeprefix("dropwatch.")
            account = ""
            if label.startswith("watcher.") and label.count(".") == 1:
                label, account = "watcher", label.split(".", 1)[1]
            self._dashboard.push(Record(
                ts=record.created,
                kind="log",
                level=record.levelname.lower(),
                label=label,
                message=record.getMessage(),
                account=account,
            ))
        except Exception:  # noqa: BLE001 — logging must never break the app
            pass


class Dashboard:
    """Holds the feed, fans it out to connected browsers, and serves the page."""

    def __init__(
        self,
        *,
        watcher: Watcher,
        bus: EventBus,
        store: Store,
        drops: DropsClient,
        config: ConfigManager,
        auth: AuthManager | None = None,
        hooks: ShellHooks | None = None,
        app: Any = None,
        page: Any = None,
    ) -> None:
        #: The whole application when there is one, so the dashboard can report
        #: every account. ``watcher``/``auth`` stay for the single-watcher
        #: callers (tests, and `serve` before an App exists) and always describe
        #: the primary account.
        self._app = app
        self._watcher = watcher
        self._bus = bus
        self._store = store
        self._config = config
        self._drops_client = drops
        self._auth = auth
        self._hooks = hooks or ShellHooks()
        self._drops_cache: dict[str, Any] | None = None
        self._drops_at = 0.0
        self._flow: DeviceFlow | None = None
        self._flow_state = "idle"  # idle | pending | ok | error
        self._flow_error = ""
        #: Set while an *additional* account is being authorised: the empty
        #: Account the new tokens will land in.
        self._flow_account: Any = None
        self._flow_adding = False
        #: Monotonic instant at which we stop waiting. Drives the countdown the
        #: page shows, so the UI promises exactly as long as the server waits.
        self._flow_deadline = 0.0
        self._flow_task: asyncio.Task[None] | None = None
        # ui_file, not ROOT/ui: frozen builds carry the page inside the bundle.
        self._page_path = page or paths.ui_file("dashboard.html")
        self._feed: deque[Record] = deque(maxlen=BACKLOG)
        self._clients: set[asyncio.Queue[Record | None]] = set()
        self._handler: _FeedHandler | None = None
        self._runner: web.AppRunner | None = None
        self._started_at = time.time()

    # ------------------------------------------------------------------ feed

    def push(self, record: Record) -> None:
        self._feed.append(record)
        for queue in list(self._clients):
            # Drop for a client that isn't draining rather than stalling the bot.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(record)

    async def _on_event(self, event: Event) -> None:
        payload = {k: v for k, v in event.payload.items() if v is not None}

        account = str(payload.get("account") or "")

        if event.type is EventType.STATE_CHANGED:
            self.push(Record(
                ts=event.ts,
                kind="transition",
                level=_level_for(str(payload.get("to_state", ""))),
                label=f"{payload.get('from_state')} → {payload.get('to_state')}",
                message=str(payload.get("reason", "")),
                detail=payload,
                account=account,
            ))
            return

        self.push(Record(
            ts=event.ts,
            kind="event",
            level=_level_for_event(event.type),
            label=str(event.type),
            message=_summarise(payload),
            detail=payload,
            account=account,
        ))

    # ------------------------------------------------------------- lifecycle

    async def start(self, host: str = "127.0.0.1", port: int = 8787) -> str:
        self._bus.subscribe(self._on_event)

        self._handler = _FeedHandler(self)
        logging.getLogger("dropwatch").addHandler(self._handler)

        app = web.Application()
        app.add_routes([
            web.get("/", self._page),
            web.get("/api/ping", self._ping),
            web.get("/api/auth", self._auth_get),
            web.post("/api/auth/login", self._auth_login),
            web.post("/api/auth/logout", self._auth_logout),
            web.post("/api/auth/cancel", self._auth_cancel),
            web.get("/api/accounts", self._accounts_get),
            web.post("/api/accounts", self._accounts_set),
            web.get("/api/state", self._state),
            web.get("/api/history", self._history),
            web.get("/api/drops", self._drops),
            web.get("/api/stats", self._stats),
            web.get("/api/settings", self._settings_get),
            web.post("/api/settings", self._settings_set),
            web.post("/api/control", self._control),
            web.get("/api/events", self._events),
            web.get("/ui/{name}", self._asset),
        ])

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()

        url = f"http://{host}:{port}/"
        log.info("dashboard on %s", url)
        return url

    async def stop(self) -> None:
        if self._flow_task is not None and not self._flow_task.done():
            self._flow_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._flow_task
            self._flow_task = None
        if self._handler is not None:
            logging.getLogger("dropwatch").removeHandler(self._handler)
            self._handler = None
        for queue in list(self._clients):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ---------------------------------------------------------------- routes

    async def _page(self, _: web.Request) -> web.StreamResponse:
        try:
            html = self._page_path.read_text("utf-8")
        except OSError as exc:
            return web.Response(
                status=500,
                text=f"Could not read {self._page_path}: {exc}",
            )
        return web.Response(text=html, content_type="text/html")

    #: Served from ui/. An explicit allowlist rather than path joining — this is a
    #: local server, but "serve whatever the URL names from a directory" is how
    #: traversal bugs happen, and there are only ever a couple of files.
    ASSETS = {
        "stats.js": "application/javascript",
    }

    async def _asset(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        content_type = self.ASSETS.get(name)
        if content_type is None:
            raise web.HTTPNotFound
        try:
            body = paths.ui_file(name).read_text("utf-8")
        except OSError:
            raise web.HTTPNotFound from None
        return web.Response(text=body, content_type=content_type)

    async def _ping(self, _: web.Request) -> web.StreamResponse:
        """Identify this server to another copy of ourselves.

        The single-instance check needs to distinguish "dropwatch already owns
        this port" from "something unrelated is listening on 8787" — connecting
        successfully proves only the latter.
        """
        return web.json_response({
            "app": "dropwatch",
            "version": __version__,
            "pid": os.getpid(),
            "windowed": self._hooks.windowed,
        })

    # -------------------------------------------------------------- accounts

    def _accounts_payload(self) -> list[dict[str, Any]]:
        """Every account and what it is doing right now.

        Built from the live Account objects rather than the registry, so it
        describes what is actually running — a just-disabled account that has not
        been stopped yet should still look running, because it is.
        """
        if self._app is None:
            # Single-watcher callers (`serve`, tests) still get one entry, so the
            # page has exactly one code path to render.
            tokens = self._auth.tokens if self._auth else None
            return [_account_entry("", tokens, self._watcher.status(), True)]

        disabled = self._app.registry.disabled_names() if self._app.registry else set()
        return [
            _account_entry(
                account.name,
                account.auth.tokens if getattr(account, "auth", None) else None,
                account.watcher.status(),
                account.name not in disabled,
            )
            for account in self._app.accounts
        ]

    async def _accounts_get(self, _: web.Request) -> web.StreamResponse:
        return web.json_response({
            "accounts": self._accounts_payload(),
            "multi": self._app is not None,
        })

    async def _accounts_set(self, request: web.Request) -> web.StreamResponse:
        """Enable, disable or forget an account."""
        self._guard(request)
        if self._app is None:
            raise web.HTTPNotImplemented(reason="this server manages a single account")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(reason="body must be JSON") from None

        name = str(body.get("account") or "")
        action = body.get("action")
        account = self._app.get(name) if name else None

        if action in ("enable", "disable"):
            enable = action == "enable"
            await self._app.registry.set_enabled(name, enable)
            if enable and account is None:
                await self._app.add_account(name)
                if self._hooks.start_watching is not None:
                    await _maybe_await(lambda: self._hooks.start_watching(name))
            elif not enable and account is not None:
                # Stop watching before dropping it, or the session row stays open
                # and telemetry keeps flying until the process exits.
                await account.watcher.stop()
                await self._app.drop_account(name)
        elif action == "remove":
            if account is not None:
                await account.watcher.stop()
                await self._app.drop_account(name)
            await self._app.registry.remove(name)
        else:
            raise web.HTTPBadRequest(reason=f"unknown account action {action!r}")

        return web.json_response({"ok": True, "accounts": self._accounts_payload()})

    # ------------------------------------------------------------------ auth

    def _auth_payload(self) -> dict[str, Any]:
        auth = self._auth
        tokens = auth.tokens if auth is not None else None
        flow: dict[str, Any] | None = None
        if self._flow is not None:
            flow = {
                "user_code": self._flow.user_code,
                "verification_uri": self._flow.verification_uri,
                # Our deadline, not Twitch's: the code stops being watched when
                # we stop polling, so counting down to Twitch's expiry would
                # promise 28 minutes we are not going to wait.
                "expires_in": max(0.0, self._flow_deadline - time.monotonic()),
                "state": self._flow_state,
                "error": self._flow_error,
                "adding": self._flow_adding,
            }
        accounts = self._accounts_payload()
        # "Signed in" is a property of the fleet, not of the primary account: one
        # working account is enough for the dashboard to be worth showing.
        return {
            "supported": auth is not None or self._app is not None,
            "authenticated": any(a["authenticated"] for a in accounts),
            "login": tokens.login if tokens else None,
            "user_id": tokens.user_id if tokens else None,
            "never_expires": tokens.never_expires if tokens else False,
            # None, not a number: a TV-client token carries no expiry at all, and
            # seconds_remaining computes one from a zero timestamp — an enormous
            # negative that renders as "expired" in anything that trusts it.
            "expires_in": (
                None if not tokens or tokens.never_expires else tokens.seconds_remaining
            ),
            "watching": self._watcher.status().state != "IDLE" or bool(tokens),
            "accounts": accounts,
            "flow": flow,
        }

    async def _auth_get(self, _: web.Request) -> web.StreamResponse:
        return web.json_response(self._auth_payload())

    async def _auth_login(self, request: web.Request) -> web.StreamResponse:
        """Start the device flow, or hand back the one already in progress.

        Idempotent on purpose: the page polls this view, and someone who reloads
        mid-login must see the same code they already typed into Twitch, not a
        fresh one that invalidates the tab they left open.

        ``{"add": true}`` authorises an *additional* account. It gets its own
        unclaimed Account, which names itself from the login Twitch returns —
        without that, a second sign-in would overwrite the first one's tokens.
        """
        self._guard(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        adding = bool(body.get("add"))

        # One flow at a time: two codes on screen is two ways to get it wrong,
        # and the activation page takes one at a time regardless.
        if (
            self._flow_state == "pending"
            and self._flow is not None
            and self._flow.seconds_remaining > 0
        ):
            return web.json_response(self._auth_payload())

        if adding:
            if self._app is None:
                raise web.HTTPNotImplemented(
                    reason="this server manages a single account"
                )
            self._flow_account = await self._app.add_account("")
            auth = self._flow_account.auth
        else:
            self._flow_account = None
            auth = self._auth
            if auth is None:
                raise web.HTTPNotImplemented(reason="auth is not wired into this server")
            if auth.authenticated:
                return web.json_response(self._auth_payload())

        try:
            self._flow = await auth.start_device_flow()
        except (AuthError, TimeoutError, OSError) as exc:
            self._flow_state = "error"
            self._flow_error = str(exc)
            await self._discard_pending()
            return web.json_response(self._auth_payload(), status=502)

        self._flow_state = "pending"
        self._flow_adding = adding
        self._flow_error = ""
        self._flow_deadline = time.monotonic() + FLOW_TIMEOUT
        self._flow_task = asyncio.create_task(
            self._await_flow(self._flow, auth), name="device-flow"
        )
        # The code has to be typed into a real browser; a WebView2 window has no
        # Twitch session to authorise with.
        open_url(self._flow.verification_uri, what="twitch activation page")
        return web.json_response(self._auth_payload())

    async def _discard_pending(self) -> None:
        """Throw away the empty Account a failed "add" left behind."""
        pending, self._flow_account = self._flow_account, None
        if pending is None or self._app is None:
            return
        with contextlib.suppress(Exception):
            await pending.stop()
        with contextlib.suppress(ValueError):
            self._app.accounts.remove(pending)

    async def _await_flow(self, flow: DeviceFlow, auth: Any) -> None:
        pending = self._flow_account
        try:
            await asyncio.wait_for(
                auth.complete_device_flow(flow), timeout=FLOW_TIMEOUT
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # asyncio.TimeoutError *is* TimeoutError, so this catches two very
            # different things: our own deadline elapsing, and an HTTP call
            # inside the flow timing out. Reporting a network fault as "nobody
            # typed the code" would send someone looking in the wrong place, so
            # the clock decides which happened.
            if time.monotonic() >= self._flow_deadline:
                self._flow_state = "timeout"
                self._flow_error = ""
                log.info("device code expired unused after %.0fs", FLOW_TIMEOUT)
            else:
                self._flow_state = "error"
                self._flow_error = "timed out talking to Twitch"
                log.warning("login from the dashboard timed out against Twitch")
            await self._discard_pending()
            return
        except (AuthError, OSError) as exc:
            self._flow_state = "error"
            self._flow_error = str(exc)
            log.warning("login from the dashboard failed: %s", exc)
            await self._discard_pending()
            return

        # The token file is already written under the login's name by TokenStore;
        # this catches the in-memory objects up so events stop being attributed to
        # the empty string.
        name = ""
        tokens = auth.tokens
        if tokens and tokens.login:
            name = tokens.login.lower()
        if pending is not None and name:
            pending.claim(name)
            if self._app is not None and self._app.registry is not None:
                await self._app.registry.set_enabled(name, True)
        self._flow_account = None

        self._flow_state = "ok"
        self._flow_error = ""
        log.info("authorised %s from the dashboard", name or "an account")
        # Launch never got a watcher for this account because there was no token;
        # start one now rather than making the user restart the app.
        if self._hooks.start_watching is not None:
            await _maybe_await(lambda: self._hooks.start_watching(name))

    async def _auth_cancel(self, request: web.Request) -> web.StreamResponse:
        """Abandon the device flow in progress — the "changed my mind" button."""
        self._guard(request)
        task, self._flow_task = self._flow_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._discard_pending()
        self._flow = None
        self._flow_state = "idle"
        self._flow_error = ""
        self._flow_adding = False
        self._flow_deadline = 0.0
        return web.json_response(self._auth_payload())

    async def _auth_logout(self, request: web.Request) -> web.StreamResponse:
        self._guard(request)
        target = self._auth
        if self._app is not None:
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                body = {}
            named = self._app.get(str(body.get("account") or "") or None)
            if named is not None:
                target = named.auth
        if target is None:
            raise web.HTTPNotImplemented(reason="auth is not wired into this server")
        await target.logout(revoke=True)
        self._flow = None
        self._flow_state = "idle"
        self._flow_error = ""
        return web.json_response(self._auth_payload())

    async def _state(self, _: web.Request) -> web.StreamResponse:
        status = self._watcher.status()
        stats = status.stats
        return web.json_response({
            "version": __version__,
            "windowed": self._hooks.windowed,
            "authenticated": bool(self._auth and self._auth.authenticated),
            # The fleet. The flat fields below still describe the primary account
            # so nothing that predates multi-account support has to change.
            "accounts": self._accounts_payload(),
            "now": time.time(),
            "uptime": time.time() - self._started_at,
            "state": status.state,
            "channel": status.channel,
            "reason": status.reason,
            "signals": status.signals,
            "rotations": status.rotations,
            "paused": status.paused,
            "grace_remaining": status.grace_remaining,
            "pubsub_connected": status.pubsub_connected,
            "stats": {
                "cycles": stats.cycles,
                "minutes_sent": stats.minutes_sent,
                "telemetry_rejected": stats.telemetry_rejected,
                "credited_start": stats.credited_start,
                "credited_now": stats.credited_now,
                "required": stats.required,
                "credited_gain": stats.credited_gain,
                "uptime": stats.uptime,
            },
        })

    async def _history(self, _: web.Request) -> web.StreamResponse:
        transitions = await self._store.recent_transitions(30)
        sessions = await self._store.recent_sessions(15)
        return web.json_response({
            "transitions": [
                {
                    "ts": t.ts, "channel": t.channel, "from": t.from_state,
                    "to": t.to_state, "reason": t.reason, "signals": t.signals,
                }
                for t in transitions
            ],
            "sessions": [dict(s) for s in sessions],
        })

    #: Keys the dashboard may write, with enough metadata to render a control.
    #:
    #: An allowlist rather than "anything ConfigManager accepts": the server has no
    #: authentication, so the blast radius of the write endpoint should be the
    #: settings a person would actually toggle. ``ui.host``/``ui.port`` are
    #: deliberately excluded — changing them needs a restart to take effect, and a
    #: bad value entered from the dashboard would make the dashboard unreachable.
    SETTINGS: tuple[dict[str, Any], ...] = (
        {"key": "ui.open_dashboard", "type": "bool", "group": "Startup",
         "label": "Open the dashboard when watching starts"},
        {"key": "ui.open_twitch", "type": "bool", "group": "Startup",
         "label": "Open the channel on Twitch when a target is picked"},
        {"key": "ui.reopen_twitch_on_rotate", "type": "bool", "group": "Startup",
         "label": "Re-open Twitch on every rotation",
         "hint": "Off by default — an overnight run would bury you in tabs"},
        {"key": "ui.tray", "type": "bool", "group": "Startup",
         "label": "Show a system-tray icon", "hint": "Restart to apply"},

        {"key": "watch.auto_discovery", "type": "bool", "group": "Watching",
         "label": "Fall back to auto-discovery when no listed channel is live"},
        {"key": "watch.auto_claim", "type": "bool", "group": "Watching",
         "label": "Attempt to claim earned rewards",
         "hint": "Twitch blocks this; earned rewards are still announced"},
        {"key": "watch.minute_watched_interval", "type": "number", "group": "Watching",
         "label": "Telemetry interval", "unit": "s", "min": 10, "max": 300, "step": 1},
        {"key": "watch.progress_check_every", "type": "number", "group": "Watching",
         "label": "Read progress every N cycles", "min": 1, "max": 60, "step": 1},
        {"key": "watch.idle_poll_interval", "type": "number", "group": "Watching",
         "label": "Idle re-check interval", "unit": "s", "min": 30, "max": 3600, "step": 30},

        {"key": "liveness.grace_period", "type": "number", "group": "Stream-end detection",
         "label": "Grace period", "unit": "s", "min": 0, "max": 600, "step": 10,
         "hint": "How long a suspected offline stream is held before rotating"},
        {"key": "liveness.confirm_reads", "type": "number", "group": "Stream-end detection",
         "label": "Confirmations required", "min": 1, "max": 10, "step": 1},
        {"key": "liveness.stall_cycles", "type": "number", "group": "Stream-end detection",
         "label": "Zero-progress checks before STALLED", "min": 1, "max": 20, "step": 1},
        {"key": "liveness.stream_poll_interval", "type": "number",
         "group": "Stream-end detection",
         "label": "Stream poll interval", "unit": "s", "min": 10, "max": 600, "step": 10},

        {"key": "logging.level", "type": "choice", "group": "Diagnostics",
         "label": "Log level", "choices": ["DEBUG", "INFO", "WARNING", "ERROR"]},
    )

    def _guard(self, request: web.Request) -> None:
        """Reject cross-site writes.

        The dashboard is unauthenticated on localhost, which means any page in the
        browser could otherwise POST to it. Requiring a custom header forces a CORS
        preflight that we never answer, so a cross-origin write can't get through —
        while same-origin fetch from our own page passes trivially.
        """
        if request.headers.get("X-Dropwatch") != "1":
            raise web.HTTPForbidden(reason="missing X-Dropwatch header")
        origin = request.headers.get("Origin")
        if origin and request.host and request.host not in origin:
            raise web.HTTPForbidden(reason="cross-origin write rejected")

    async def _settings_get(self, _: web.Request) -> web.StreamResponse:
        flat = self._config.flat()
        overrides = self._config.overrides
        return web.json_response({
            "settings": [
                {**spec, "value": flat.get(spec["key"]),
                 "overridden": spec["key"] in overrides}
                for spec in self.SETTINGS
            ],
            "groups": list(dict.fromkeys(s["group"] for s in self.SETTINGS)),
        })

    async def _settings_set(self, request: web.Request) -> web.StreamResponse:
        self._guard(request)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(reason="body must be JSON") from None

        key = payload.get("key")
        allowed = {s["key"] for s in self.SETTINGS}
        if key not in allowed:
            raise web.HTTPForbidden(reason=f"{key!r} is not editable from the dashboard")

        try:
            if payload.get("reset"):
                await self._config.clear_override(key)
            else:
                await self._config.set_override(key, payload.get("value"))
        except ConfigError as exc:
            # A rejected value is the user's mistake, not a server fault — report it
            # so the page can show the reason inline.
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

        flat = self._config.flat()
        log.info("setting changed from dashboard: %s = %r", key, flat.get(key))
        return web.json_response({
            "ok": True, "key": key, "value": flat.get(key),
            "overridden": key in self._config.overrides,
        })

    async def _control(self, request: web.Request) -> web.StreamResponse:
        """Pause, resume, open a page, or drive the window — the tray menu over HTTP.

        ``show`` is also how a second launch of the exe reaches the copy that is
        already running: it has no other channel to the first process, and the
        server is by definition alive if it answered at all.
        """
        self._guard(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(reason="body must be JSON") from None
        action = body.get("action")

        # A named account acts on that one; unnamed means "all of them", which is
        # what the global Pause button in the header means with a fleet running.
        named = str(body.get("account") or "")
        targets = self._control_targets(named)

        if action == "pause":
            for watcher in targets:
                await watcher.pause()
        elif action == "resume":
            for watcher in targets:
                await watcher.resume()
        elif action == "open_twitch":
            channel = next((w.status().channel for w in targets if w.status().channel), None)
            if not channel:
                return web.json_response({"ok": False, "error": "no current target"})
            open_channel(channel)
        elif action == "open":
            url = OPENABLE.get(str(body.get("what")))
            if url is None:
                raise web.HTTPBadRequest(reason=f"cannot open {body.get('what')!r}")
            open_url(url, what=str(body.get("what")))
        elif action in ("show", "hide", "minimize", "maximize"):
            hook = {
                "show": self._hooks.show, "hide": self._hooks.hide,
                "minimize": self._hooks.minimize, "maximize": self._hooks.maximize,
            }[action]
            if hook is None:
                return web.json_response({"ok": False, "error": "no window to " + action})
            await _maybe_await(hook)
        elif action == "quit":
            # Answer first: tearing the server down inside the request would
            # leave the caller with a connection reset instead of a result.
            asyncio.get_running_loop().call_later(0.15, self._quit_soon)
        else:
            raise web.HTTPBadRequest(reason=f"unknown action {action!r}")

        return web.json_response({"ok": True, "state": self._watcher.status().state})

    def _control_targets(self, account: str) -> list[Any]:
        """The watchers a control action applies to."""
        if self._app is None:
            return [self._watcher]
        if account:
            found = self._app.get(account)
            return [found.watcher] if found is not None else []
        return [a.watcher for a in self._app.accounts]

    def _quit_soon(self) -> None:
        async def go() -> None:
            for watcher in self._control_targets(""):
                await watcher.stop()
            if self._hooks.quit is not None:
                await _maybe_await(self._hooks.quit)

        asyncio.get_running_loop().create_task(go(), name="quit")  # noqa: RUF006

    async def _stats(self, request: web.Request) -> web.StreamResponse:
        """Series and aggregates for the charts."""
        try:
            hours = float(request.query.get("hours", "24"))
        except ValueError:
            hours = 24.0
        hours = min(max(hours, 0.25), 24 * 30)

        samples = await self._store.samples(since=time.time() - hours * 3600)
        sessions = await self._store.recent_sessions(200)
        completed = [s for s in sessions if s.get("ended_at")]

        durations = [
            s["ended_at"] - s["started_at"] for s in completed
            if s["ended_at"] and s["started_at"]
        ]
        total_minutes = sum(int(s.get("minutes") or 0) for s in completed)

        return web.json_response({
            "window_hours": hours,
            "series": [
                {
                    "ts": s["ts"],
                    "credited": s.get("credited"),
                    "sent": s.get("minutes_sent"),
                    "state": s.get("state"),
                    "channel": s.get("channel"),
                }
                for s in samples
            ],
            "daily": await self._store.daily_totals(14),
            "channels": await self._store.channel_totals(30),
            "transitions": await self._store.transition_counts(30),
            "totals": {
                "sessions": len(completed),
                "minutes_reported": total_minutes,
                "hours_reported": round(total_minutes / 60, 2),
                "avg_session_minutes": (
                    round(sum(durations) / len(durations) / 60, 1) if durations else 0
                ),
                "longest_session_minutes": (
                    round(max(durations) / 60, 1) if durations else 0
                ),
            },
        })

    async def _drops(self, _: web.Request) -> web.StreamResponse:
        """Every active campaign, its drops, and what's claimable.

        Cached briefly: the page polls this and the underlying call is one of the
        heavier ones, but drop progress only moves once a minute anyway.
        """
        now = time.monotonic()
        if self._drops_cache is not None and now - self._drops_at < 20.0:
            return web.json_response(self._drops_cache)

        try:
            campaigns = await self._drops_client.inventory()
        except Exception as exc:  # noqa: BLE001 — the page must degrade, not 500
            log.debug("inventory read failed: %s", exc)
            return web.json_response(
                {"error": f"{type(exc).__name__}: {exc}", "campaigns": []},
                status=200,
            )

        linked, link_url = self._drops_client.link_status(campaigns)
        payload = {
            "fetched_at": time.time(),
            "claimable_total": sum(len(c.claimable) for c in campaigns),
            # None means "cannot tell yet", and the page must render that as
            # silence rather than as a warning.
            "account_linked": linked,
            "account_link_url": link_url,
            "campaigns": [
                {
                    "id": c.id,
                    "name": c.name,
                    "status": c.status,
                    "game": c.game,
                    "ends_at": c.ends_at.timestamp() if c.ends_at else None,
                    "hours_left": c.hours_left,
                    "expiring_soon": c.expiring_soon,
                    "claimable_count": len(c.claimable),
                    "account_connected": c.account_connected,
                    "drops": [
                        {
                            "id": d.id,
                            "name": d.name,
                            "reward": d.reward_name,
                            "image": d.image_url,
                            "required": d.required_minutes,
                            "current": d.current_minutes,
                            "remaining": d.remaining_minutes,
                            "fraction": d.fraction,
                            "claimed": d.claimed,
                            "complete": d.complete,
                            "claimable": d.claimable,
                        }
                        for d in c.drops
                    ],
                }
                for c in campaigns
            ],
        }
        self._drops_cache = payload
        self._drops_at = now
        return web.json_response(payload)

    async def _events(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        queue: asyncio.Queue[Record | None] = asyncio.Queue(maxsize=1000)
        self._clients.add(queue)
        try:
            # Replay the backlog so a browser opened late still sees the run.
            for record in list(self._feed):
                await response.write(_sse(record.to_json()))

            while True:
                try:
                    record = await asyncio.wait_for(queue.get(), timeout=20.0)
                except TimeoutError:
                    # Comment frame keeps proxies and the browser from timing out.
                    await response.write(b": keepalive\n\n")
                    continue
                if record is None:
                    break
                await response.write(_sse(record.to_json()))
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._clients.discard(queue)
        return response


def _account_entry(
    name: str, tokens: Any, status: Any, enabled: bool
) -> dict[str, Any]:
    """One account as the dashboard renders it: identity plus live state."""
    stats = status.stats
    return {
        "name": name,
        "login": tokens.login if tokens else None,
        "user_id": tokens.user_id if tokens else None,
        "authenticated": bool(tokens and tokens.user_id),
        "enabled": enabled,
        "state": "PAUSED" if status.paused else status.state,
        "channel": status.channel,
        "reason": status.reason,
        "paused": status.paused,
        "rotations": status.rotations,
        "pubsub_connected": status.pubsub_connected,
        "signals": status.signals,
        "stats": {
            "cycles": stats.cycles,
            "minutes_sent": stats.minutes_sent,
            "credited_now": stats.credited_now,
            "credited_gain": stats.credited_gain,
            "required": stats.required,
            "uptime": stats.uptime,
        },
    }


def _sse(data: str) -> bytes:
    return f"data: {data}\n\n".encode()


async def _maybe_await(func: Callable[[], Any]) -> Any:
    """Call a hook that may be sync or async, without the caller caring which."""
    result = func()
    if inspect.isawaitable(result):
        return await result
    return result


def _level_for(state: str) -> str:
    return {
        "OFFLINE": "error",
        "STALLED": "warning",
        "SUSPECT": "warning",
        "WATCHING": "ok",
        "PAUSED": "info",
        "IDLE": "info",
    }.get(state, "info")


def _level_for_event(kind: EventType) -> str:
    if kind in (EventType.SCHEMA_DRIFT, EventType.ERROR, EventType.AUTH_EXPIRED):
        return "error"
    if kind in (
        EventType.TELEMETRY_DEGRADED, EventType.STREAM_STALLED,
        EventType.NO_TARGETS, EventType.CAMPAIGN_EXPIRING, EventType.CLAIM_BLOCKED,
    ):
        return "warning"
    if kind in (
        EventType.DROP_CLAIMED, EventType.DROP_COMPLETED, EventType.WATCH_STARTED,
        EventType.DROP_CLAIMABLE,
    ):
        return "ok"
    return "info"


def _summarise(payload: dict[str, Any]) -> str:
    # "account" is rendered as a chip by the page, not repeated in the text.
    skip = {"signals", "account"}
    return " ".join(f"{k}={v}" for k, v in payload.items() if k not in skip)
