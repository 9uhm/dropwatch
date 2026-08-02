"""Logging setup.

One rule enforced here: tokens never reach the log. ``RedactingFilter`` scrubs
registered secrets from every record, so an accidental ``log.debug(payload)``
can't leak an access token into a file someone later pastes into Discord.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, ClassVar

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"
_DATEFMT = "%H:%M:%S"


class RedactingFilter(logging.Filter):
    """Replaces known secret values with ``***`` anywhere in the formatted record."""

    _secrets: ClassVar[set[str]] = set()

    @classmethod
    def register(cls, secret: str | None) -> None:
        # Short strings would match far too much; only redact plausible tokens.
        if secret and len(secret) >= 8:
            cls._secrets.add(secret)

    @classmethod
    def forget(cls, secret: str | None) -> None:
        cls._secrets.discard(secret or "")

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        msg = record.getMessage()
        redacted = msg
        for secret in self._secrets:
            redacted = redacted.replace(secret, "***")
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # These are chatty and we have our own reconnect logging.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"dropwatch.{name}")


# ------------------------------------------------------------------- console


def attach_parent_console() -> bool:
    """Borrow the launching terminal's console, if there is one. Windows only.

    The packaged app is built for the *windowed* subsystem so that double-clicking
    it never creates a console window — see the note in ``dropwatch.spec``. That
    also means a frozen build starts with no stdout at all, which would make
    ``dropwatch.exe doctor`` typed into a terminal print nothing and look broken.

    ``AttachConsole(ATTACH_PARENT_PROCESS)`` reattaches to the console that
    launched us. It fails when there isn't one — launched from Explorer, the tray,
    or a shortcut — and that failure is the desired outcome, not an error: it is
    exactly the case where output should go nowhere.

    Returns whether a console was attached. Safe to call more than once, and on
    any platform.
    """
    if sys.platform != "win32":
        return False
    # A build that already has working streams — running from source, under a
    # test harness, or with an inherited console — must not have them swapped
    # out from under it. Probed by writing rather than by inspecting, because
    # pytest's capture and PyInstaller's stubs each fail a different check.
    if sys.stdout is not None:
        try:
            sys.stdout.write("")
            sys.stdout.flush()
            return False
        except Exception:  # noqa: BLE001 — a dead stream is what we're here for
            pass

    import ctypes

    ATTACH_PARENT_PROCESS = -1
    try:
        attached = bool(ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS))
    except Exception:  # noqa: BLE001 — no console is a normal outcome
        return False
    if not attached:
        return False

    # Two ways back to the caller's output, and the order matters.
    #
    # A *handle* the parent passed us is the real destination — that is what
    # carries a redirect, so `dropwatch.exe status > out.txt` only works if we
    # write there. CONOUT$ addresses the console device directly and would put
    # the text on screen while leaving the file empty.
    #
    # So: adopt an inherited handle when there is one, and fall back to the
    # console device when there isn't (the plain, unredirected case).
    for attr, mode, std_id, device in (
        ("stdout", "w", -11, "CONOUT$"),
        ("stderr", "w", -12, "CONOUT$"),
        ("stdin", "r", -10, "CONIN$"),
    ):
        stream = _stream_from_handle(std_id, mode) or _stream_from_device(device, mode)
        if stream is not None:
            setattr(sys, attr, stream)
    return True


def _stream_from_handle(std_id: int, mode: str) -> Any:
    """Wrap an inherited standard handle, or None if there isn't a usable one."""
    import ctypes
    import msvcrt

    try:
        handle = ctypes.windll.kernel32.GetStdHandle(std_id)
    except Exception:  # noqa: BLE001
        return None
    # 0 is "not set"; -1 (INVALID_HANDLE_VALUE) is "no such handle".
    if not handle or handle == -1 or handle == 0xFFFFFFFFFFFFFFFF:
        return None
    try:
        flags = os.O_RDONLY if mode == "r" else os.O_WRONLY
        fd = msvcrt.open_osfhandle(handle, flags)
        return open(fd, mode, encoding="utf-8", errors="replace",
                    buffering=1, closefd=False)
    except (OSError, ValueError):
        return None


def _stream_from_device(name: str, mode: str) -> Any:
    """Open the console device itself — the unredirected case."""
    try:
        return open(name, mode, encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return None
