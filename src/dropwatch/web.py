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
import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from aiohttp import web

from . import __version__, paths
from .config import ConfigError
from .desktop import open_channel
from .events import Event, EventType
from .log import get_logger

if TYPE_CHECKING:
    from .config import ConfigManager
    from .events import EventBus
    from .store import Store
    from .twitch.drops import DropsClient
    from .watcher import Watcher

log = get_logger("web")

#: How many records to keep for clients that connect late.
BACKLOG = 500


@dataclass(slots=True)
class Record:
    """One line in the feed: a log line, a bus event, or a state transition."""

    ts: float
    kind: str  # "log" | "event" | "transition"
    level: str = "info"
    label: str = ""
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

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
            self._dashboard.push(Record(
                ts=record.created,
                kind="log",
                level=record.levelname.lower(),
                label=record.name.removeprefix("dropwatch."),
                message=record.getMessage(),
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
        page: Any = None,
    ) -> None:
        self._watcher = watcher
        self._bus = bus
        self._store = store
        self._config = config
        self._drops_client = drops
        self._drops_cache: dict[str, Any] | None = None
        self._drops_at = 0.0
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

        if event.type is EventType.STATE_CHANGED:
            self.push(Record(
                ts=event.ts,
                kind="transition",
                level=_level_for(str(payload.get("to_state", ""))),
                label=f"{payload.get('from_state')} → {payload.get('to_state')}",
                message=str(payload.get("reason", "")),
                detail=payload,
            ))
            return

        self.push(Record(
            ts=event.ts,
            kind="event",
            level=_level_for_event(event.type),
            label=str(event.type),
            message=_summarise(payload),
            detail=payload,
        ))

    # ------------------------------------------------------------- lifecycle

    async def start(self, host: str = "127.0.0.1", port: int = 8787) -> str:
        self._bus.subscribe(self._on_event)

        self._handler = _FeedHandler(self)
        logging.getLogger("dropwatch").addHandler(self._handler)

        app = web.Application()
        app.add_routes([
            web.get("/", self._page),
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

    async def _state(self, _: web.Request) -> web.StreamResponse:
        status = self._watcher.status()
        stats = status.stats
        return web.json_response({
            "version": __version__,
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
        """Pause, resume, rotate, or open a window — the tray menu over HTTP."""
        self._guard(request)
        try:
            action = (await request.json()).get("action")
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(reason="body must be JSON") from None

        if action == "pause":
            await self._watcher.pause()
        elif action == "resume":
            await self._watcher.resume()
        elif action == "open_twitch":
            channel = self._watcher.status().channel
            if not channel:
                return web.json_response({"ok": False, "error": "no current target"})
            open_channel(channel)
        else:
            raise web.HTTPBadRequest(reason=f"unknown action {action!r}")

        return web.json_response({"ok": True, "state": self._watcher.status().state})

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

        payload = {
            "fetched_at": time.time(),
            "claimable_total": sum(len(c.claimable) for c in campaigns),
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


def _sse(data: str) -> bytes:
    return f"data: {data}\n\n".encode()


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
    parts = [f"{k}={v}" for k, v in payload.items() if k != "signals"]
    return " ".join(parts)
