"""Channel state: is it live, is it *creditable*, and what else could we watch.

The distinction that matters here is live-versus-eligible. A ``rerun`` (Twitch
calls it a vodcast) reports as live in every API response but earns no drop
progress, so treating "live" as "watchable" would leave the bot happily farming
nothing. :attr:`StreamInfo.drops_eligible` is the only thing callers should ask.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..log import get_logger
from .gql import GQLClient, GQLError, SchemaDriftError, pluck

if TYPE_CHECKING:
    from ..config import WatchConfig

log = get_logger("twitch.channels")


@dataclass(frozen=True, slots=True)
class StreamInfo:
    login: str
    channel_id: str | None = None
    display_name: str | None = None
    live: bool = False
    #: Twitch's stream id, which the telemetry payload calls ``broadcast_id``.
    stream_id: str | None = None
    #: ``"live"`` or ``"rerun"``; anything else is treated as ineligible.
    stream_type: str | None = None
    game: str | None = None
    viewers: int | None = None
    started_at: datetime | None = None

    @property
    def is_rerun(self) -> bool:
        return self.live and self.stream_type is not None and self.stream_type != "live"

    @property
    def drops_eligible(self) -> bool:
        """Live, and of a type Twitch actually credits drops for."""
        return self.live and self.stream_type == "live"

    @property
    def uptime_seconds(self) -> float | None:
        if not self.started_at:
            return None
        return (datetime.now(UTC) - self.started_at).total_seconds()

    def describe(self) -> str:
        if not self.live:
            return "offline"
        if self.is_rerun:
            return f"rerun ({self.stream_type}) — drops do not credit"
        bits = ["live"]
        if self.game:
            bits.append(self.game)
        if self.viewers is not None:
            bits.append(f"{self.viewers:,} viewers")
        return ", ".join(bits)


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _stream_info(login: str, user: Any) -> StreamInfo:
    """Build a :class:`StreamInfo` from a ``user`` node.

    A ``None`` user means no such channel; a ``None`` ``stream`` means offline.
    Both are ordinary answers, so neither raises — but a *present* stream missing
    its ``id`` is drift, and pluck will say so.
    """
    if user is None:
        return StreamInfo(login=login, live=False)

    stream = pluck(user, "stream", default=None)
    if stream is None:
        return StreamInfo(
            login=pluck(user, "login", default=login) or login,
            channel_id=pluck(user, "id", default=None),
            display_name=pluck(user, "displayName", default=None),
            live=False,
        )

    return StreamInfo(
        login=pluck(user, "login", default=login) or login,
        channel_id=pluck(user, "id", default=None),
        display_name=pluck(user, "displayName", default=None),
        live=True,
        stream_id=pluck(stream, "id"),
        stream_type=pluck(stream, "type", default="live"),
        game=pluck(stream, "game", "name", default=None),
        viewers=pluck(stream, "viewersCount", default=None),
        started_at=_parse_ts(pluck(stream, "createdAt", default=None)),
    )


class ChannelClient:
    """Live checks, watchability probes, and drops-enabled discovery."""

    def __init__(self, gql: GQLClient, watch: WatchConfig) -> None:
        self._gql = gql
        self._watch = watch

    async def fetch(self, login: str, *, detailed: bool = False) -> StreamInfo:
        """Current state of one channel.

        ``detailed`` uses ``StreamMetadata`` for viewer count and display name;
        the default ``UseLive`` is cheaper and unauthenticated, which is what the
        60-second liveness poll wants.
        """
        login = login.strip().lower()
        op = "StreamMetadata" if detailed else "UseLive"
        data = await self._gql.call(op, {"channelLogin": login})
        return _stream_info(login, pluck(data, "user", default=None))

    async def fetch_many(self, logins: list[str]) -> dict[str, StreamInfo]:
        """State of several channels, one request each, failures isolated.

        A single bad channel must not blank the whole priority list, so an error
        on one login degrades to "offline" for that login only.
        """
        import asyncio

        async def one(login: str) -> tuple[str, StreamInfo]:
            try:
                return login, await self.fetch(login)
            except (GQLError, TimeoutError) as exc:
                log.warning("live check failed for %s: %s", login, exc)
                return login, StreamInfo(login=login, live=False)

        results = await asyncio.gather(*(one(x.strip().lower()) for x in logins))
        return dict(results)

    async def playback_probe(self, login: str) -> bool:
        """Signal S3: can we actually obtain a playback token right now?

        This is the active confirmation the liveness detector escalates to. It's
        the strongest evidence available short of pulling the HLS manifest, and
        unlike the polls it answers on demand rather than on a cadence.
        """
        login = login.strip().lower()
        try:
            data = await self._gql.call(
                "PlaybackAccessToken",
                {
                    "isLive": True,
                    "login": login,
                    "isVod": False,
                    "vodID": "",
                    "playerType": "site",
                },
            )
        except SchemaDriftError:
            raise
        except GQLError as exc:
            log.debug("playback probe for %s failed: %s", login, exc)
            return False

        token = pluck(data, "streamPlaybackAccessToken", default=None)
        return bool(token and pluck(token, "value", default=None))

    async def discover(self, limit: int = 30) -> list[StreamInfo]:
        """Live channels in the configured game with drops enabled.

        Twitch's ``DROPS_ENABLED`` system filter does the eligibility work for us,
        which is far more reliable than inferring it from campaign membership.
        """
        data = await self._gql.call(
            "DirectoryPage_Game",
            {
                "limit": limit,
                "slug": self._watch.game_slug,
                "options": {
                    "includeRestricted": ["SUB_ONLY_LIVE"],
                    "sort": "RELEVANCE",
                    "tags": [],
                    "recommendationsContext": {"platform": "web"},
                    "freeformTags": None,
                    # Twitch's own filter for drops-enabled streams — far more
                    # reliable than inferring eligibility from campaign membership.
                    "systemFilters": ["DROPS_ENABLED"],
                },
                "cursor": None,
            },
        )

        edges = pluck(data, "game", "streams", "edges", default=None)
        if edges is None:
            raise SchemaDriftError(
                f"discovery returned no stream list for slug {self._watch.game_slug!r} "
                "— check watch.game_slug"
            )

        found: list[StreamInfo] = []
        for edge in edges:
            node = pluck(edge, "node", default=None)
            if node is None:
                continue
            broadcaster = pluck(node, "broadcaster", default=None) or {}
            login = pluck(broadcaster, "login", default=None)
            if not login:
                continue
            found.append(
                StreamInfo(
                    login=login,
                    channel_id=pluck(broadcaster, "id", default=None),
                    display_name=pluck(broadcaster, "displayName", default=None),
                    live=True,
                    stream_id=pluck(node, "id", default=None),
                    stream_type=pluck(node, "type", default="live"),
                    game=pluck(node, "game", "name", default=None),
                    viewers=pluck(node, "viewersCount", default=None),
                )
            )

        eligible = [s for s in found if s.drops_eligible]
        eligible.sort(key=lambda s: -(s.viewers or 0))
        log.debug(
            "discovery: %d streams, %d drops-eligible", len(found), len(eligible)
        )
        return eligible
