"""Twitch OAuth via the device code flow.

The device flow is what smart-TV and console apps use: we ask Twitch for a short
user code, the human enters it at twitch.tv/activate, and we poll until it's
approved. No password ever passes through this process, and there is no browser to
scrape.

Tokens land in ``data/tokens.json`` with permissions tightened to the current user
only. Refresh happens proactively at a configurable fraction of the token
lifetime, and reactively on a single 401 retry.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from pydantic import BaseModel, Field

from .. import paths
from ..config import TwitchConfig
from ..events import EventBus, EventType
from ..log import RedactingFilter, get_logger

log = get_logger("twitch.auth")

DEVICE_ENDPOINT = "https://id.twitch.tv/oauth2/device"
TOKEN_ENDPOINT = "https://id.twitch.tv/oauth2/token"
VALIDATE_ENDPOINT = "https://id.twitch.tv/oauth2/validate"
REVOKE_ENDPOINT = "https://id.twitch.tv/oauth2/revoke"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class AuthError(Exception):
    """Any auth failure."""


class AuthNeededError(AuthError):
    """No usable token on disk — the user must run the login flow."""


class DeviceFlowDenied(AuthError):
    """The user denied the authorisation request."""


class DeviceFlowTimeout(AuthError):
    """The user code expired before it was approved."""


# ------------------------------------------------------------------- token model


class TokenSet(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_at: float = 0.0
    obtained_at: float = Field(default_factory=time.time)
    scopes: list[str] = Field(default_factory=list)
    # Populated by validate(); needed later for telemetry payloads.
    user_id: str | None = None
    login: str | None = None

    @property
    def lifetime(self) -> float:
        return max(self.expires_at - self.obtained_at, 0.0)

    @property
    def seconds_remaining(self) -> float:
        return self.expires_at - time.time()

    @property
    def never_expires(self) -> bool:
        """True for the non-expiring tokens Twitch issues to TV/console clients.

        Twitch signals this with ``expires_in: 0``, which we store as
        ``expires_at == 0.0`` — distinct from "expires at the epoch".
        """
        return self.expires_at <= 0

    @property
    def expired(self) -> bool:
        return not self.never_expires and self.seconds_remaining <= 0

    def needs_refresh(self, at_fraction: float) -> bool:
        if self.expires_at <= 0 or self.lifetime <= 0:
            return False
        elapsed = time.time() - self.obtained_at
        return elapsed >= self.lifetime * at_fraction


@dataclass(slots=True)
class DeviceFlow:
    device_code: str
    user_code: str
    verification_uri: str
    interval: float
    expires_at: float

    @property
    def seconds_remaining(self) -> float:
        return max(self.expires_at - time.time(), 0.0)


# ------------------------------------------------------------------- token store


class TokenStore:
    """Reads and writes ``tokens.json``, tightening file permissions on write."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.TOKEN_PATH

    def load(self) -> TokenSet | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text("utf-8"))
            tokens = TokenSet.model_validate(data)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            log.warning("could not read %s (%s) — re-login required", self.path.name, exc)
            return None
        self._register_secrets(tokens)
        return tokens

    def save(self, tokens: TokenSet) -> None:
        paths.ensure_data_dir()
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(tokens.model_dump_json(indent=2), encoding="utf-8")
        self._harden(tmp)
        tmp.replace(self.path)  # atomic — never leaves a half-written token file
        self._register_secrets(tokens)
        log.debug("tokens saved to %s", self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    @staticmethod
    def _register_secrets(tokens: TokenSet) -> None:
        RedactingFilter.register(tokens.access_token)
        RedactingFilter.register(tokens.refresh_token)

    @staticmethod
    def _harden(path: Path) -> None:
        """Restrict the token file to the current user.

        POSIX mode bits are near-meaningless on Windows, so use ``icacls`` there:
        strip inheritance and grant only the current user full control.
        """
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if sys.platform != "win32":
            return
        user = os.environ.get("USERNAME")
        if not user:
            return
        try:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                check=True,
                capture_output=True,
                timeout=15,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("could not restrict permissions on %s: %s", path.name, exc)


# ----------------------------------------------------------------- auth manager


class AuthManager:
    """Owns the current token and guarantees callers get a valid one."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        config: TwitchConfig,
        bus: EventBus,
        store: TokenStore | None = None,
    ) -> None:
        self._session = session
        self._config = config
        self._bus = bus
        self._store = store or TokenStore()
        self._tokens: TokenSet | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- properties

    @property
    def tokens(self) -> TokenSet | None:
        return self._tokens

    @property
    def authenticated(self) -> bool:
        return self._tokens is not None and not self._tokens.expired

    def load_from_disk(self) -> TokenSet | None:
        self._tokens = self._store.load()
        return self._tokens

    # ------------------------------------------------------------ device flow

    async def start_device_flow(self) -> DeviceFlow:
        payload = {
            "client_id": self._config.client_id,
            "scopes": " ".join(self._config.scopes),
        }
        async with self._session.post(DEVICE_ENDPOINT, data=payload) as resp:
            body = await self._json(resp)
            if resp.status != 200:
                raise AuthError(f"device request failed ({resp.status}): {body}")

        expires_in = float(body.get("expires_in", 1800))
        flow = DeviceFlow(
            device_code=body["device_code"],
            user_code=body["user_code"],
            verification_uri=body.get("verification_uri") or "https://www.twitch.tv/activate",
            interval=float(body.get("interval", 5)),
            expires_at=time.time() + expires_in,
        )
        log.info("device flow started, code %s valid for %.0fs", flow.user_code, expires_in)
        return flow

    async def complete_device_flow(self, flow: DeviceFlow) -> TokenSet:
        """Poll until the user approves, then persist and validate the token."""
        deadline = min(flow.expires_at, time.time() + self._config.device_poll_timeout)
        interval = flow.interval

        while time.time() < deadline:
            await asyncio.sleep(interval)
            payload = {
                "client_id": self._config.client_id,
                "device_code": flow.device_code,
                "grant_type": DEVICE_GRANT,
            }
            async with self._session.post(TOKEN_ENDPOINT, data=payload) as resp:
                body = await self._json(resp)

            if resp.status == 200:
                tokens = self._tokens_from_response(body)
                await self._adopt(tokens)
                await self.validate()
                await self._bus.publish(
                    EventType.AUTH_REFRESHED,
                    login=self._tokens.login if self._tokens else None,
                    reason="device_flow",
                )
                return tokens

            error = str(body.get("message") or body.get("error") or "").lower()
            if "authorization_pending" in error or "pending" in error:
                continue
            if "slow_down" in error:
                interval += 5
                continue
            if "expired" in error:
                raise DeviceFlowTimeout("the user code expired — run login again")
            if "denied" in error or "access_denied" in error:
                raise DeviceFlowDenied("authorisation was denied")
            raise AuthError(f"device token exchange failed ({resp.status}): {body}")

        raise DeviceFlowTimeout("timed out waiting for authorisation")

    # ---------------------------------------------------------------- refresh

    async def ensure_valid(self) -> str:
        """Return a usable access token, refreshing if it's near expiry.

        The lock means a burst of concurrent callers triggers exactly one refresh.
        """
        async with self._lock:
            if self._tokens is None:
                self.load_from_disk()
            if self._tokens is None:
                await self._bus.publish(EventType.AUTH_NEEDED)
                raise AuthNeededError("no stored token — run `dropwatch login`")

            if self._tokens.expired or self._tokens.needs_refresh(
                self._config.refresh_at_lifetime_fraction
            ):
                await self._refresh_locked()
            return self._tokens.access_token

    async def refresh(self) -> TokenSet:
        async with self._lock:
            return await self._refresh_locked()

    async def _refresh_locked(self) -> TokenSet:
        tokens = self._tokens
        if tokens is None or not tokens.refresh_token:
            await self._bus.publish(
                EventType.AUTH_EXPIRED, reason="no refresh token available"
            )
            raise AuthNeededError("token cannot be refreshed — run `dropwatch login`")

        payload = {
            "client_id": self._config.client_id,
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
        }
        async with self._session.post(TOKEN_ENDPOINT, data=payload) as resp:
            body = await self._json(resp)

        if resp.status != 200:
            # 400/401 here means the refresh token is revoked — not retryable.
            self._store.clear()
            self._tokens = None
            await self._bus.publish(
                EventType.AUTH_EXPIRED, reason=f"refresh rejected ({resp.status})"
            )
            raise AuthNeededError(f"refresh failed ({resp.status}): {body}")

        refreshed = self._tokens_from_response(body)
        # Twitch may omit a new refresh token; keep the existing one if so.
        refreshed.refresh_token = refreshed.refresh_token or tokens.refresh_token
        refreshed.user_id = tokens.user_id
        refreshed.login = tokens.login
        await self._adopt(refreshed)
        log.info("access token refreshed, valid %.0fs", refreshed.seconds_remaining)
        await self._bus.publish(EventType.AUTH_REFRESHED, reason="scheduled")
        return refreshed

    async def validate(self) -> dict[str, Any]:
        """Hit ``/oauth2/validate`` to confirm the token and learn the user id."""
        if self._tokens is None:
            raise AuthNeededError("no token to validate")
        headers = {"Authorization": f"OAuth {self._tokens.access_token}"}
        async with self._session.get(VALIDATE_ENDPOINT, headers=headers) as resp:
            body = await self._json(resp)
            if resp.status != 200:
                raise AuthError(f"token validation failed ({resp.status}): {body}")

        self._tokens.user_id = str(body.get("user_id") or "") or None
        self._tokens.login = body.get("login")
        if body.get("scopes"):
            self._tokens.scopes = list(body["scopes"])
        # /validate reports the true remaining lifetime; trust it over our estimate.
        if body.get("expires_in"):
            self._tokens.obtained_at = time.time()
            self._tokens.expires_at = time.time() + float(body["expires_in"])
        self._store.save(self._tokens)
        log.info("authenticated as %s (id %s)", self._tokens.login, self._tokens.user_id)
        return body

    async def logout(self, *, revoke: bool = True) -> bool:
        """Remove stored tokens, and by default revoke them at Twitch.

        Deleting the local file alone is *not* an unlink. Twitch issues
        non-expiring tokens to TV clients, so a token that is only forgotten
        locally stays valid indefinitely — revoking is what actually severs it.

        Returns whether the remote revocation succeeded. A failure still clears
        local state: leaving a token on disk that the caller believes is gone
        would be the worse outcome.
        """
        async with self._lock:
            tokens = self._tokens
            revoked = False

            if revoke and tokens and tokens.access_token:
                try:
                    async with self._session.post(
                        REVOKE_ENDPOINT,
                        data={
                            "client_id": self._config.client_id,
                            "token": tokens.access_token,
                        },
                    ) as resp:
                        revoked = resp.status == 200
                        if not revoked:
                            body = (await resp.text())[:200]
                            log.warning(
                                "token revocation returned HTTP %s: %s", resp.status, body
                            )
                except (TimeoutError, aiohttp.ClientError) as exc:
                    log.warning("could not reach Twitch to revoke token: %s", exc)

            if tokens:
                RedactingFilter.forget(tokens.access_token)
                RedactingFilter.forget(tokens.refresh_token)
            self._store.clear()
            self._tokens = None
            log.info(
                "logged out, stored tokens removed%s",
                " and revoked at Twitch" if revoked else "",
            )
            return revoked

    # ----------------------------------------------------------------- helpers

    async def _adopt(self, tokens: TokenSet) -> None:
        self._tokens = tokens
        self._store.save(tokens)

    @staticmethod
    def _tokens_from_response(body: dict[str, Any]) -> TokenSet:
        now = time.time()
        expires_in = body.get("expires_in")
        return TokenSet(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            obtained_at=now,
            expires_at=now + float(expires_in) if expires_in else 0.0,
            scopes=body.get("scope") or [],
        )

    @staticmethod
    async def _json(resp: aiohttp.ClientResponse) -> dict[str, Any]:
        """Parse a response body as JSON, tolerating a non-JSON error page."""
        try:
            data = await resp.json(content_type=None)
        except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
            return {"error": (await resp.text())[:300]}
        return data if isinstance(data, dict) else {"data": data}
