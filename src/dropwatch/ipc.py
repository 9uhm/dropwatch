"""Talking to the copy of dropwatch that is already running.

The app is meant to be launched by double-clicking an exe, which means the second
double-click has to find the first process and raise its window rather than
starting a rival watcher — two of them reporting the same account to Twitch is
worse than useless.

The dashboard's HTTP server *is* the lock. It binds a fixed localhost port, so
whoever holds that port is the live instance, and it already exposes the control
endpoint needed to drive the window. No named mutex, no lock file, and — unlike a
lock file — nothing left behind to go stale when a process is killed.

Everything here is deliberately synchronous stdlib: it runs before the event loop
exists, and pulling aiohttp in just to ask one question would mean paying its
import cost on a launch that is about to exit anyway.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .log import get_logger

log = get_logger("ipc")

#: Short by design. This runs on the launch path, in front of a user who just
#: double-clicked something, and a dead port must not cost them five seconds.
TIMEOUT = 2.0


def base_url(host: str, port: int) -> str:
    # A server bound to 0.0.0.0 is reachable at 127.0.0.1, and that is the address
    # to *ask* on — connecting to 0.0.0.0 is not portable.
    return f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"


def probe(host: str, port: int, *, timeout: float = TIMEOUT) -> dict[str, Any] | None:
    """Return the running instance's identity, or ``None`` if there isn't one.

    A successful connection is not enough to conclude anything: any process could
    hold that port. Only a well-formed answer from ``/api/ping`` proves it is us.
    """
    try:
        with urllib.request.urlopen(  # noqa: S310 — fixed http scheme, localhost
            f"{base_url(host, port)}/api/ping", timeout=timeout
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("app") != "dropwatch":
        log.debug("something else is listening on %s:%s", host, port)
        return None
    return payload


def control(host: str, port: int, action: str, *, timeout: float = TIMEOUT, **extra: Any) -> bool:
    """Send one control action to the running instance. Never raises."""
    body = json.dumps({"action": action, **extra}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 — fixed http scheme, localhost
        f"{base_url(host, port)}/api/control",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            # The server's CSRF guard. Same header its own page sends.
            "X-Dropwatch": "1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as exc:
        log.debug("control %s failed: %s", action, exc)
        return False


def show_running_instance(host: str, port: int) -> dict[str, Any] | None:
    """If another copy is live, raise its window and describe it. Else ``None``.

    Returning the ping payload rather than a bare bool lets the caller say
    something useful — including the case where the live instance is a headless
    ``serve`` with no window to raise, where "already running" is the whole story.
    """
    running = probe(host, port)
    if running is None:
        return None
    if running.get("windowed"):
        control(host, port, "show")
    return running
