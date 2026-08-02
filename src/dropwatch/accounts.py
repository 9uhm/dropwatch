"""Which Twitch accounts exist, and which of them should be farming.

Two sources on purpose, because they answer different questions:

* **Identity comes from disk.** An account exists if ``data/accounts/<login>.json``
  exists. Deleting that file removes the account, with no second registry to fall
  out of sync with it, and no way to end up listing an account whose token is
  gone.
* **Preference comes from the database.** Whether an account is currently enabled
  is a setting, so it lives with the other settings and survives a re-login that
  rewrites the token file.

Accounts farm independently: each has its own token, its own telemetry stream and
its own watcher, and each picks whichever channel is best for it — so two accounts
usually end up on the same stream, which is fine. Drops accrue per account, not
per stream.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import paths
from .log import get_logger
from .twitch.auth import TokenStore

if TYPE_CHECKING:
    from .store import Store

log = get_logger("accounts")

#: Name given to a migrated legacy token whose login could not be determined.
FALLBACK_NAME = "default"


@dataclass(slots=True)
class AccountInfo:
    """One known account, as the registry sees it — no live state."""

    name: str
    enabled: bool = True
    #: False when the token file is unreadable or has no usable login. Kept
    #: listed rather than hidden, so a broken account is something you can see
    #: and fix rather than an account that silently stopped existing.
    usable: bool = True
    login: str | None = None
    user_id: str | None = None

    @property
    def path(self) -> object:
        return paths.token_path(self.name)


def migrate_legacy_token() -> str | None:
    """Move a single-account ``tokens.json`` into ``accounts/``. Returns its name.

    Runs before the registry is read, so an upgrade keeps farming the account it
    was already farming instead of starting up with nothing configured. The old
    file is renamed rather than deleted: if anything here is wrong, the tokens are
    still on disk and a re-login is not forced.
    """
    legacy = paths.TOKEN_PATH
    if not legacy.is_file():
        return None
    if any(paths.ACCOUNTS_DIR.glob("*.json")):
        # Already migrated on a previous run and the old file was left behind.
        return None

    tokens = TokenStore(legacy).load()
    if tokens is None:
        log.warning("%s is unreadable; leaving it alone", legacy.name)
        return None

    name = (tokens.login or FALLBACK_NAME).lower()
    if not paths.is_valid_account_name(name):
        name = FALLBACK_NAME

    paths.ensure_accounts_dir()
    TokenStore(paths.token_path(name)).save(tokens)
    try:
        legacy.rename(legacy.with_suffix(".json.migrated"))
    except OSError as exc:
        log.debug("could not rename the legacy token file: %s", exc)
    log.info("migrated the existing login into the account %r", name)
    return name


def discover() -> list[str]:
    """Every account with a token file, in a stable order."""
    if not paths.ACCOUNTS_DIR.is_dir():
        return []
    names = [
        p.stem for p in paths.ACCOUNTS_DIR.glob("*.json")
        if paths.is_valid_account_name(p.stem)
    ]
    return sorted(names)


class Registry:
    """The account list, and the enabled flags that go with it."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._disabled: set[str] = set()

    async def load(self) -> Registry:
        self._disabled = await self._store.disabled_accounts()
        return self

    def list(self) -> list[AccountInfo]:
        """Every known account. Reads each token file to report its identity."""
        out: list[AccountInfo] = []
        for name in discover():
            tokens = TokenStore(paths.token_path(name)).load()
            out.append(AccountInfo(
                name=name,
                enabled=name not in self._disabled,
                usable=tokens is not None and bool(tokens.user_id),
                login=tokens.login if tokens else None,
                user_id=tokens.user_id if tokens else None,
            ))
        return out

    def enabled(self) -> list[AccountInfo]:
        """Accounts that should actually be farming right now."""
        return [a for a in self.list() if a.enabled and a.usable]

    def names(self) -> list[str]:
        return [a.name for a in self.list()]

    def disabled_names(self) -> set[str]:
        return set(self._disabled)

    async def set_enabled(self, name: str, enabled: bool) -> None:
        if enabled:
            self._disabled.discard(name)
        else:
            self._disabled.add(name)
        await self._store.set_account_enabled(name, enabled)
        log.info("account %r %s", name, "enabled" if enabled else "disabled")

    async def remove(self, name: str) -> bool:
        """Forget an account: delete its token file and its enabled flag.

        Deliberately does *not* revoke at Twitch — that is ``logout``'s job, and
        conflating them would mean removing an account from this machine silently
        invalidated it everywhere else it was signed in.
        """
        path = paths.token_path(name)
        existed = path.is_file()
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.error("could not remove %s: %s", path, exc)
            return False
        self._disabled.discard(name)
        await self._store.forget_account(name)
        if existed:
            log.info("removed account %r", name)
        return existed

    @staticmethod
    def claim_name(login: str) -> str:
        """The account name a freshly authorised login should be stored under."""
        name = (login or "").strip().lower()
        return name if paths.is_valid_account_name(name) else FALLBACK_NAME

    @staticmethod
    def touch(name: str) -> None:
        """Record that an account was seen. Cheap enough to call on every start."""
        log.debug("account %r active at %.0f", name, time.time())
