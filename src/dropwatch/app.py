"""Application container.

Owns construction and teardown order for the shared singletons so that both the
CLI and (from phase 5) the Discord bot get an identically wired app.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

import aiohttp

from . import __version__
from .config import AppConfig, ConfigManager, Secrets, load_secrets
from .events import EventBus
from .log import get_logger, setup_logging
from .store import Store
from .twitch.auth import AuthManager
from .twitch.channels import ChannelClient
from .twitch.drops import DropsClient
from .twitch.gql import GQLClient
from .twitch.pubsub import PubSubClient
from .twitch.spade import SpadeClient
from .watcher import Watcher

log = get_logger("app")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class App:
    """Wires the event bus, store, config, HTTP session and auth manager."""

    def __init__(self) -> None:
        self.bus: EventBus
        self.store: Store
        self.config_manager: ConfigManager
        self.secrets: Secrets
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
        return self.config_manager.current

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
        self.secrets = load_secrets()

        # Store must exist before ConfigManager — overrides live in it.
        self.store = await Store().open()
        self.config_manager = await ConfigManager(self.store).load()

        cfg = self.config_manager.current
        setup_logging(cfg.logging.level)
        log.info("dropwatch v%s starting", __version__)

        self.bus = EventBus(history_size=cfg.logging.history_size)

        self.session = aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT, "Client-ID": cfg.twitch.client_id},
            timeout=aiohttp.ClientTimeout(total=30, connect=10),
            raise_for_status=False,
        )
        self.auth = AuthManager(self.session, cfg.twitch, self.bus)
        self.auth.load_from_disk()

        self.gql = GQLClient(self.session, self.auth, cfg.twitch, self.bus)
        self.channels = ChannelClient(self.gql, cfg.watch)
        self.spade = SpadeClient(self.session, cfg.watch, self.bus)
        self.drops = DropsClient(self.gql)
        self.pubsub = PubSubClient(self.session)
        self.watcher = Watcher(
            config_manager=self.config_manager,
            bus=self.bus,
            store=self.store,
            auth=self.auth,
            channels=self.channels,
            spade=self.spade,
            drops=self.drops,
            pubsub=self.pubsub,
        )
        return self

    async def stop(self) -> None:
        # PubSub owns a background task holding the session open; it has to go
        # first or closing the session cancels it mid-frame and logs noise.
        if getattr(self, "pubsub", None):
            await self.pubsub.stop()
        if getattr(self, "session", None) and not self.session.closed:
            await self.session.close()
        if getattr(self, "store", None):
            await self.store.close()
        log.debug("app stopped")
