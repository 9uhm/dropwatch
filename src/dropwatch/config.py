"""Configuration.

Three layers, lowest priority first:

1. model defaults (this file)
2. ``config.toml``
3. runtime overrides stored in SQLite, set via Discord ``/config set``

Secrets are deliberately separate: they come from the environment / ``.env`` only
and live on :class:`Secrets`, never on :class:`AppConfig`. That split means the
runtime config can be freely dumped to Discord by ``/config show`` with no risk of
printing a token.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Self

from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from . import paths
from .log import RedactingFilter, get_logger

if TYPE_CHECKING:
    from .store import Store

log = get_logger("config")


class ConfigError(Exception):
    """Raised for an invalid override key or value."""


# --------------------------------------------------------------------------- models


class ChannelTarget(BaseModel):
    login: str
    priority: int = 100

    @field_validator("login")
    @classmethod
    def _normalise(cls, v: str) -> str:
        return v.strip().lower().removeprefix("https://twitch.tv/").removeprefix("twitch.tv/")


class WatchConfig(BaseModel):
    channels: list[ChannelTarget] = Field(default_factory=list)
    auto_discovery: bool = True
    game: str = "Overwatch 2"
    #: Twitch's URL slug for the category — what the directory query filters on.
    #: Distinct from ``game`` because the display name won't resolve a directory.
    game_slug: str = "overwatch-2"
    minute_watched_interval: float = Field(58.0, gt=0, le=300)
    minute_watched_jitter: float = Field(3.0, ge=0, le=30)
    progress_check_every: int = Field(3, ge=1, le=60)
    idle_poll_interval: float = Field(300.0, gt=0)
    auto_claim: bool = True

    @field_validator("channels", mode="before")
    @classmethod
    def _coerce(cls, v: Any) -> Any:
        """Allow a bare string in place of a table: ``channels = ["foo"]``."""
        if isinstance(v, list):
            return [{"login": item} if isinstance(item, str) else item for item in v]
        return v

    def ordered(self) -> list[ChannelTarget]:
        return sorted(self.channels, key=lambda c: (c.priority, c.login))


class LivenessConfig(BaseModel):
    grace_period: float = Field(90.0, ge=0, le=600)
    stream_poll_interval: float = Field(60.0, gt=0, le=600)
    stall_cycles: int = Field(3, ge=1, le=20)
    confirm_reads: int = Field(2, ge=1, le=10)


class TwitchConfig(BaseModel):
    #: Twitch's public smart-TV client, the only one that offers the device flow.
    client_id: str = "ue6666qo983tsx6so1t0vnawi233wa"
    #: The *web* client, whose persisted GraphQL query hashes we send. Deliberately
    #: separate from ``client_id``: the hashes belong to the web player, so GQL
    #: calls must identify as it even though the token was issued to the TV client.
    gql_client_id: str = "kimne78kx3ncx6brgo4mv6wki5h1ko"
    scopes: list[str] = Field(default_factory=lambda: ["user:read:follows"])
    refresh_at_lifetime_fraction: float = Field(0.8, gt=0, lt=1)
    device_poll_timeout: float = Field(900.0, gt=0)

    #: Send our own verified query documents rather than Twitch's persisted-query
    #: hashes. Documents don't rotate, so this is the robust default; set it false
    #: to mimic the real web client more closely, at the cost of breaking whenever
    #: Twitch redeploys and rotates a hash.
    prefer_documents: bool = True

    gql_max_concurrency: int = Field(4, ge=1, le=32)
    gql_max_retries: int = Field(3, ge=0, le=10)
    gql_backoff_base: float = Field(0.5, gt=0, le=30)
    gql_backoff_max: float = Field(20.0, gt=0, le=300)


class DiscordConfig(BaseModel):
    owner_id: int = 0
    guild_id: int = 0
    notify_channel_id: int = 0
    ping_role_id: int = 0
    status_edit_throttle: float = Field(60.0, ge=5)

    @property
    def configured(self) -> bool:
        return bool(self.owner_id)


class UIConfig(BaseModel):
    """Dashboard, browser and tray behaviour.

    These are persisted so ``serve`` behaves the same whether it's launched from a
    shell, a shortcut, or the tray — a CLI flag that has to be remembered every
    time isn't a setting, it's a chore.
    """

    #: Bind address. Left at localhost by default: the dashboard has no
    #: authentication, so exposing it to a network is an explicit decision.
    host: str = "127.0.0.1"
    port: int = Field(8787, ge=1, le=65535)

    #: Open the dashboard in a browser when the watcher starts.
    open_dashboard: bool = False
    #: Open the watched channel's Twitch page when a target is picked.
    open_twitch: bool = False
    #: Re-open Twitch on every rotation, not just the first target. Off by
    #: default — an unattended overnight run would otherwise bury you in tabs.
    reopen_twitch_on_rotate: bool = False
    #: Show a system-tray icon. This is what makes a detached run controllable.
    tray: bool = True


class LoggingConfig(BaseModel):
    level: str = "INFO"
    history_size: int = Field(200, ge=10, le=5000)


class AppConfig(BaseModel):
    """Non-secret runtime configuration. Safe to render anywhere."""

    watch: WatchConfig = Field(default_factory=WatchConfig)
    liveness: LivenessConfig = Field(default_factory=LivenessConfig)
    twitch: TwitchConfig = Field(default_factory=TwitchConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


class Secrets(BaseSettings):
    """Environment-only values."""

    model_config = SettingsConfigDict(
        env_file=str(paths.ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    discord_token: SecretStr | None = None

    @field_validator("discord_token", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: Any) -> Any:
        """``DISCORD_TOKEN=`` in .env means "not configured yet", not an empty token."""
        if isinstance(v, str) and not v.strip():
            return None
        return v


class _FileSettings(BaseSettings):
    """Loads ``config.toml`` into the :class:`AppConfig` shape.

    Kept separate from :class:`AppConfig` so that override merging can use plain
    ``model_validate`` without re-triggering settings-source resolution.
    """

    model_config = SettingsConfigDict(
        toml_file=str(paths.CONFIG_FILE),
        extra="ignore",
    )

    watch: WatchConfig = Field(default_factory=WatchConfig)
    liveness: LivenessConfig = Field(default_factory=LivenessConfig)
    twitch: TwitchConfig = Field(default_factory=TwitchConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, TomlConfigSettingsSource(settings_cls))

    def to_app_config(self) -> AppConfig:
        return AppConfig.model_validate(self.model_dump())


# ---------------------------------------------------------------------- management


def _set_path(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cursor: Any = data
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            raise ConfigError(f"unknown config key: {dotted!r}")
        cursor = cursor[part]
    if not isinstance(cursor, dict) or parts[-1] not in cursor:
        raise ConfigError(f"unknown config key: {dotted!r}")
    cursor[parts[-1]] = value


def parse_override_value(raw: str) -> Any:
    """Interpret a value typed into a Discord command.

    JSON first (so ``true``, ``42``, ``["a","b"]`` do the obvious thing), falling
    back to the literal string for bare words like ``INFO``.
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


