"""Drop progress and inventory.

Two different views, and conflating them causes real confusion:

* :meth:`DropsClient.current_session` is what Twitch is crediting *right now*. It
  reports a single ``dropID`` and nothing else — no name, no campaign. It's the
  proof the telemetry loop works, and nothing more.
* :meth:`DropsClient.inventory` is the authoritative picture: every active
  campaign, every drop in it, minutes watched, and whether it's been claimed.

The session view is deliberately narrow and easy to misread — the drop it names is
whichever one Twitch picked, not necessarily the campaign you had in mind, and
several campaigns credit from the same watch time simultaneously. Anything shown to
a person should come from the inventory.

Claiming itself is still phase 4 and deliberately unimplemented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..log import get_logger
from .gql import SchemaDriftError, pluck

if TYPE_CHECKING:
    from .gql import GQLClient

log = get_logger("twitch.drops")


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Benefit:
    """The actual thing you receive — a skin, spray, icon, or loot box."""

    id: str
    name: str
    image_url: str | None = None


@dataclass(frozen=True, slots=True)
class Drop:
    """One time-based reward tier within a campaign."""

    id: str
    name: str
    required_minutes: int
    current_minutes: int
    claimed: bool
    benefits: list[Benefit] = field(default_factory=list)
    ends_at: datetime | None = None
    #: Twitch's handle for claiming. ``None`` until the drop is actually earned,
    #: so its absence on an unearned drop is expected, not an error.
    instance_id: str | None = None

    @property
    def complete(self) -> bool:
        return self.current_minutes >= self.required_minutes > 0

    @property
    def claimable(self) -> bool:
        """Earned but not yet collected — the thing worth surfacing loudly."""
        return self.complete and not self.claimed

    @property
    def remaining_minutes(self) -> int:
        return max(0, self.required_minutes - self.current_minutes)

    @property
    def fraction(self) -> float:
        if self.required_minutes <= 0:
            return 0.0
        return min(1.0, self.current_minutes / self.required_minutes)

    @property
    def reward_name(self) -> str:
        return self.benefits[0].name if self.benefits else self.name

    @property
    def image_url(self) -> str | None:
        return self.benefits[0].image_url if self.benefits else None


@dataclass(frozen=True, slots=True)
class Campaign:
    id: str
    name: str
    status: str
    game: str | None = None
    game_slug: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    drops: list[Drop] = field(default_factory=list)

    #: Whether the external account this campaign requires (Battle.net, for
    #: Overwatch) is linked to this Twitch account. ``None`` means Twitch did not
    #: say -- an older hash, or a campaign with no connection requirement -- and
    #: is deliberately distinct from ``False``, because "we don't know" must not
    #: be reported as "you are not linked".
    account_connected: bool | None = None
    #: Where to go and fix it. Publisher-specific, so it comes from Twitch.
    account_link_url: str | None = None

    @property
    def claimable(self) -> list[Drop]:
        return [d for d in self.drops if d.claimable]

    @property
    def next_drop(self) -> Drop | None:
        """Closest unclaimed drop to completion — what the bot is working toward."""
        pending = [d for d in self.drops if not d.complete]
        return min(pending, key=lambda d: d.remaining_minutes) if pending else None

    @property
    def hours_left(self) -> float | None:
        if self.ends_at is None:
            return None
        return (self.ends_at - datetime.now(UTC)).total_seconds() / 3600

    @property
    def expiring_soon(self) -> bool:
        left = self.hours_left
        return left is not None and 0 < left <= 24


@dataclass(frozen=True, slots=True)
class SessionProgress:
    """Twitch's server-side view of the current drop session."""

    #: ``None`` when Twitch reports no active drop session at all — usually the
    #: channel's campaign has ended, the account isn't linked, or the region is
    #: excluded. Distinct from ``current_minutes == 0``, which means a session
    #: exists but hasn't credited yet.
    active: bool
    channel_login: str | None = None
    game: str | None = None
    drop_id: str | None = None
    drop_name: str | None = None
    current_minutes: int = 0
    required_minutes: int = 0

    @property
    def remaining_minutes(self) -> int:
        return max(0, self.required_minutes - self.current_minutes)

    @property
    def fraction(self) -> float:
        if self.required_minutes <= 0:
            return 0.0
        return min(1.0, self.current_minutes / self.required_minutes)

    def describe(self) -> str:
        if not self.active:
            return "no active drop session — nothing is being credited"
        name = self.drop_name or self.drop_id or "current drop"
        return (
            f"{name}: {self.current_minutes}/{self.required_minutes} min "
            f"({self.fraction * 100:.0f}%)"
        )


