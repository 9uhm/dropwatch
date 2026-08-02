"""Tests for farming several Twitch accounts at once.

The invariants that matter here are about *separation*. Accounts share a config,
a database and an event bus; they must share nothing that carries an identity,
and every row and event they produce has to say which one it came from. A bug in
that direction credits one account's watch time to another, which is invisible
until someone wonders why the numbers don't add up.

Nothing here talks to Twitch: the account plumbing is exercised against the
registry, the store and the bus.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from dropwatch import accounts as accounts_mod
from dropwatch import paths
from dropwatch.events import EventBus, EventType
from dropwatch.store import LEGACY_ACCOUNT, Store, Transition

# --------------------------------------------------------------- account names


@pytest.mark.parametrize("name", ["fnjackttv", "Some_User", "a", "x" * 40])
def test_valid_account_names_are_accepted(name: str) -> None:
    assert paths.is_valid_account_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "has space",
        "dot.dot",
        "../escape",
        "..\\escape",
        "sub/dir",
        "x" * 41,
        "unicode⁄slash",
    ],
)
def test_hostile_account_names_are_rejected(name: str) -> None:
    # The name arrives from a Twitch response and is used to build a path, so
    # this is the boundary that keeps a login out of the rest of the filesystem.
    assert not paths.is_valid_account_name(name)
    with pytest.raises(ValueError):
        paths.token_path(name)


def test_token_paths_are_per_account_and_inside_the_accounts_dir() -> None:
    a = paths.token_path("alice")
    b = paths.token_path("bob")
    assert a != b
    assert a.parent == paths.ACCOUNTS_DIR
    assert a.name == "alice.json"
    # Case is normalised so "Alice" and "alice" are one account, not two.
    assert paths.token_path("Alice") == a


# ----------------------------------------------------------------- state root


def test_a_source_checkout_keeps_its_state_in_the_repo(monkeypatch: Any) -> None:
    monkeypatch.delenv("DROPWATCH_HOME", raising=False)
    monkeypatch.setattr(paths, "FROZEN", False)
    # Development writes where development expects to find it.
    assert paths.app_root() == pathlib.Path(paths.__file__).resolve().parents[2]


def test_a_frozen_build_defaults_to_the_user_data_directory(
    monkeypatch: Any, tmp_path: Any
) -> None:
    # The exe is a file you can leave on the Desktop; state must not follow it.
    monkeypatch.delenv("DROPWATCH_HOME", raising=False)
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(paths.sys, "executable", str(tmp_path / "dropwatch.exe"))
    assert paths.app_root() == paths.user_data_dir()


def test_an_existing_data_folder_beside_the_exe_keeps_it_portable(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """The upgrade guard: a beside-the-exe install must not be abandoned."""
    monkeypatch.delenv("DROPWATCH_HOME", raising=False)
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(paths.sys, "executable", str(tmp_path / "dropwatch.exe"))
    (tmp_path / "data").mkdir()
    assert paths.app_root() == tmp_path


def test_the_portable_marker_opts_back_in(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.delenv("DROPWATCH_HOME", raising=False)
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(paths.sys, "executable", str(tmp_path / "dropwatch.exe"))
    (tmp_path / paths.PORTABLE_MARKER).write_text("", encoding="utf-8")
    assert paths.app_root() == tmp_path


def test_dropwatch_home_beats_everything(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setenv("DROPWATCH_HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(paths.sys, "executable", str(tmp_path / "dropwatch.exe"))
    (tmp_path / "data").mkdir()
    assert paths.app_root() == (tmp_path / "elsewhere").resolve()


# --------------------------------------------------------------- token naming


def test_an_unclaimed_store_names_its_file_after_the_login(home: Any) -> None:
    from dropwatch.twitch.auth import TokenSet, TokenStore

    store = TokenStore(None)
    assert store.claimed is False
    store.save(TokenSet(access_token="tok", login="Alice", user_id="1"))
    assert store.path == paths.token_path("alice")
    assert paths.token_path("alice").is_file()


def test_a_token_with_no_login_yet_is_not_filed_as_default(home: Any) -> None:
    """Regression: the device flow saves once *before* it knows who you are.

    Naming the file from that empty login filed every newly added account as
    ``default.json``, so a second account silently overwrote the first.
    """
    from dropwatch.twitch.auth import TokenSet, TokenStore

    store = TokenStore(None)
    store.save(TokenSet(access_token="tok"))  # no login yet — as after /token
    assert store.claimed is False
    assert not (paths.ACCOUNTS_DIR / "default.json").exists()
    assert accounts_mod.discover() == []

    # The post-validate save is the one that names it.
    store.save(TokenSet(access_token="tok", login="alice", user_id="1"))
    assert accounts_mod.discover() == ["alice"]


def test_two_accounts_added_in_turn_get_separate_files(home: Any) -> None:
    from dropwatch.twitch.auth import TokenSet, TokenStore

    for login in ("alice", "bob"):
        store = TokenStore(None)
        store.save(TokenSet(access_token=f"tok-{login}"))          # pre-validate
        store.save(TokenSet(access_token=f"tok-{login}", login=login, user_id="1"))
    assert accounts_mod.discover() == ["alice", "bob"]


# ------------------------------------------------------------------- registry


def _write_token(tmp_path: Any, name: str, login: str | None = None) -> None:
    d = tmp_path / "data" / "accounts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({
        "access_token": f"tok-{name}",
        "refresh_token": None,
        "login": login if login is not None else name,
        "user_id": f"id-{name}",
        "scopes": [],
        "expires_at": 0,
        "obtained_at": 0,
    }), encoding="utf-8")


@pytest.fixture
def home(tmp_path: Any, monkeypatch: Any) -> Any:
    """Point every path constant at a throwaway directory."""
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(paths, "ACCOUNTS_DIR", tmp_path / "data" / "accounts")
    monkeypatch.setattr(paths, "TOKEN_PATH", tmp_path / "data" / "tokens.json")
    monkeypatch.setattr(paths, "DB_PATH", tmp_path / "data" / "test.db")
    return tmp_path


def test_discover_lists_every_token_file(home: Any) -> None:
    _write_token(home, "alice")
    _write_token(home, "bob")
    assert accounts_mod.discover() == ["alice", "bob"]


def test_discover_ignores_files_that_are_not_usable_account_names(home: Any) -> None:
    _write_token(home, "alice")
    (paths.ACCOUNTS_DIR / "not a login.json").write_text("{}", encoding="utf-8")
    (paths.ACCOUNTS_DIR / "notes.txt").write_text("hi", encoding="utf-8")
    assert accounts_mod.discover() == ["alice"]


def test_legacy_single_account_token_is_migrated_under_its_login(home: Any) -> None:
    # The upgrade path: someone who has been farming one account must not open
    # the new build and find no accounts configured.
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    paths.TOKEN_PATH.write_text(json.dumps({
        "access_token": "legacy", "refresh_token": None, "login": "OldUser",
        "user_id": "42", "scopes": [], "expires_at": 0, "obtained_at": 0,
    }), encoding="utf-8")

    name = accounts_mod.migrate_legacy_token()
    assert name == "olduser"
    assert paths.token_path("olduser").is_file()
    # Renamed, not deleted: if any of this is wrong the tokens are still there.
    assert not paths.TOKEN_PATH.exists()
    assert paths.TOKEN_PATH.with_suffix(".json.migrated").is_file()


def test_migration_does_not_run_twice(home: Any) -> None:
    _write_token(home, "alice")
    paths.TOKEN_PATH.write_text(json.dumps({
        "access_token": "legacy", "refresh_token": None, "login": "bob",
        "user_id": "1", "scopes": [], "expires_at": 0, "obtained_at": 0,
    }), encoding="utf-8")
    # Accounts already exist, so the stale legacy file must be left alone rather
    # than resurrecting an account someone removed on purpose.
    assert accounts_mod.migrate_legacy_token() is None
    assert not paths.token_path("bob").exists()


def test_migration_is_a_no_op_with_nothing_to_migrate(home: Any) -> None:
    assert accounts_mod.migrate_legacy_token() is None


async def test_registry_reports_and_persists_enabled_state(home: Any) -> None:
    _write_token(home, "alice")
    _write_token(home, "bob")
    store = await Store(paths.DB_PATH).open()
    try:
        registry = await accounts_mod.Registry(store).load()
        assert [a.name for a in registry.list()] == ["alice", "bob"]
        assert len(registry.enabled()) == 2

        await registry.set_enabled("bob", False)
        assert [a.name for a in registry.enabled()] == ["alice"]

        # The preference outlives the object holding it.
        again = await accounts_mod.Registry(store).load()
        assert [a.name for a in again.enabled()] == ["alice"]
        assert again.disabled_names() == {"bob"}
    finally:
        await store.close()


async def test_removing_an_account_deletes_its_token_only(home: Any) -> None:
    _write_token(home, "alice")
    _write_token(home, "bob")
    store = await Store(paths.DB_PATH).open()
    try:
        registry = await accounts_mod.Registry(store).load()
        assert await registry.remove("bob") is True
        assert not paths.token_path("bob").exists()
        assert paths.token_path("alice").exists()
        assert [a.name for a in registry.list()] == ["alice"]
        # Removing something already gone is not an error.
        assert await registry.remove("bob") is False
    finally:
        await store.close()


async def test_an_unreadable_token_is_listed_but_not_farmed(home: Any) -> None:
    # Hiding a broken account would present "it stopped earning" as "it was never
    # configured", which is the harder problem to notice.
    _write_token(home, "alice")
    paths.token_path("broken").write_text("{ not json", encoding="utf-8")
    store = await Store(paths.DB_PATH).open()
    try:
        registry = await accounts_mod.Registry(store).load()
        listed = {a.name: a for a in registry.list()}
        assert set(listed) == {"alice", "broken"}
        assert listed["broken"].usable is False
        assert [a.name for a in registry.enabled()] == ["alice"]
    finally:
        await store.close()


# ---------------------------------------------------------------------- store


async def test_history_rows_are_attributed_to_their_account(home: Any) -> None:
    store = await Store(paths.DB_PATH).open()
    try:
        a = await store.start_session("chan_a", "alice")
        b = await store.start_session("chan_b", "bob")
        await store.end_session(a, 10, "done")
        await store.end_session(b, 25, "done")

        totals = {r["account"]: r["minutes"] for r in await store.account_totals()}
        assert totals == {"alice": 10, "bob": 25}
    finally:
        await store.close()


async def test_reconcile_only_closes_its_own_accounts_sessions(home: Any) -> None:
    # Every watcher reconciles at startup at the same moment. An unscoped sweep
    # would let the first one close sessions the others still hold.
    store = await Store(paths.DB_PATH).open()
    try:
        mine = await store.start_session("chan", "alice")
        theirs = await store.start_session("chan", "bob")

        closed = await store.reconcile_open_sessions("alice")
        assert closed == 1

        still_open = {r["id"] for r in await store.open_sessions()}
        assert theirs in still_open
        assert mine not in still_open
    finally:
        await store.close()


async def test_transitions_and_samples_carry_the_account(home: Any) -> None:
    store = await Store(paths.DB_PATH).open()
    try:
        await store.record_transition(Transition(
            ts=1.0, channel="chan", from_state="IDLE", to_state="WATCHING",
            reason="live", signals={}, account="alice",
        ))
        await store.record_sample(
            session_id=None, channel="chan", minutes_sent=1, credited=1,
            required=60, state="WATCHING", account="alice",
        )
        assert (await store.recent_transitions(5))[0].account == "alice"
        assert (await store.samples())[0]["account"] == "alice"
    finally:
        await store.close()


async def test_upgrading_an_existing_database_backfills_old_rows(home: Any) -> None:
    """A v2 database must keep its history, tagged as pre-accounts."""
    import sqlite3

    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths.DB_PATH)
    conn.executescript(
        "CREATE TABLE watch_session (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " channel TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL,"
        " minutes INTEGER NOT NULL DEFAULT 0, end_reason TEXT);"
        "INSERT INTO watch_session(channel, started_at, ended_at, minutes)"
        " VALUES('chan', 1, 2, 7);"
        "PRAGMA user_version=2;"
    )
    conn.commit()
    conn.close()

    store = await Store(paths.DB_PATH).open()
    try:
        totals = {r["account"]: r["minutes"] for r in await store.account_totals(days=36500)}
        assert totals == {LEGACY_ACCOUNT: 7}, "old minutes must survive the migration"
    finally:
        await store.close()


async def test_opening_an_already_migrated_database_is_a_no_op(home: Any) -> None:
    # SQLite has no ALTER TABLE ... ADD COLUMN IF NOT EXISTS, so a second open
    # would abort on "duplicate column name" if the step were not guarded.
    store = await Store(paths.DB_PATH).open()
    await store.close()
    again = await Store(paths.DB_PATH).open()
    await again.close()


# ------------------------------------------------------------------ event bus


async def test_scoped_bus_stamps_every_event_with_its_account() -> None:
    bus = EventBus()
    seen: list[Any] = []
    bus.subscribe(lambda e: seen.append(e))

    await bus.scoped("alice").publish(EventType.WATCH_STARTED, channel="chan")
    await bus.scoped("bob").publish(EventType.WATCH_STARTED, channel="chan")

    assert [e.get("account") for e in seen] == ["alice", "bob"]
    # Subscribers still see one combined stream — the dashboard needs all of it.
    assert len(seen) == 2


async def test_an_explicit_account_beats_the_scope() -> None:
    bus = EventBus()
    seen: list[Any] = []
    bus.subscribe(lambda e: seen.append(e))
    await bus.scoped("alice").publish(EventType.ERROR, account="bob", where="x")
    assert seen[0].get("account") == "bob"


async def test_scoped_bus_still_delegates_subscription_and_history() -> None:
    bus = EventBus()
    scoped = bus.scoped("alice")
    got: list[Any] = []
    unsubscribe = scoped.subscribe(lambda e: got.append(e), EventType.WATCH_STOPPED)

    await scoped.publish(EventType.WATCH_STOPPED, reason="test")
    assert len(got) == 1
    assert len(scoped.history()) == 1

    unsubscribe()
    await scoped.publish(EventType.WATCH_STOPPED, reason="again")
    assert len(got) == 1


# ------------------------------------------------- battle.net account linking


def _campaign(connected: bool | None, url: str | None = None) -> Any:
    from dropwatch.twitch.drops import Campaign

    return Campaign(
        id="c", name="n", status="ACTIVE",
        account_connected=connected, account_link_url=url,
    )


def test_a_confirmed_connection_reads_as_linked() -> None:
    from dropwatch.twitch.drops import DropsClient

    linked, url = DropsClient.link_status([_campaign(True)])
    assert linked is True
    assert url is None


def test_a_missing_connection_is_reported_with_the_fix_url() -> None:
    # The failure this exists for: telemetry accepted, state WATCHING, and zero
    # minutes credited forever, with nothing anywhere saying why.
    from dropwatch.twitch.drops import DropsClient

    linked, url = DropsClient.link_status(
        [_campaign(False, "https://account.battle.net/connections")]
    )
    assert linked is False
    assert url == "https://account.battle.net/connections"


def test_no_campaigns_means_unknown_not_unlinked() -> None:
    # An unlinked account and an account between campaigns look identical from
    # here. Guessing "unlinked" would put a red banner in front of someone whose
    # setup is fine, which trains people to ignore the banner.
    from dropwatch.twitch.drops import DropsClient

    assert DropsClient.link_status([]) == (None, None)
    assert DropsClient.link_status([_campaign(None)]) == (None, None)


def test_one_unlinked_campaign_outweighs_a_linked_one() -> None:
    # Campaigns can require different publishers' accounts. Anything missing is
    # worth surfacing, because that campaign really will earn nothing.
    from dropwatch.twitch.drops import DropsClient

    linked, url = DropsClient.link_status(
        [_campaign(True), _campaign(False, "https://example.invalid/link")]
    )
    assert linked is False
    assert url == "https://example.invalid/link"


def test_the_inventory_query_asks_for_the_connection_fields() -> None:
    """Without these fields in the document, link_status is silently always None."""
    from dropwatch.twitch.gql import OPERATIONS

    document = OPERATIONS["Inventory"].document or ""
    assert "isAccountConnected" in document
    assert "accountLinkURL" in document