class ConfigManager:
    """Holds the effective config and applies validated runtime overrides."""

    def __init__(self, store: Store | None = None) -> None:
        self._store = store
        self._base: AppConfig = AppConfig()
        self._overrides: dict[str, Any] = {}
        self._effective: AppConfig = AppConfig()

    @property
    def current(self) -> AppConfig:
        return self._effective

    @property
    def overrides(self) -> dict[str, Any]:
        return dict(self._overrides)

    async def load(self) -> Self:
        self._base = self._load_file()
        if self._store is not None:
            self._overrides = await self._store.get_overrides()
        self._rebuild()
        return self

    def _load_file(self) -> AppConfig:
        if not paths.CONFIG_FILE.exists():
            log.warning("%s not found — using defaults", paths.CONFIG_FILE.name)
            return AppConfig()
        try:
            return _FileSettings().to_app_config()
        except ValidationError as exc:
            raise ConfigError(f"invalid {paths.CONFIG_FILE.name}:\n{exc}") from exc

    def _rebuild(self) -> None:
        data = self._base.model_dump()
        for key, value in self._overrides.items():
            try:
                _set_path(data, key, value)
            except ConfigError:
                log.warning("dropping stale override %r", key)
        try:
            self._effective = AppConfig.model_validate(data)
        except ValidationError as exc:
            raise ConfigError(f"overrides produce invalid config:\n{exc}") from exc

    async def set_override(self, key: str, value: Any) -> AppConfig:
        """Validate then persist an override. Rolls back on failure.

        Unknown keys raise here, unlike in :meth:`_rebuild` — a typo typed into
        ``/config set`` must be reported, whereas a stale key already in the
        database (left by a renamed field) should only warn.
        """
        _set_path(self._base.model_dump(), key, value)  # raises on unknown key

        previous = dict(self._overrides)
        self._overrides[key] = value
        try:
            self._rebuild()
        except ConfigError:
            self._overrides = previous
            self._rebuild()
            raise
        if self._store is not None:
            await self._store.set_override(key, value)
        log.info("config override %s = %r", key, value)
        return self._effective

    async def clear_override(self, key: str) -> AppConfig:
        self._overrides.pop(key, None)
        self._rebuild()
        if self._store is not None:
            await self._store.clear_override(key)
        return self._effective

    async def reload(self) -> AppConfig:
        """Re-read ``config.toml`` from disk, keeping runtime overrides."""
        await self.load()
        return self._effective

    def flat(self) -> dict[str, Any]:
        """Effective config as dotted key -> value, for ``/config show``."""
        out: dict[str, Any] = {}

        def walk(prefix: str, value: Any) -> None:
            if isinstance(value, dict):
                for k, v in value.items():
                    walk(f"{prefix}.{k}" if prefix else k, v)
            else:
                out[prefix] = value

        walk("", self._effective.model_dump())
        return out


def load_secrets() -> Secrets:
    secrets = Secrets()
    if secrets.discord_token:
        RedactingFilter.register(secrets.discord_token.get_secret_value())
    return secrets
