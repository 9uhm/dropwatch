"""Internal async event bus.

Every component talks through this rather than holding references to the Discord
bot, so the watcher stays testable and Discord stays an optional consumer. A
handler that raises is logged and dropped — a broken notifier must never stop the
watch loop.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .log import get_logger

log = get_logger("events")


class EventType(StrEnum):
    # Auth
    AUTH_NEEDED = "auth.needed"
    AUTH_REFRESHED = "auth.refreshed"
    AUTH_EXPIRED = "auth.expired"

    # Watching
    WATCH_STARTED = "watch.started"
    WATCH_STOPPED = "watch.stopped"
    TARGET_SWITCHED = "target.switched"
    NO_TARGETS = "target.none"

    # Liveness state machine
    STATE_CHANGED = "state.changed"
    STREAM_ENDED = "stream.ended"
    STREAM_STALLED = "stream.stalled"

    # Drops
    DROP_PROGRESS = "drop.progress"
    DROP_COMPLETED = "drop.completed"
    #: Earned and waiting to be collected. Distinct from DROP_CLAIMED because
    #: Twitch gates claiming behind an integrity token, so the bot can earn a
    #: reward it cannot collect — and that gap must be visible, not silent.
    DROP_CLAIMABLE = "drop.claimable"
    DROP_CLAIMED = "drop.claimed"
    CLAIM_BLOCKED = "drop.claim_blocked"
    CAMPAIGN_EXPIRING = "campaign.expiring"

    # Health
    TELEMETRY_DEGRADED = "health.telemetry_degraded"
    SCHEMA_DRIFT = "health.schema_drift"
    ERROR = "health.error"


@dataclass(frozen=True, slots=True)
class Event:
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


Handler = Callable[[Event], Awaitable[None] | None]


class EventBus:
    def __init__(self, history_size: int = 200) -> None:
        self._handlers: dict[EventType | None, list[Handler]] = {}
        self._history: deque[Event] = deque(maxlen=history_size)

    def subscribe(self, handler: Handler, *types: EventType) -> Callable[[], None]:
        """Subscribe to specific event types, or to all types if none are given.

        Returns a callable that unsubscribes.
        """
        keys: Iterable[EventType | None] = types or (None,)
        for key in keys:
            self._handlers.setdefault(key, []).append(handler)

        def unsubscribe() -> None:
            for k in keys:
                bucket = self._handlers.get(k)
                if bucket and handler in bucket:
                    bucket.remove(handler)

        return unsubscribe

    async def publish(self, type_: EventType, /, **payload: Any) -> Event:
        event = Event(type=type_, payload=payload)
        self._history.append(event)
        log.debug("event %s %s", event.type, event.payload)

        handlers = [*self._handlers.get(type_, []), *self._handlers.get(None, [])]
        if not handlers:
            return event

        results = await asyncio.gather(
            *(self._invoke(h, event) for h in handlers), return_exceptions=True
        )
        for handler, result in zip(handlers, results, strict=True):
            if isinstance(result, Exception):
                log.exception(
                    "handler %s failed on %s", getattr(handler, "__qualname__", handler),
                    event.type, exc_info=result,
                )
        return event

    @staticmethod
    async def _invoke(handler: Handler, event: Event) -> None:
        result = handler(event)
        if inspect.isawaitable(result):
            await result

    def history(self, limit: int | None = None, *types: EventType) -> list[Event]:
        items = list(self._history)
        if types:
            wanted = set(types)
            items = [e for e in items if e.type in wanted]
        items.reverse()  # newest first
        return items[:limit] if limit else items