class DropsClient:
    def __init__(self, gql: GQLClient) -> None:
        self._gql = gql

    async def current_session(self, channel_login: str | None = None) -> SessionProgress:
        """Read the authoritative minutes-watched count for the active session."""
        variables = {"channelLogin": channel_login} if channel_login else {}
        data = await self._gql.call("DropCurrentSessionContext", variables)

        session = pluck(data, "currentUser", "dropCurrentSession", default=None)
        if session is None:
            return SessionProgress(active=False)

        return SessionProgress(
            active=True,
            # Verified against the live schema: ``Channel`` exposes ``name``, not
            # ``login`` — asking for ``login`` is a hard query error, not a null.
            channel_login=pluck(session, "channel", "name", default=None),
            game=pluck(session, "game", "name", default=None),
            drop_id=pluck(session, "dropID", default=None),
            drop_name=None,  # not on dropCurrentSession; resolved from Inventory in phase 4
            # These two are the point of the call, so absence is drift, not zero.
            current_minutes=int(pluck(session, "currentMinutesWatched") or 0),
            required_minutes=int(pluck(session, "requiredMinutesWatched") or 0),
        )

    async def inventory(self) -> list[Campaign]:
        """Every active campaign with per-drop progress and claim state.

        Several campaigns credit from the same watch time at once, so this returns
        all of them rather than trying to pick one.
        """
        data = await self._gql.call("Inventory", {})
        raw = pluck(data, "currentUser", "inventory", "dropCampaignsInProgress",
                    default=None)
        if raw is None:
            return []

        campaigns: list[Campaign] = []
        for entry in raw:
            drops: list[Drop] = []
            for item in pluck(entry, "timeBasedDrops", default=None) or []:
                mine = pluck(item, "self", default=None) or {}
                benefits = [
                    Benefit(
                        id=pluck(edge, "benefit", "id", default="") or "",
                        name=pluck(edge, "benefit", "name", default="") or "",
                        image_url=pluck(edge, "benefit", "imageAssetURL", default=None),
                    )
                    for edge in (pluck(item, "benefitEdges", default=None) or [])
                    if pluck(edge, "benefit", default=None)
                ]
                drops.append(Drop(
                    id=pluck(item, "id", default="") or "",
                    name=pluck(item, "name", default="") or "",
                    # Absence here is drift: a drop with no minute target would
                    # silently read as already complete.
                    required_minutes=int(pluck(item, "requiredMinutesWatched") or 0),
                    current_minutes=int(pluck(mine, "currentMinutesWatched", default=0) or 0),
                    claimed=bool(pluck(mine, "isClaimed", default=False)),
                    benefits=benefits,
                    ends_at=_parse_ts(pluck(item, "endAt", default=None)),
                    instance_id=pluck(mine, "dropInstanceID", default=None),
                ))

            # Ascending by requirement so the UI reads as a ladder, which is how
            # tiered campaigns are actually designed.
            drops.sort(key=lambda d: d.required_minutes)

            campaigns.append(Campaign(
                id=pluck(entry, "id", default="") or "",
                name=pluck(entry, "name", default="") or "",
                status=pluck(entry, "status", default="") or "",
                game=pluck(entry, "game", "name", default=None),
                game_slug=pluck(entry, "game", "slug", default=None),
                starts_at=_parse_ts(pluck(entry, "startAt", default=None)),
                ends_at=_parse_ts(pluck(entry, "endAt", default=None)),
                drops=drops,
                account_connected=pluck(entry, "self", "isAccountConnected",
                                        default=None),
                account_link_url=pluck(entry, "accountLinkURL", default=None),
            ))

        # Soonest to expire first: that's the one worth watching for.
        campaigns.sort(key=lambda c: (c.ends_at is None, c.ends_at))
        return campaigns

    @staticmethod
    def link_status(campaigns: list[Campaign]) -> tuple[bool | None, str | None]:
        """Is the required external account linked? ``(verdict, fix_url)``.

        ``True``  -- at least one campaign confirms the connection.
        ``False`` -- a campaign says outright that it is missing.
        ``None``  -- unknowable right now, which is *not* the same as False.

        The None case is the honest answer when there are no campaigns in
        progress: an unlinked account and an account with nothing active look
        identical from here, and guessing would produce a scary banner for
        someone whose setup is fine and simply between campaigns.
        """
        known = [c for c in campaigns if c.account_connected is not None]
        if not known:
            return None, None
        missing = [c for c in known if not c.account_connected]
        if missing:
            url = next((c.account_link_url for c in missing if c.account_link_url), None)
            return False, url
        return True, None

    async def claimable(self) -> list[Drop]:
        """Every earned-but-uncollected drop, across all campaigns."""
        return [d for c in await self.inventory() for d in c.claimable]

    async def claim(self, drop: Drop) -> str:
        """Collect one earned drop.

        **Twitch gates this behind a Client-Integrity token that this bot does not
        produce**, so in practice it raises :class:`IntegrityChallengeError` and the
        reward has to be collected in a browser. The implementation is correct and
        kept live deliberately: if Twitch ever stops gating the mutation, auto-claim
        starts working with no code change.
        """
        if not drop.instance_id:
            raise ValueError(
                f"{drop.name}: no dropInstanceID — Twitch only assigns one once a "
                "drop is actually earned"
            )
        data = await self._gql.call(
            "DropsPage_ClaimDropRewards",
            {"input": {"dropInstanceID": drop.instance_id}},
        )
        status = pluck(data, "claimDropRewards", "status", default=None)
        if not status:
            raise SchemaDriftError(f"{drop.name}: claim returned no status")
        log.info("claimed %s: %s", drop.reward_name, status)
        return str(status)
