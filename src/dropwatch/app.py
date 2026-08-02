"""Application container.

Owns construction and teardown order for the shared singletons so that the CLI,
the desktop app and (from phase 5) the Discord bot get an identically wired app.

**One :class:`Account` per Twitch login, all farming at once.** Everything that
carries an identity is per-account — the HTTP session, the token, the telemetry
stream, the PubSub connection, the watcher — because Twitch credits watch time to
whoever sent it, and sharing any of those between accounts would credit the wrong
one. Everything that does not carry an identity is shared: the config, the
database, the event bus.

Accounts pick targets independently. Two of them usually land on the same stream,
which is correct: drops accrue per account, so watching the same channel twice
earns twice.
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Self

import aiohttp

from . import __version__, paths
from . import accounts as accounts_mod
from .config import AppConfig, ConfigManager, Secrets, load_secrets
from .events import AccountBus, EventBus
from .log import get_logger, setup_logging
from .store import Store
from .twitch.auth import AuthManager, TokenStore
from .twitch.channels import ChannelClient
from .twitch.drops import DropsClient
from .twitch.gql import GQLClient
from .twitch.pubsub import PubSubClient
from .twitch.spade import SpadeClient
from .watcher import Watcher

if TYPE_CHECKING:
    from .accounts import Registry

log = get_logger("app")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class Account:
    """One Twitch login and everything that reports as it.

    The HTTP session is deliberately not shared with other accounts. Sessions
    carry cookies, and one cookie jar spanning two logins is a bug waiting to
    surprise someone — the whole point of this class is that nothing identifying
    crosses between accounts.
    """

    def __init__(
        self,
        name: str,
        *,
        config_manager: ConfigManager,
        bus: EventBus,
        store: Store,
    ) -> None:
        #: Empty until claimed — a fresh install has an account object with no
        #: login yet, so that the device flow has somewhere to run.
        self.name = name
        self._config_manager = config_manager
        self._root_bus = bus
        self.bus: AccountBus = bus.scoped(name)
        self._store = store

        self.session: aiohttp.ClientSession
        self.auth: AuthManager
        self.gql: GQLClient
        self.channels: ChannelClient
        self.spade: SpadeClient
        self.drops: DropsClient
        self.pubsub: PubSubClient
        self.watcher: Watcher

    @property
    def config(self) -> AppConfig:
        return self._config_manager.current

    @property
    def claimed(self) -> bool:
        return bool(self.name)

    @property
    def login(self) -> str | None:
        tokens = getattr(self, "auth", None) and self.auth.tokens
        return tokens.login if tokens else None

    @property
    def authenticated(self) -> bool:
        return bool(getattr(self, "auth", None) and self.auth.authenticated)

    @property
    def ready(self) -> bool:
        """Authenticated *and* able to report — a token with no user id cannot."""
        return self.authenticated and bool(self.auth.tokens and self.auth.tokens.user_id)

    async def start(self) -> Self:
        cfg = self._config_manager.current

        self.session = aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT, "Client-ID": cfg.twitch.client_id},
            timeout=aiohttp.ClientTimeout(total=30, connect=10),
            raise_for_status=False,
        )
        token_store = TokenStore(paths.token_path(self.name) if self.name else None)
        self.auth = AuthManager(self.session, cfg.twitch, self.bus, token_store)
        self.auth.load_from_disk()

        self.gql = GQLClient(self.session, self.auth, cfg.twitch, self.bus)
        self.channels = ChannelClient(self.gql, cfg.watch)
        self.spade = SpadeClient(self.session, cfg.watch, self.bus)
        self.drops = DropsClient(self.gql)
        self.pubsub = PubSubClient(self.session)
        self.watcher = Watcher(
            config_manager=self._config_manager,
            bus=self.bus,
            store=self._store,
            auth=self.auth,
            channels=self.channels,
            spade=self.spade,
            drops=self.drops,
            pubsub=self.pubsub,
            account=self.name,
        )
        return self

    def claim(self, name: str) -> None:
        """Give an unclaimed account its identity, once the login is known.

        The token file was already written under this name by ``TokenStore.save``;
        this catches the in-memory objects up so events and history rows stop
        being attributed to the empty string.
        """
        if self.name == name:
            return
        self.name = name
        self.bus = self._root_bus.scoped(name)
        self.watcher.rename(name, self.bus)
        log.info("account claimed as %r", name)

    async def stop(self) -> None:
        # PubSub owns a background task holding the session open; it has to go
        # first or closing the session cancels it mid-frame and logs noise.
        if getattr(self, "pubsub", None):
            await self.pubsub.stop()
        if getattr(self, "session", None) and not self.session.closed:
            await self.session.close()


class App:
    """Wires the shared services, then one :class:`Account` per enabled login."""

    def __init__(self) -> None:
        self.bus: EventBus
        self.store: Store
        self.config_manager: ConfigManager
        self.secrets: Secrets
        self.registry: Registry
        self.accounts: list[Account] = []

    @property
    def config(self) -> AppConfig:
        return self.config_manager.current

    # ------------------------------------------------------------- accounts

    @property
    def primary(self) -> Account:
        """The account single-account callers mean.

        Every CLI command that predates multi-account support says ``app.auth``
        and means "the one account". Keeping that working — rather than making
        each command account-aware — is why this exists; commands that genuinely
        need to choose take ``--account``.
        """
        return self.accounts[0]

    def get(self, name: str | None) -> Account | None:
        if not name:
            return self.accounts[0] if self.accounts else None
        for account in self.accounts:
            if account.name == name:
                return account
        return None

    def ready_accounts(self) -> list[Account]:
        return [a for a in self.accounts if a.ready]

    # Back-compat: single-account callers reach straight through to the primary.
    @property
    def auth(self) -> AuthManager:
        return self.primary.auth

    @property
    def gql(self) -> GQLClient:
        return self.primary.gql

    @property
    def channels(self) -> ChannelClient:
        return self.primary.channels

    @property
    def spade(self) -> SpadeClient:
        return self.primary.spade

    @property
    def drops(self) -> DropsClient:
        return self.primary.drops

    @property
    def pubsub(self) -> PubSubClient:
        return self.primary.pubsub

    @property
    def watcher(self) -> Watcher:
        return self.primary.watcher

    @property
    def session(self) -> aiohttp.ClientSession:
        return self.primary.session

    async def add_account(self, name: str) -> Account:
        account = await Account(
            name, config_manager=self.config_manager, bus=self.bus, store=self.store
        ).start()
        self.accounts.append(account)
        return account

    async def drop_account(self, name: str) -> bool:
        for i, account in enumerate(self.accounts):
            if account.name == name:
                await account.stop()
                del self.accounts[i]
                return True
        return False

    # ------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start(self) -> Self:
        # Before anything reads config or opens the database: frozen, the state
        # directory is under %LOCALAPPDATA% and may not exist yet.
        paths.ensure_root()
        self.secrets = load_secrets()

        # Store must exist before ConfigManager — overrides live in it.
        self.store = await Store().open()
        self.config_manager = await ConfigManager(self.store).load()

        cfg = self.config_manager.current
        setup_logging(cfg.logging.level)
        log.info("dropwatch v%s starting", __version__)

        self.bus = EventBus(history_size=cfg.logging.history_size)

        # Before reading the registry: an upgrade from a single-account install
        # has its login in the old tokens.json, and skipping this would present a
        # working install as having no accounts at all.
        accounts_mod.migrate_legacy_token()
        self.registry = await accounts_mod.Registry(self.store).load()

        for info in self.registry.enabled():
            await self.add_account(info.name)

        if not self.accounts:
            # An unclaimed account, so `login` and the dashboard's sign-in gate
            # have a session and an AuthManager to work with. It names itself
            # when the device flow returns a login.
            await self.add_account("")
            log.info("no accounts configured yet")
        else:
            log.info(
                "%d account(s): %s",
                len(self.accounts), ", ".join(a.name for a in self.accounts),
            )
        return self

    async def stop(self) -> None:
        for account in self.accounts:
            await account.stop()
        if getattr(self, "store", None):
            await self.store.close()
        log.debug("app stopped")
