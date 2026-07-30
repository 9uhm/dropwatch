"""Twitch PubSub WebSocket — signal S1.

Subscribes to ``video-playback-by-id.<channel_id>`` and turns ``stream-up`` /
``stream-down`` / ``commercial`` frames into liveness observations. This is the
only signal that arrives in seconds rather than on a poll cadence.

The rule that matters most here is what happens when the socket dies: **a dead
socket reports UNKNOWN, never OFFLINE.** Losing our connection says nothing about
whether the broadcast is still running, and treating it as an ending would rotate
the bot off a perfectly good stream every time the network hiccups. The polling
signals decide in that case.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp

from ..log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger("twitch.pubsub")

PUBSUB_URL = "wss://pubsub-edge.twitch.tv/v1"

#: Twitch drops the socket at 5 minutes of silence; stay comfortably inside that.
PING_INTERVAL = 240.0
PONG_TIMEOUT = 15.0


@dataclass(slots=True)
class PlaybackState:
    """What PubSub last told us about one channel."""

    channel_id: str
    #: ``True`` for stream-up, ``False`` for stream-down, ``None`` for never heard.
    online: bool | None = None
    #: Monotonic timestamp of the last message about this channel.
    updated_at: float | None = None
    #: Set while a ``commercial`` is running — an ad break is not a stream ending.
    in_commercial_until: float = 0.0
    viewers: int | None = None

    @property
    def in_commercial(self) -> bool:
        return time.monotonic() < self.in_commercial_until

    @property
    def age(self) -> float | None:
        if self.updated_at is None:
            return None
        return time.monotonic() - self.updated_at


@dataclass(slots=True)
class _Topic:
    channel_id: str
    nonce: str
    confirmed: bool = False


class PubSubClient:
    """Maintains one WebSocket, resubscribing on reconnect.

    Runs as a background task. Callers read :meth:`state` rather than being
    pushed to, so a socket that's down simply yields stale-or-absent data that
    the liveness detector can weigh, instead of a callback that never fires.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        on_event: Callable[[str, str], None] | None = None,
    ) -> None:
        self._session = session
        self._on_event = on_event
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task[None] | None = None
        self._topics: dict[str, _Topic] = {}
        self._states: dict[str, PlaybackState] = {}
        self._connected = False
        self._connected_at: float | None = None
        self._failures = 0
        self._stopping = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def uptime(self) -> float | None:
        if not self._connected or self._connected_at is None:
            return None
        return time.monotonic() - self._connected_at

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="pubsub")

    async def stop(self) -> None:
        self._stopping = True
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._connected = False

    # -------------------------------------------------------------- topics

    def state(self, channel_id: str) -> PlaybackState | None:
        return self._states.get(channel_id)

    async def watch_channel(self, channel_id: str) -> None:
        """Subscribe to one channel, replacing any previous subscription."""
        async with self._lock:
            if channel_id in self._topics:
                return
            nonce = f"n{random.randrange(1 << 60):x}"
            self._topics[channel_id] = _Topic(channel_id=channel_id, nonce=nonce)
            self._states.setdefault(channel_id, PlaybackState(channel_id=channel_id))
            if self._ws is not None and not self._ws.closed:
                await self._send_listen(channel_id, nonce)

    async def unwatch_all(self) -> None:
        async with self._lock:
            self._topics.clear()
            self._states.clear()

    async def _send_listen(self, channel_id: str, nonce: str) -> None:
        assert self._ws is not None
        await self._ws.send_json({
            "type": "LISTEN",
            "nonce": nonce,
            "data": {"topics": [f"video-playback-by-id.{channel_id}"]},
        })

    # ----------------------------------------------------------------- loop

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._session_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — must never kill the task
                log.debug("pubsub connection error: %s", exc)

            self._connected = False
            self._connected_at = None
            if self._stopping:
                break

            self._failures += 1
            delay = min(60.0, 1.5 * (2 ** min(self._failures, 5)))
            delay *= 0.5 + random.random()
            log.info("pubsub reconnecting in %.0fs (failure %d)", delay, self._failures)
            await asyncio.sleep(delay)

    async def _session_once(self) -> None:
        async with self._session.ws_connect(PUBSUB_URL, heartbeat=None) as ws:
            self._ws = ws
            self._connected = True
            self._connected_at = time.monotonic()
            self._failures = 0
            log.info("pubsub connected")

            async with self._lock:
                for topic in self._topics.values():
                    topic.confirmed = False
                    await self._send_listen(topic.channel_id, topic.nonce)

            pinger = asyncio.create_task(self._ping_loop(ws), name="pubsub-ping")
            try:
                async for message in ws:
                    if message.type is aiohttp.WSMsgType.TEXT:
                        self._handle(message.data)
                    elif message.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
            finally:
                pinger.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pinger
                self._ws = None
                self._connected = False
        log.info("pubsub disconnected")

    async def _ping_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while not ws.closed:
            # Jittered, because Twitch's docs ask for it explicitly.
            await asyncio.sleep(PING_INTERVAL * (0.9 + random.random() * 0.1))
            if ws.closed:
                return
            with contextlib.suppress(Exception):
                await ws.send_json({"type": "PING"})

    # -------------------------------------------------------------- messages

    def _handle(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("pubsub sent non-JSON frame")
            return

        kind = frame.get("type")

        if kind == "RESPONSE":
            nonce = frame.get("nonce")
            error = frame.get("error") or ""
            for topic in self._topics.values():
                if topic.nonce == nonce:
                    topic.confirmed = not error
                    if error:
                        log.warning("pubsub refused topic for %s: %s", topic.channel_id, error)
                    return
            return

        if kind == "RECONNECT":
            # Twitch is asking us to move; closing triggers the reconnect loop.
            log.info("pubsub asked us to reconnect")
            if self._ws is not None:
                asyncio.create_task(self._ws.close())  # noqa: RUF006
            return

        if kind != "MESSAGE":
            return  # PONG and anything else we don't act on

        data = frame.get("data") or {}
        topic = str(data.get("topic", ""))
        if not topic.startswith("video-playback-by-id."):
            return
        channel_id = topic.split(".", 1)[1]

        payload = data.get("message")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return
        if not isinstance(payload, dict):
            return

        self._apply(channel_id, payload)

    def _apply(self, channel_id: str, payload: dict[str, Any]) -> None:
        state = self._states.setdefault(channel_id, PlaybackState(channel_id=channel_id))
        kind = payload.get("type")
        state.updated_at = time.monotonic()

        if kind == "stream-up":
            state.online = True
            state.in_commercial_until = 0.0
        elif kind == "stream-down":
            state.online = False
        elif kind == "viewcount":
            state.viewers = payload.get("viewers")
            # A viewcount frame is itself proof the stream is up.
            if state.online is None:
                state.online = True
        elif kind == "commercial":
            length = payload.get("length")
            duration = float(length) if isinstance(length, (int, float)) else 180.0
            state.in_commercial_until = time.monotonic() + duration
            log.debug("commercial on %s for %.0fs", channel_id, duration)
        else:
            return

        if self._on_event is not None and kind in ("stream-up", "stream-down", "commercial"):
            with contextlib.suppress(Exception):
                self._on_event(channel_id, str(kind))
