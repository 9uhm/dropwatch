"""Phase 1 tests: event bus, store, config layering, token lifecycle.

Network is never touched — the device flow and refresh are driven against a fake
aiohttp session so the token state machine is tested without a live Twitch.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from dropwatch.config import AppConfig, ConfigError, ConfigManager, parse_override_value
from dropwatch.events import EventBus, EventType
from dropwatch.store import Store, Transition
from dropwatch.twitch.auth import (
    AuthManager,
    AuthNeededError,
    DeviceFlowDenied,
    TokenSet,
    TokenStore,
)

# ------------------------------------------------------------------ fake aiohttp


class FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type: Any = None) -> Any:
        return self._body

    async def text(self) -> str:
        return json.dumps(self._body)

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


class FakeSession:
    """Returns queued responses in order and records the requests made."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def _next(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((method, url, kwargs.get("data") or kwargs.get("headers") or {}))
        if not self._responses:
            raise AssertionError(f"unexpected extra request: {method} {url}")
        return self._responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("POST", url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("GET", url, **kwargs)


@pytest.fixture
def bus() -> EventBus:
    return EventBus(history_size=50)


@pytest.fixture
def token_store(tmp_path: Any) -> TokenStore:
    return TokenStore(tmp_path / "tokens.json")


# ---------------------------------------------------------------------- event bus


async def test_bus_delivers_to_type_and_wildcard_subscribers(bus: EventBus) -> None:
    typed: list[str] = []
    everything: list[str] = []

    bus.subscribe(lambda e: typed.append(e.type), EventType.WATCH_STARTED)
    bus.subscribe(lambda e: everything.append(e.type))

    await bus.publish(EventType.WATCH_STARTED, channel="foo")
    await bus.publish(EventType.STREAM_ENDED, channel="foo")

    assert typed == [EventType.WATCH_STARTED]
    assert everything == [EventType.WATCH_STARTED, EventType.STREAM_ENDED]


async def test_failing_handler_does_not_stop_delivery(bus: EventBus) -> None:
    """A broken Discord notifier must never take down the watch loop."""
    delivered: list[str] = []

    def boom(_: Any) -> None:
        raise RuntimeError("notifier exploded")

    bus.subscribe(boom, EventType.DROP_CLAIMED)
    bus.subscribe(lambda e: delivered.append(e.type), EventType.DROP_CLAIMED)

    await bus.publish(EventType.DROP_CLAIMED, name="Loot Box")
    assert delivered == [EventType.DROP_CLAIMED]


async def test_unsubscribe_and_history(bus: EventBus) -> None:
    seen: list[str] = []
    off = bus.subscribe(lambda e: seen.append(e.type), EventType.ERROR)
    await bus.publish(EventType.ERROR, msg="one")
    off()
    await bus.publish(EventType.ERROR, msg="two")

    assert len(seen) == 1
    history = bus.history()
    assert [e.get("msg") for e in history] == ["two", "one"]  # newest first
    assert len(bus.history(1, EventType.ERROR)) == 1


# -------------------------------------------------------------------------- store


async def test_store_roundtrips_overrides_sessions_and_claims(tmp_path: Any) -> None:
    store = await Store(tmp_path / "t.db").open()
    try:
        await store.set_override("liveness.grace_period", 120.0)
        await store.set_override("watch.auto_claim", False)
        assert await store.get_overrides() == {
            "liveness.grace_period": 120.0,
            "watch.auto_claim": False,
        }

        await store.clear_override("watch.auto_claim")
        assert "watch.auto_claim" not in await store.get_overrides()

        sid = await store.start_session("overwatchleague")
        assert await store.open_sessions()          # dangling until ended
        await store.end_session(sid, minutes=42, reason="stream_ended")
        assert not await store.open_sessions()
        assert (await store.recent_sessions())[0]["minutes"] == 42

        await store.record_transition(
            Transition(time.time(), "owl", "WATCHING", "OFFLINE", "pubsub", {"S1": "OFFLINE"})
        )
        t = (await store.recent_transitions())[0]
        assert t.to_state == "OFFLINE" and t.signals == {"S1": "OFFLINE"}
    finally:
        await store.close()


async def test_claims_are_idempotent(tmp_path: Any) -> None:
    """Restart mid-drop must not double-count a claim."""
    store = await Store(tmp_path / "t.db").open()
    try:
        assert await store.record_claim("d1", "Loot Box", "c1", None) is True
        assert await store.record_claim("d1", "Loot Box", "c1", None) is False
        assert await store.is_claimed("d1")
        assert len(await store.claims()) == 1
    finally:
        await store.close()


# ------------------------------------------------------------------------- config


async def test_overrides_layer_over_defaults_and_persist(tmp_path: Any) -> None:
    store = await Store(tmp_path / "t.db").open()
    try:
        manager = await ConfigManager(store).load()
        assert manager.current.liveness.grace_period == 90.0

        await manager.set_override("liveness.grace_period", 150.0)
        assert manager.current.liveness.grace_period == 150.0

        # A fresh manager over the same store must see the persisted override.
        reloaded = await ConfigManager(store).load()
        assert reloaded.current.liveness.grace_period == 150.0

        await reloaded.clear_override("liveness.grace_period")
        assert reloaded.current.liveness.grace_period == 90.0
    finally:
        await store.close()


async def test_invalid_override_is_rejected_and_rolled_back(tmp_path: Any) -> None:
    store = await Store(tmp_path / "t.db").open()
    try:
        manager = await ConfigManager(store).load()

        with pytest.raises(ConfigError):
            await manager.set_override("liveness.grace_period", -5)   # violates ge=0
        with pytest.raises(ConfigError):
            await manager.set_override("nope.not_a_key", 1)

        # Rejected values must not linger in memory or on disk.
        assert manager.current.liveness.grace_period == 90.0
        assert await store.get_overrides() == {}
    finally:
        await store.close()


def test_channels_accept_bare_strings_and_urls_and_sort_by_priority() -> None:
    cfg = AppConfig.model_validate(
        {"watch": {"channels": ["https://twitch.tv/Zed", {"login": "Abc", "priority": 5}]}}
    )
    assert [c.login for c in cfg.watch.ordered()] == ["abc", "zed"]


def test_override_value_parsing() -> None:
    assert parse_override_value("true") is True
    assert parse_override_value("120.5") == 120.5
    assert parse_override_value('["a","b"]') == ["a", "b"]
    assert parse_override_value("INFO") == "INFO"   # bare word, not JSON


# -------------------------------------------------------------------------- auth


def test_needs_refresh_at_lifetime_fraction() -> None:
    now = time.time()
    fresh = TokenSet(access_token="a", obtained_at=now, expires_at=now + 1000)
    assert not fresh.needs_refresh(0.8)
    assert not fresh.expired

    stale = TokenSet(access_token="a", obtained_at=now - 900, expires_at=now + 100)
    assert stale.needs_refresh(0.8)          # 90% elapsed
    assert not stale.expired

    dead = TokenSet(access_token="a", obtained_at=now - 200, expires_at=now - 1)
    assert dead.expired

    # Twitch reports expires_in: 0 for TV-client tokens, meaning "never expires".
    # This must not read as "expired at the epoch" or the bot would refresh forever.
    forever = TokenSet(access_token="a", expires_at=0.0)
    assert forever.never_expires
    assert not forever.needs_refresh(0.8) and not forever.expired
    assert not fresh.never_expires


async def test_validate_treats_expires_in_zero_as_non_expiring(
    bus: EventBus, token_store: TokenStore
) -> None:
    """Regression: a real device-flow login returned expires_in: 0 from /validate."""
    session = FakeSession([
        FakeResponse(200, {"login": "someone", "user_id": "1", "expires_in": 0,
                           "scopes": ["user:read:follows"]}),
    ])
    auth = AuthManager(session, _twitch_cfg(), bus, token_store)
    token_store.save(TokenSet(access_token="at", refresh_token="rt"))
    auth.load_from_disk()

    await auth.validate()
    tokens = auth.tokens
    assert tokens is not None
    assert tokens.never_expires and not tokens.expired
    assert auth.authenticated


def test_token_store_roundtrip_and_atomic_write(token_store: TokenStore) -> None:
    assert token_store.load() is None
    token_store.save(TokenSet(access_token="secret-access", refresh_token="r", login="me"))

    loaded = token_store.load()
    assert loaded is not None
    assert loaded.access_token == "secret-access" and loaded.login == "me"
    assert not token_store.path.with_suffix(".json.tmp").exists()   # no temp left behind

    token_store.clear()
    assert token_store.load() is None


def test_corrupt_token_file_is_survivable(token_store: TokenStore) -> None:
    token_store.path.parent.mkdir(parents=True, exist_ok=True)
    token_store.path.write_text("{not json", encoding="utf-8")
    assert token_store.load() is None       # degrades to "re-login", not a crash


async def test_device_flow_success_persists_and_validates(
    bus: EventBus, token_store: TokenStore
) -> None:
    session = FakeSession([
        FakeResponse(200, {"device_code": "dc", "user_code": "ABCD-1234",
                           "verification_uri": "https://twitch.tv/activate",
                           "interval": 0, "expires_in": 600}),
        FakeResponse(400, {"message": "authorization_pending"}),
        FakeResponse(200, {"access_token": "at", "refresh_token": "rt",
                           "expires_in": 3600, "scope": ["user:read:follows"]}),
        FakeResponse(200, {"login": "someone", "user_id": "12345",
                           "scopes": ["user:read:follows"], "expires_in": 3600}),
    ])
    auth = AuthManager(session, _twitch_cfg(), bus, token_store)

    flow = await auth.start_device_flow()
    assert flow.user_code == "ABCD-1234"
    await auth.complete_device_flow(flow)

    assert auth.authenticated
    assert auth.tokens is not None and auth.tokens.login == "someone"
    assert auth.tokens.user_id == "12345"
    assert token_store.load().access_token == "at"     # persisted across the flow

    kinds = [e.type for e in bus.history()]
    assert EventType.AUTH_REFRESHED in kinds


async def test_device_flow_denied(bus: EventBus, token_store: TokenStore) -> None:
    session = FakeSession([
        FakeResponse(200, {"device_code": "dc", "user_code": "X", "interval": 0,
                           "expires_in": 600}),
        FakeResponse(400, {"message": "access_denied"}),
    ])
    auth = AuthManager(session, _twitch_cfg(), bus, token_store)
    flow = await auth.start_device_flow()

    with pytest.raises(DeviceFlowDenied):
        await auth.complete_device_flow(flow)
    assert not auth.authenticated


async def test_refresh_keeps_existing_refresh_token_and_identity(
    bus: EventBus, token_store: TokenStore
) -> None:
    """Twitch may omit refresh_token on refresh; losing it would force a re-login."""
    now = time.time()
    token_store.save(TokenSet(access_token="old", refresh_token="rt-keep",
                              obtained_at=now - 900, expires_at=now + 100,
                              user_id="12345", login="someone"))
    session = FakeSession([FakeResponse(200, {"access_token": "new", "expires_in": 3600})])
    auth = AuthManager(session, _twitch_cfg(), bus, token_store)
    auth.load_from_disk()

    refreshed = await auth.refresh()
    assert refreshed.access_token == "new"
    assert refreshed.refresh_token == "rt-keep"
    assert refreshed.login == "someone" and refreshed.user_id == "12345"


async def test_revoked_refresh_token_clears_state_and_emits_expired(
    bus: EventBus, token_store: TokenStore
) -> None:
    now = time.time()
    token_store.save(TokenSet(access_token="old", refresh_token="revoked",
                              obtained_at=now - 900, expires_at=now + 100))
    session = FakeSession([FakeResponse(400, {"message": "Invalid refresh token"})])
    auth = AuthManager(session, _twitch_cfg(), bus, token_store)
    auth.load_from_disk()

    with pytest.raises(AuthNeededError):
        await auth.refresh()

    assert not auth.authenticated
    assert token_store.load() is None          # cleared so we don't hot-loop on it
    assert EventType.AUTH_EXPIRED in [e.type for e in bus.history()]


async def test_ensure_valid_refreshes_once_under_concurrency(
    bus: EventBus, token_store: TokenStore
) -> None:
    """Ten concurrent callers must produce exactly one refresh request."""
    import asyncio

    now = time.time()
    token_store.save(TokenSet(access_token="old", refresh_token="rt",
                              obtained_at=now - 900, expires_at=now + 100))
    session = FakeSession([FakeResponse(200, {"access_token": "new", "expires_in": 3600})])
    auth = AuthManager(session, _twitch_cfg(), bus, token_store)
    auth.load_from_disk()

    results = await asyncio.gather(*(auth.ensure_valid() for _ in range(10)))
    assert set(results) == {"new"}
    assert len(session.requests) == 1


async def test_ensure_valid_without_token_raises_and_emits(
    bus: EventBus, token_store: TokenStore
) -> None:
    auth = AuthManager(FakeSession([]), _twitch_cfg(), bus, token_store)
    with pytest.raises(AuthNeededError):
        await auth.ensure_valid()
    assert EventType.AUTH_NEEDED in [e.type for e in bus.history()]


def _twitch_cfg():
    from dropwatch.config import TwitchConfig

    return TwitchConfig(device_poll_timeout=10.0)
