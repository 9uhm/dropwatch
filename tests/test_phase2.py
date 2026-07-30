"""Phase 2 tests: GraphQL transport, drift detection, channel state, telemetry.

Network is never touched. The focus is on the decisions that are easy to get
wrong and expensive to get wrong quietly:

* a renamed field must raise, not read as zero progress
* a rerun must not count as watchable
* a rejected telemetry POST must not read as a stream ending
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from dropwatch.config import TwitchConfig, WatchConfig
from dropwatch.events import EventBus, EventType
from dropwatch.twitch.channels import ChannelClient, StreamInfo, _stream_info
from dropwatch.twitch.drops import Campaign, Drop, DropsClient
from dropwatch.twitch.gql import (
    GQLClient,
    GQLError,
    GQLTransportError,
    IntegrityChallengeError,
    Operation,
    PersistedQueryNotFound,
    SchemaDriftError,
    build_registry,
    load_operation_overrides,
    pluck,
)
from dropwatch.twitch.spade import SpadeClient

# --------------------------------------------------------------------- fake http


class FakeResponse:
    def __init__(self, status: int, body: Any, *, text: str | None = None) -> None:
        self.status = status
        self._body = body
        self._text = text

    async def json(self, content_type: Any = None) -> Any:
        return self._body

    async def text(self) -> str:
        return self._text if self._text is not None else json.dumps(self._body)

    async def read(self) -> bytes:
        return (await self.text()).encode()

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


class FakeSession:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self._responses = responses
        self.posts: list[dict[str, Any]] = []
        self.gets: list[str] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        self.posts.append({"url": url, **kwargs})
        return self._pop(url)

    def get(self, url: str, **kwargs: Any) -> Any:
        self.gets.append(url)
        return self._pop(url)

    def _pop(self, url: str) -> Any:
        if not self._responses:
            raise AssertionError(f"unexpected request to {url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeAuth:
    """Stands in for AuthManager; records refreshes so retry logic is observable."""

    def __init__(self, *, token: str = "tok") -> None:
        self._token = token
        self.authenticated = True
        self.refreshes = 0

    async def ensure_valid(self) -> str:
        return self._token

    async def refresh(self) -> None:
        self.refreshes += 1


def _client(
    responses: list[FakeResponse | Exception],
    *,
    bus: EventBus | None = None,
    operations: dict[str, Operation] | None = None,
    **cfg: Any,
) -> tuple[GQLClient, FakeSession, FakeAuth]:
    session = FakeSession(responses)
    auth = FakeAuth()
    config = TwitchConfig(gql_backoff_base=0.001, gql_backoff_max=0.001, **cfg)
    client = GQLClient(
        session,  # type: ignore[arg-type]
        auth,  # type: ignore[arg-type]
        config,
        bus or EventBus(history_size=20),
        operations=operations,
    )
    return client, session, auth


def _ok(data: Any) -> FakeResponse:
    return FakeResponse(200, {"data": data})


# -------------------------------------------------------------------------- pluck


def test_pluck_walks_and_raises_on_drift() -> None:
    data = {"user": {"stream": {"id": "42", "game": {"name": "Overwatch"}}}}
    assert pluck(data, "user", "stream", "id") == "42"
    assert pluck(data, "user", "stream", "game", "name") == "Overwatch"

    # The whole point: a renamed field is an error, not a silent None. Returning
    # None here would be indistinguishable from "zero minutes credited".
    with pytest.raises(SchemaDriftError, match="currentMinutesWatched"):
        pluck(data, "user", "stream", "currentMinutesWatched")

    # ...unless absence is genuinely meaningful, which the caller opts into.
    assert pluck(data, "user", "stream", "viewersCount", default=None) is None


def test_pluck_reports_the_path_that_broke() -> None:
    with pytest.raises(SchemaDriftError, match="user -> inventory"):
        pluck({"user": {}}, "user", "inventory", "campaigns")


def test_pluck_handles_lists_and_wrong_types() -> None:
    assert pluck({"edges": [{"node": 1}]}, "edges", 0, "node") == 1
    with pytest.raises(SchemaDriftError):
        pluck({"edges": []}, "edges", 0)
    with pytest.raises(SchemaDriftError):
        pluck({"n": 5}, "n", "deeper")


# ---------------------------------------------------------------- registry / call


def test_registry_overrides_only_known_operations() -> None:
    ops = build_registry({"UseLive": "beef" * 16, "NotAnOp": "x"})
    assert ops["UseLive"].sha256 == "beef" * 16
    assert "NotAnOp" not in ops
    # Overriding the hash must not discard the verified document.
    assert ops["UseLive"].document


def test_operation_overrides_tolerate_bad_files(tmp_path: Any) -> None:
    missing = tmp_path / "nope.json"
    assert load_operation_overrides(missing) == {}

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", "utf-8")
    assert load_operation_overrides(broken) == {}

    wrong_shape = tmp_path / "list.json"
    wrong_shape.write_text("[1,2]", "utf-8")
    assert load_operation_overrides(wrong_shape) == {}

    good = tmp_path / "good.json"
    good.write_text(json.dumps({"UseLive": "abc"}), "utf-8")
    assert load_operation_overrides(good) == {"UseLive": "abc"}


async def test_call_prefers_document_and_sends_no_hash() -> None:
    client, session, _ = _client([_ok({"user": None})])
    await client.call("UseLive", {"channelLogin": "x"})

    sent = session.posts[0]["json"]
    assert "query" in sent and "extensions" not in sent
    assert sent["operationName"] == "UseLive"


async def test_call_uses_hash_when_documents_disabled() -> None:
    client, session, _ = _client([_ok({"user": None})], prefer_documents=False)
    await client.call("UseLive", {"channelLogin": "x"})

    sent = session.posts[0]["json"]
    assert sent["extensions"]["persistedQuery"]["sha256Hash"]
    assert "query" not in sent


async def test_stale_hash_falls_back_to_document_and_sticks() -> None:
    """A rotated hash must degrade to the document, once, not on every call."""
    ops = {
        "UseLive": Operation(
            name="UseLive", sha256="stale", document="query UseLive { x }",
            requires_auth=False,
        )
    }
    client, session, _ = _client(
        [
            FakeResponse(200, {"errors": [{"message": "PersistedQueryNotFound"}]}),
            _ok({"user": None}),
            _ok({"user": None}),
        ],
        operations=ops,
        prefer_documents=False,
    )

    await client.call("UseLive", {})
    assert "UseLive" in client.stale_operations
    assert "query" in session.posts[1]["json"]

    # Second call should go straight to the document — no wasted round trip.
    await client.call("UseLive", {})
    assert len(session.posts) == 3
    assert "query" in session.posts[2]["json"]


async def test_stale_hash_without_document_raises_and_reports_drift() -> None:
    bus = EventBus(history_size=10)
    seen: list[Any] = []
    bus.subscribe(lambda e: seen.append(e), EventType.SCHEMA_DRIFT)

    ops = {"Solo": Operation(name="Solo", sha256="stale", document=None)}
    client, _, _ = _client(
        [FakeResponse(200, {"errors": [{"message": "PersistedQueryNotFound"}]})],
        bus=bus,
        operations=ops,
        prefer_documents=False,
    )

    with pytest.raises(PersistedQueryNotFound):
        await client.call("Solo", {})
    assert len(seen) == 1 and seen[0].get("operation") == "Solo"


async def test_retries_5xx_then_succeeds() -> None:
    client, session, _ = _client(
        [FakeResponse(503, {}), FakeResponse(502, {}), _ok({"user": None})]
    )
    assert await client.call("UseLive", {}) == {"user": None}
    assert len(session.posts) == 3


async def test_gives_up_after_max_retries() -> None:
    client, session, _ = _client([FakeResponse(503, {})] * 3, gql_max_retries=2)
    with pytest.raises(GQLTransportError):
        await client.call("UseLive", {})
    assert len(session.posts) == 3


async def test_401_triggers_one_refresh_then_retries() -> None:
    client, session, auth = _client([FakeResponse(401, {}), _ok({"user": None})])
    await client.call("DropCurrentSessionContext", {})
    assert auth.refreshes == 1
    assert len(session.posts) == 2


async def test_graphql_errors_surface_and_non_json_is_drift() -> None:
    client, _, _ = _client([FakeResponse(200, {"errors": [{"message": "boom"}]})])
    with pytest.raises(GQLError, match="boom"):
        await client.call("UseLive", {})

    client2, _, _ = _client([FakeResponse(200, None, text="<html>502</html>")])
    with pytest.raises(SchemaDriftError, match="not JSON"):
        await client2.call("UseLive", {})


async def test_missing_data_key_is_drift() -> None:
    client, _, _ = _client([FakeResponse(200, {"extensions": {}})])
    with pytest.raises(SchemaDriftError, match="no data"):
        await client.call("UseLive", {})


async def test_batched_list_response_is_unwrapped() -> None:
    client, _, _ = _client([FakeResponse(200, [{"data": {"user": {"id": "1"}}}])])
    assert await client.call("UseLive", {}) == {"user": {"id": "1"}}


# ------------------------------------------------------------------- stream state


def test_rerun_is_live_but_never_eligible() -> None:
    """The distinction the whole watcher depends on."""
    live = _stream_info("x", {"id": "1", "login": "x", "stream": {"id": "9", "type": "live"}})
    assert live.live and live.drops_eligible and not live.is_rerun

    rerun = _stream_info("x", {"id": "1", "login": "x", "stream": {"id": "9", "type": "rerun"}})
    assert rerun.live, "a rerun really is broadcasting"
    assert rerun.is_rerun
    assert not rerun.drops_eligible, "but Twitch credits no drops for it"
    assert "drops do not credit" in rerun.describe()


def test_offline_and_unknown_channels_are_ordinary_answers() -> None:
    offline = _stream_info("x", {"id": "1", "login": "x", "stream": None})
    assert not offline.live and not offline.drops_eligible
    assert offline.channel_id == "1"

    missing = _stream_info("ghost", None)
    assert not missing.live and missing.channel_id is None


def test_present_stream_missing_its_id_is_drift() -> None:
    # Offline is a null stream; a stream object with no id is a changed schema.
    with pytest.raises(SchemaDriftError):
        _stream_info("x", {"id": "1", "login": "x", "stream": {"type": "live"}})


def test_started_at_parsing_survives_junk() -> None:
    info = _stream_info(
        "x", {"id": "1", "login": "x",
              "stream": {"id": "9", "type": "live", "createdAt": "not-a-date"}}
    )
    assert info.started_at is None and info.uptime_seconds is None


async def test_fetch_many_isolates_a_failing_channel() -> None:
    """One broken channel must not blank the whole priority list."""
    client, _, _ = _client(
        [
            _ok({"user": {"id": "1", "login": "good", "stream": {"id": "5", "type": "live"}}}),
            FakeResponse(200, {"errors": [{"message": "kaboom"}]}),
        ]
    )
    channels = ChannelClient(client, WatchConfig())
    result = await channels.fetch_many(["good", "bad"])

    assert result["good"].drops_eligible
    assert not result["bad"].live, "a failed lookup degrades to offline, not to a crash"


async def test_playback_probe_distinguishes_failure_from_drift() -> None:
    client, _, _ = _client([_ok({"streamPlaybackAccessToken": {"value": "abc"}})])
    channels = ChannelClient(client, WatchConfig())
    assert await channels.playback_probe("x") is True

    # A plain error means "not watchable" — a usable answer for signal S3.
    client2, _, _ = _client([FakeResponse(200, {"errors": [{"message": "nope"}]})])
    assert await ChannelClient(client2, WatchConfig()).playback_probe("x") is False

    # Drift must not be swallowed into a confident False.
    client3, _, _ = _client([FakeResponse(200, {"extensions": {}})])
    with pytest.raises(SchemaDriftError):
        await ChannelClient(client3, WatchConfig()).playback_probe("x")


async def test_discovery_filters_reruns_and_sorts_by_viewers() -> None:
    def node(login: str, viewers: int, type_: str = "live") -> dict[str, Any]:
        return {"node": {"id": login, "type": type_, "viewersCount": viewers,
                         "broadcaster": {"id": login, "login": login},
                         "game": {"name": "Overwatch"}}}

    client, _, _ = _client([
        _ok({"game": {"streams": {"edges": [
            node("small", 10), node("big", 900), node("vod", 5000, "rerun"),
            {"node": None}, {"node": {"id": "x", "broadcaster": {}}},
        ]}}})
    ])
    found = await ChannelClient(client, WatchConfig()).discover()

    assert [s.login for s in found] == ["big", "small"], "rerun excluded, sorted by viewers"


async def test_discovery_with_no_game_is_drift_not_emptiness() -> None:
    """A bad slug returning null must not look like 'nobody is streaming'."""
    client, _, _ = _client([_ok({"game": None})])
    with pytest.raises(SchemaDriftError, match="game_slug"):
        await ChannelClient(client, WatchConfig()).discover()


# ------------------------------------------------------------------ drop progress


async def test_session_progress_reads_channel_name_not_login() -> None:
    client, _, _ = _client([
        _ok({"currentUser": {"dropCurrentSession": {
            "channel": {"id": "1", "name": "ow_esports"},
            "game": {"name": "Overwatch"},
            "dropID": "d1",
            "currentMinutesWatched": 15,
            "requiredMinutesWatched": 60,
        }}})
    ])
    progress = await DropsClient(client).current_session("ow_esports")

    assert progress.active and progress.channel_login == "ow_esports"
    assert progress.current_minutes == 15 and progress.remaining_minutes == 45
    assert progress.fraction == pytest.approx(0.25)
    assert "15/60" in progress.describe()


async def test_no_drop_session_is_inactive_not_zero() -> None:
    """'No campaign covers this' must be distinguishable from 'zero credited'."""
    client, _, _ = _client([_ok({"currentUser": {"dropCurrentSession": None}})])
    progress = await DropsClient(client).current_session()

    assert not progress.active
    assert "nothing is being credited" in progress.describe()


async def test_missing_minute_counts_are_drift() -> None:
    client, _, _ = _client([
        _ok({"currentUser": {"dropCurrentSession": {
            "channel": {"id": "1", "name": "c"}, "dropID": "d",
            "minutesWatched": 5,  # renamed field
        }}})
    ])
    with pytest.raises(SchemaDriftError, match="currentMinutesWatched"):
        await DropsClient(client).current_session()


def test_progress_fraction_is_safe_when_nothing_is_required() -> None:
    from dropwatch.twitch.drops import SessionProgress

    assert SessionProgress(active=True, required_minutes=0).fraction == 0.0


# ------------------------------------------------------------------- inventory


def _drop(name: str, req: int, cur: int, claimed: bool = False) -> dict[str, Any]:
    return {
        "id": f"d-{name}", "name": name, "requiredMinutesWatched": req,
        "endAt": "2026-08-24T13:00:00Z",
        "self": {"currentMinutesWatched": cur, "isClaimed": claimed},
        "benefitEdges": [{"benefit": {
            "id": f"b-{name}", "name": f"{name} reward",
            "imageAssetURL": f"https://cdn/{name}.png",
        }}],
    }


def _inventory(*campaigns: dict[str, Any]) -> dict[str, Any]:
    return {"currentUser": {"inventory": {"dropCampaignsInProgress": list(campaigns)}}}


async def test_inventory_identifies_claimable_rewards() -> None:
    """Earned-but-unclaimed is the state the dashboard exists to surface."""
    client, _, _ = _client([_ok(_inventory({
        "id": "c1", "name": "EWC 2026", "status": "ACTIVE",
        "game": {"name": "Special Events", "slug": "special-events"},
        "startAt": "2026-07-08T13:00:00Z", "endAt": "2026-08-24T13:00:00Z",
        "timeBasedDrops": [
            _drop("Bronze", 60, 67, claimed=True),
            _drop("Silver", 120, 67),
            _drop("Gold", 180, 67),
        ],
    }))])

    campaigns = await DropsClient(client).inventory()
    assert len(campaigns) == 1
    camp = campaigns[0]

    bronze, silver, gold = camp.drops
    assert bronze.complete and bronze.claimed and not bronze.claimable
    assert not silver.complete and not silver.claimable
    assert camp.claimable == [], "nothing earned-and-unclaimed here"

    # Now push past Silver's requirement.
    earned = Drop(id="x", name="Silver", required_minutes=120, current_minutes=120,
                  claimed=False)
    assert earned.complete and earned.claimable

    assert silver.remaining_minutes == 53
    assert gold.fraction == pytest.approx(67 / 180)
    assert silver.reward_name == "Silver reward"
    assert silver.image_url == "https://cdn/Silver.png"


async def test_inventory_orders_drops_as_a_ladder_and_campaigns_by_expiry() -> None:
    client, _, _ = _client([_ok(_inventory(
        {
            "id": "late", "name": "Later", "status": "ACTIVE",
            "endAt": "2026-09-01T00:00:00Z",
            "timeBasedDrops": [_drop("big", 300, 0), _drop("small", 30, 0)],
        },
        {
            "id": "soon", "name": "Sooner", "status": "ACTIVE",
            "endAt": "2026-07-30T00:00:00Z", "timeBasedDrops": [_drop("x", 60, 0)],
        },
    ))])

    campaigns = await DropsClient(client).inventory()
    assert [c.name for c in campaigns] == ["Sooner", "Later"], "soonest to expire first"
    assert [d.required_minutes for d in campaigns[1].drops] == [30, 300], "ascending ladder"


async def test_next_drop_is_the_closest_unclaimed_target() -> None:
    client, _, _ = _client([_ok(_inventory({
        "id": "c", "name": "C", "status": "ACTIVE", "endAt": None,
        "timeBasedDrops": [
            _drop("done", 30, 40, claimed=True),
            _drop("near", 60, 55),
            _drop("far", 600, 55),
        ],
    }))])

    camp = (await DropsClient(client).inventory())[0]
    nxt = camp.next_drop
    assert nxt is not None and nxt.name == "near" and nxt.remaining_minutes == 5


async def test_empty_inventory_is_not_an_error() -> None:
    client, _, _ = _client([_ok({"currentUser": {"inventory":
                                                 {"dropCampaignsInProgress": []}}})])
    assert await DropsClient(client).inventory() == []


async def test_drop_missing_its_requirement_is_drift() -> None:
    """A zero requirement would read as 'already complete' for every drop."""
    client, _, _ = _client([_ok(_inventory({
        "id": "c", "name": "C", "status": "ACTIVE", "endAt": None,
        "timeBasedDrops": [{"id": "d", "name": "d", "self": {}, "benefitEdges": []}],
    }))])
    with pytest.raises(SchemaDriftError, match="requiredMinutesWatched"):
        await DropsClient(client).inventory()


async def test_integrity_challenge_is_its_own_error() -> None:
    """Must not be reported as schema drift — nothing is wrong with the schema.

    Twitch emits a generic error alongside the challenge, so checking `errors`
    first would send someone hunting a field rename that doesn't exist.
    """
    client, _, _ = _client([FakeResponse(200, {
        "errors": [{"message": "failed integrity check"}],
        "data": {"claimDropRewards": None},
        "extensions": {"challenge": {"type": "integrity"}},
    })])

    with pytest.raises(IntegrityChallengeError, match="Client-Integrity"):
        await client.call("DropsPage_ClaimDropRewards", {"input": {}})


async def test_claim_reports_the_integrity_gate_rather_than_failing_obscurely() -> None:
    client, _, _ = _client([FakeResponse(200, {
        "data": {"claimDropRewards": None},
        "extensions": {"challenge": {"type": "integrity"}},
    })])
    drop = Drop(id="d", name="Icon", required_minutes=60, current_minutes=70,
                claimed=False, instance_id="user#camp#drop")

    with pytest.raises(IntegrityChallengeError):
        await DropsClient(client).claim(drop)


async def test_claim_succeeds_if_twitch_ever_stops_gating_it() -> None:
    """The claim path is kept correct so it just works if the gate is lifted."""
    client, session, _ = _client([_ok({"claimDropRewards": {"status": "ELIGIBLE"}})])
    drop = Drop(id="d", name="Icon", required_minutes=60, current_minutes=70,
                claimed=False, instance_id="user#camp#drop")

    assert await DropsClient(client).claim(drop) == "ELIGIBLE"
    sent = session.posts[0]["json"]["variables"]["input"]
    assert sent == {"dropInstanceID": "user#camp#drop"}


async def test_claim_refuses_a_drop_with_no_instance_id() -> None:
    """Unearned drops have no instance id; claiming one is a caller bug."""
    client, _, _ = _client([])
    drop = Drop(id="d", name="Silver", required_minutes=120, current_minutes=60,
                claimed=False, instance_id=None)

    with pytest.raises(ValueError, match="dropInstanceID"):
        await DropsClient(client).claim(drop)


async def test_claimable_collects_across_every_campaign() -> None:
    client, _, _ = _client([_ok(_inventory(
        {
            "id": "a", "name": "A", "status": "ACTIVE", "endAt": None,
            "timeBasedDrops": [_drop("earned", 30, 40), _drop("gone", 30, 40, claimed=True)],
        },
        {
            "id": "b", "name": "B", "status": "ACTIVE", "endAt": None,
            "timeBasedDrops": [_drop("also", 60, 99), _drop("nope", 600, 99)],
        },
    ))])

    pending = await DropsClient(client).claimable()
    assert sorted(d.name for d in pending) == ["also", "earned"]


async def test_instance_id_is_carried_through_from_inventory() -> None:
    raw = _inventory({
        "id": "c", "name": "C", "status": "ACTIVE", "endAt": None,
        "timeBasedDrops": [_drop("earned", 30, 40)],
    })
    raw["currentUser"]["inventory"]["dropCampaignsInProgress"][0]["timeBasedDrops"][0][
        "self"]["dropInstanceID"] = "u#c#d"

    client, _, _ = _client([_ok(raw)])
    drop = (await DropsClient(client).inventory())[0].drops[0]
    assert drop.instance_id == "u#c#d" and drop.claimable


def test_expiring_soon_only_covers_the_next_day() -> None:
    from datetime import UTC, datetime, timedelta

    def camp(hours: float) -> Campaign:
        return Campaign(id="c", name="c", status="ACTIVE",
                        ends_at=datetime.now(UTC) + timedelta(hours=hours))

    assert camp(6).expiring_soon
    assert not camp(72).expiring_soon
    assert not camp(-2).expiring_soon, "already over is not 'expiring soon'"
    assert Campaign(id="c", name="c", status="ACTIVE").hours_left is None


# ---------------------------------------------------------------------- telemetry


@pytest.fixture
def stream() -> StreamInfo:
    return StreamInfo(
        login="ow_esports", channel_id="137512364", live=True,
        stream_id="317929101287", stream_type="live",
    )


def test_payload_is_the_shape_twitch_expects(stream: StreamInfo) -> None:
    encoded = SpadeClient.build_payload(stream, "100000001")
    decoded = json.loads(base64.b64decode(encoded))

    assert len(decoded) == 1
    assert decoded[0]["event"] == "minute-watched"
    props = decoded[0]["properties"]
    # These are ints on the wire, not strings — Twitch rejects the strings.
    assert props["channel_id"] == 137512364
    assert props["broadcast_id"] == 317929101287
    assert props["user_id"] == 100000001
    assert props["player"] == "site" and props["live"] is True


async def test_spade_url_is_scraped_then_cached() -> None:
    session = FakeSession([
        FakeResponse(200, None, text='x <script src="https://a.tv/config/settings.abc123.js">'),
        FakeResponse(200, None, text='{"spade_url":"https://spade.twitch.tv/track?x=1"}'),
    ])
    spade = SpadeClient(session, WatchConfig(), EventBus())  # type: ignore[arg-type]

    assert await spade.resolve_url() == "https://spade.twitch.tv/track?x=1"
    assert await spade.resolve_url() == "https://spade.twitch.tv/track?x=1"
    assert len(session.gets) == 2, "second call must be served from cache"


async def test_spade_url_falls_back_when_unscrapable() -> None:
    session = FakeSession([FakeResponse(500, None, text="")])
    spade = SpadeClient(session, WatchConfig(), EventBus())  # type: ignore[arg-type]
    assert "spade" in await spade.resolve_url()
    assert not spade.scraped


async def test_scraped_flag_survives_matching_the_fallback_value() -> None:
    """Twitch currently ships exactly the fallback URL, so value equality lies.

    A real scrape that happens to return FALLBACK_SPADE_URL must still report as
    scraped, or `doctor` calls a working setup broken.
    """
    from dropwatch.twitch.spade import FALLBACK_SPADE_URL

    session = FakeSession([
        FakeResponse(200, None, text=f'{{"spade_url":"{FALLBACK_SPADE_URL}"}}'),
    ])
    spade = SpadeClient(session, WatchConfig(), EventBus())  # type: ignore[arg-type]

    assert await spade.resolve_url() == FALLBACK_SPADE_URL
    assert spade.scraped, "resolved from Twitch, even though it equals the fallback"


async def test_rejected_telemetry_degrades_but_does_not_raise(stream: StreamInfo) -> None:
    """Telemetry failure is signal S4, not an exception and not a stream ending."""
    bus = EventBus(history_size=10)
    degraded: list[Any] = []
    bus.subscribe(lambda e: degraded.append(e), EventType.TELEMETRY_DEGRADED)

    session = FakeSession([
        FakeResponse(200, None, text='{"spade_url":"https://spade.twitch.tv/track"}'),
        FakeResponse(400, None, text="bad"),
        FakeResponse(400, None, text="bad"),
    ])
    spade = SpadeClient(session, WatchConfig(), EventBus())  # type: ignore[arg-type]
    spade._bus = bus  # type: ignore[attr-defined]
    await spade.resolve_url()

    first = await spade.send_minute_watched(stream, "1")
    assert not first.accepted and not spade.degraded

    second = await spade.send_minute_watched(stream, "1")
    assert not second.accepted and spade.degraded
    assert len(degraded) == 1, "announced once on crossing the threshold, not per cycle"


async def test_accepted_telemetry_clears_failures(stream: StreamInfo) -> None:
    session = FakeSession([
        FakeResponse(200, None, text='{"spade_url":"https://spade.twitch.tv/track"}'),
        FakeResponse(500, None, text=""),
        FakeResponse(204, None, text=""),
    ])
    spade = SpadeClient(session, WatchConfig(), EventBus())  # type: ignore[arg-type]
    await spade.resolve_url()

    await spade.send_minute_watched(stream, "1")
    assert spade.consecutive_failures == 1
    result = await spade.send_minute_watched(stream, "1")
    assert result.accepted and spade.consecutive_failures == 0


async def test_telemetry_refuses_without_broadcast_id() -> None:
    """Reporting with a missing broadcast id would silently credit nothing."""
    spade = SpadeClient(FakeSession([]), WatchConfig(), EventBus())  # type: ignore[arg-type]
    result = await spade.send_minute_watched(StreamInfo(login="x", live=True), "1")
    assert not result.accepted and "broadcast_id" in (result.error or "")


def test_interval_is_jittered_within_bounds() -> None:
    watch = WatchConfig(minute_watched_interval=58, minute_watched_jitter=3)
    spade = SpadeClient(FakeSession([]), watch, EventBus())  # type: ignore[arg-type]
    values = {round(spade.next_interval(), 4) for _ in range(200)}

    assert all(55 <= v <= 61 for v in values)
    assert len(values) > 50, "a fixed interval would be a perfect metronome"
