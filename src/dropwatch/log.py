"""Logging setup.

One rule enforced here: tokens never reach the log. ``RedactingFilter`` scrubs
registered secrets from every record, so an accidental ``log.debug(payload)``
can't leak an access token into a file someone later pastes into Discord.
"""

from __future__ import annotations

import logging
import sys
from typing import ClassVar

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
