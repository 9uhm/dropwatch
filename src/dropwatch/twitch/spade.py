"""Minute-watched telemetry — the mechanism that actually earns drop progress.

Twitch's web player reports viewing to a "spade" endpoint roughly once a minute.
Drop progress is credited server-side off those reports, so replicating them *is*
the watching. No video is fetched and nothing is decoded.

Two details are easy to get wrong:

* **The endpoint moves.** ``spade_url`` lives in a hashed settings bundle that
  changes on every web deploy, so it's resolved at runtime and cached, never
  hardcoded. There's a fallback constant, but it's a last resort.
* **A rejected POST is not an offline stream.** It means our telemetry stopped
  counting while the broadcast carries on, which is a different failure with a
  different fix. It surfaces as signal S4 (*degraded*), and the liveness detector
  deliberately gives it low weight.
"""

from __future__ import annotations

import base64
import json
import random
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp

from ..events import EventType
from ..log import get_logger

if TYPE_CHECKING:
    from ..config import WatchConfig
    from ..events import EventBus
    from .channels import StreamInfo

log = get_logger("twitch.spade")

TWITCH_HOME = "https://www.twitch.tv/"
#: Only used if the settings bundle can't be parsed at all.
FALLBACK_SPADE_URL = "https://spade.twitch.tv/track"

_SETTINGS_RE = re.compile(r"https://[\w./-]+/config/settings\.[0-9a-f]+\.js")
_SPADE_RE = re.compile(r'"spade_url"\s*:\s*"(https://[^"]+)"')

#: The resolved endpoint is cached this long before being looked up again.
URL_TTL = 3600.0


@dataclass(frozen=True, slots=True)
class TelemetryResult:
    accepted: bool
    status: int | None = None
    error: str | None = None

    def describe(self) -> str:
        if self.accepted:
            return f"accepted (HTTP {self.status})"
        if self.error:
            return f"rejected: {self.error}"
        return f"rejected (HTTP {self.status})"


class SpadeClient:
    """Resolves the telemetry endpoint and posts minute-watched events."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        watch: WatchConfig,
        bus: EventBus,
    ) -> None:
        self._session = session
        self._watch = watch
        self._bus = bus
        self._url: str | None = None
        self._resolved_at: float = 0.0
        self._consecutive_failures = 0
        self._scraped = False

    @property
    def url(self) -> str | None:
        return self._url

    @property
    def scraped(self) -> bool:
        """Whether the endpoint came from Twitch rather than the fallback constant.

        Tracked as a flag instead of inferred by comparing against
        :data:`FALLBACK_SPADE_URL`, because the value Twitch currently ships is
        identical to it — comparing values reports a successful scrape as a
        failure.
        """
        return self._scraped

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def degraded(self) -> bool:
        """True once telemetry has failed enough times to distrust it (signal S4)."""
        return self._consecutive_failures >= 2

    # ------------------------------------------------------------- endpoint

    async def resolve_url(self, *, force: bool = False) -> str:
        """Find the current spade endpoint, caching it for :data:`URL_TTL`."""
        fresh = self._url and (time.monotonic() - self._resolved_at) < URL_TTL
        if fresh and not force:
            return self._url  # type: ignore[return-value]

        url = await self._scrape_url()
        if url:
            if url != self._url:
                log.info("spade endpoint resolved to %s", url)
            self._url = url
            self._scraped = True
        elif not self._url:
            log.warning("could not resolve spade_url — falling back to %s", FALLBACK_SPADE_URL)
            self._url = FALLBACK_SPADE_URL
            self._scraped = False
        self._resolved_at = time.monotonic()
        return self._url

    async def _scrape_url(self) -> str | None:
        try:
            async with self._session.get(TWITCH_HOME) as resp:
                if resp.status != 200:
                    log.debug("twitch home returned HTTP %s", resp.status)
                    return None
                home = await resp.text()
        except (TimeoutError, aiohttp.ClientError) as exc:
            log.debug("could not fetch twitch home: %s", exc)
            return None

        # Occasionally the home page inlines it and we can skip a request.
        if direct := _SPADE_RE.search(home):
            return direct.group(1)

        match = _SETTINGS_RE.search(home)
        if not match:
            log.debug("no settings bundle URL in twitch home page")
            return None

        try:
            async with self._session.get(match.group(0)) as resp:
                if resp.status != 200:
                    log.debug("settings bundle returned HTTP %s", resp.status)
                    return None
                settings = await resp.text()
        except (TimeoutError, aiohttp.ClientError) as exc:
            log.debug("could not fetch settings bundle: %s", exc)
            return None

        found = _SPADE_RE.search(settings)
        return found.group(1) if found else None

    # ------------------------------------------------------------ reporting

    def next_interval(self) -> float:
        """Watch interval with jitter, so the bot isn't a perfect metronome."""
        jitter = self._watch.minute_watched_jitter
        base = self._watch.minute_watched_interval
        return max(1.0, base + random.uniform(-jitter, jitter))

    @staticmethod
    def build_payload(info: StreamInfo, user_id: str) -> str:
        """Base64'd minute-watched event, matching the web player's shape."""
        properties = {
            "channel_id": int(info.channel_id) if info.channel_id else None,
            "channel": info.login,
            "broadcast_id": int(info.stream_id) if info.stream_id else None,
            "user_id": int(user_id) if user_id else None,
            "player": "site",
            "playback": "live",
            "platform": "web",
            "player_state": "playing",
            "live": True,
            "hidden": False,
            "muted": False,
        }
        body = [{"event": "minute-watched", "properties": properties}]
        return base64.b64encode(json.dumps(body).encode("utf-8")).decode("ascii")

    async def send_minute_watched(self, info: StreamInfo, user_id: str) -> TelemetryResult:
        """Report one minute of viewing. Never raises — returns a result instead.

        The watch loop must survive a telemetry failure and let the liveness
        detector decide what it means, so transport errors come back as data.
        """
        if not info.channel_id or not info.stream_id:
            return TelemetryResult(
                accepted=False,
                error="missing channel_id or broadcast_id — cannot report watching",
            )

        url = await self.resolve_url()
        payload = self.build_payload(info, user_id)

        try:
            async with self._session.post(url, data={"data": payload}) as resp:
                await resp.read()
                status = resp.status
        except (TimeoutError, aiohttp.ClientError) as exc:
            return await self._record(TelemetryResult(accepted=False, error=str(exc)), info)

        if status == 204 or 200 <= status < 300:
            if self._consecutive_failures:
                log.info("telemetry recovered after %d failures", self._consecutive_failures)
            self._consecutive_failures = 0
            return TelemetryResult(accepted=True, status=status)

        # A 404 usually means the cached endpoint moved under us; re-resolve so
        # the next cycle has a chance of working rather than failing identically.
        if status in (404, 410):
            log.info("spade endpoint returned %s — re-resolving", status)
            await self.resolve_url(force=True)

        return await self._record(TelemetryResult(accepted=False, status=status), info)

    async def _record(self, result: TelemetryResult, info: StreamInfo) -> TelemetryResult:
        self._consecutive_failures += 1
        log.warning(
            "telemetry %s for %s (%d consecutive)",
            result.describe(), info.login, self._consecutive_failures,
        )
        if self._consecutive_failures == 2:
            # Announce once on crossing the threshold, not on every cycle.
            await self._bus.publish(
                EventType.TELEMETRY_DEGRADED,
                channel=info.login,
                status=result.status,
                error=result.error,
                consecutive=self._consecutive_failures,
            )
        return result

    def reset(self) -> None:
        """Clear failure state — called when the watcher rotates target."""
        self._consecutive_failures = 0
