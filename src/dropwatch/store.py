"""SQLite persistence.

Uses stdlib ``sqlite3`` on a worker thread rather than adding an async driver —
this workload is a handful of small writes per minute, so the thread hop costs
nothing and the dependency footprint stays smaller. A single connection is shared
under an ``asyncio.Lock``, which also serialises writes.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Self

from . import paths
from .log import get_logger

log = get_logger("store")

SCHEMA_VERSION = 3

#: Rows written before multi-account support existed belong to *an* account, but
#: not one the database recorded. They are backfilled with this rather than the
#: migrating install's login: guessing a name would silently attribute someone
#: else's history to a real account, and a distinct marker keeps the old totals
#: visible without claiming they came from anywhere in particular.
LEGACY_ACCOUNT = "(before accounts)"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config_override (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,          -- JSON-encoded
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS watch_session (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel    TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at   REAL,
    minutes    INTEGER NOT NULL DEFAULT 0,
    end_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_started ON watch_session(started_at DESC);

CREATE TABLE IF NOT EXISTS drop_claim (
    drop_id     TEXT PRIMARY KEY,
    campaign_id TEXT,
    name        TEXT NOT NULL,
    image_url   TEXT,
    claimed_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS state_transition (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    channel    TEXT,
    from_state TEXT NOT NULL,
    to_state   TEXT NOT NULL,
    reason     TEXT,
    signals    TEXT                    -- JSON: which signals voted, and how
);
CREATE INDEX IF NOT EXISTS idx_transition_ts ON state_transition(ts DESC);

-- One row per progress read. Session totals alone can't produce a curve, and
-- Twitch's credited count is the only number worth plotting, so it's sampled
-- rather than derived from our own cycle counter.
CREATE TABLE IF NOT EXISTS watch_sample (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    session_id INTEGER,
    channel    TEXT NOT NULL,
    minutes_sent INTEGER NOT NULL DEFAULT 0,   -- what we reported
    credited     INTEGER,                      -- what Twitch counted, NULL if unread
    required     INTEGER,
    state        TEXT
);
CREATE INDEX IF NOT EXISTS idx_sample_ts ON watch_sample(ts DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Only the *preference*, not the account list: an account exists because its
-- token file exists (see accounts.py). A row here for a name with no token file
-- is harmless and gets cleaned up when the account is removed.
CREATE TABLE IF NOT EXISTS account_pref (
    name       TEXT PRIMARY KEY,
    enabled    INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);
"""

#: Added in v3. Nullable and backfilled rather than NOT NULL, because ALTER TABLE
#: cannot add a NOT NULL column without a default and the backfill is what gives
#: the old rows meaning.
_ACCOUNT_COLUMNS = (
    ("watch_session", "account"),
    ("watch_sample", "account"),
    ("state_transition", "account"),
    ("drop_claim", "account"),
)


@dataclass(slots=True)
class Transition:
    ts: float
    channel: str | None
    from_state: str
    to_state: str
    reason: str | None
    signals: dict[str, Any]
    #: Which account this transition belongs to. Defaulted so existing callers
    #: and tests keep working; the watcher always sets it.
    account: str = ""


class Store:
    def __init__(self, path: Any = None) -> None:
        self._path = path or paths.DB_PATH
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle

    async def open(self) -> Self:
        paths.ensure_data_dir()
        await asyncio.to_thread(self._connect)
        return self

    def _connect(self) -> None:
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL keeps the reader (Discord status) from blocking on the writer.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()
        log.debug("store open at %s", self._path)

    def _migrate(self) -> None:
        assert self._conn is not None
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if current == SCHEMA_VERSION:
            return
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema v{current} is newer than this build (v{SCHEMA_VERSION})"
            )
        # The schema above is CREATE TABLE IF NOT EXISTS throughout, so an older
        # database gains new tables simply by having been opened. Steps here are
        # only needed for changes that alter or backfill existing rows.
        if current < 2:
            log.info("migrating database v%d -> v2 (watch samples)", current)
        if current < 3:
            log.info("migrating database v%d -> v3 (per-account history)", current)
            self._add_account_columns()

        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        log.info("database schema at v%d", SCHEMA_VERSION)

    def _add_account_columns(self) -> None:
        """Tag every historical row with an account.

        Idempotent by inspection rather than by ``IF NOT EXISTS``, which SQLite's
        ALTER TABLE does not support — a re-run on an already-migrated database
        would otherwise abort the whole open with "duplicate column name".
        """
        assert self._conn is not None
        for table, column in _ACCOUNT_COLUMNS:
            existing = {
                row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if column in existing:
                continue
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            self._conn.execute(
                f"UPDATE {table} SET {column}=? WHERE {column} IS NULL", (LEGACY_ACCOUNT,)
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_account ON watch_session(account)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sample_account ON watch_sample(account)"
        )

    async def close(self) -> None:
        if self._conn is None:
            return
        async with self._lock:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    # --------------------------------------------------------------- helpers

    async def _write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        async with self._lock:
            conn = self._require()

            def run() -> None:
                conn.execute(sql, params)
                conn.commit()

            await asyncio.to_thread(run)

    async def _read(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        async with self._lock:
            conn = self._require()
            return await asyncio.to_thread(lambda: conn.execute(sql, params).fetchall())

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("store is not open — call await store.open() first")
        return self._conn

    # -------------------------------------------------------- config overrides

    async def get_overrides(self) -> dict[str, Any]:
        rows = await self._read("SELECT key, value FROM config_override")
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                log.warning("corrupt override %r, ignoring", row["key"])
        return out

    async def set_override(self, key: str, value: Any) -> None:
        await self._write(
            "INSERT INTO config_override(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), time.time()),
        )

    async def clear_override(self, key: str) -> None:
        await self._write("DELETE FROM config_override WHERE key=?", (key,))

    # -------------------------------------------------------------- sessions

    async def start_session(self, channel: str, account: str = "") -> int:
        async with self._lock:
            conn = self._require()

            def run() -> int:
                cur = conn.execute(
                    "INSERT INTO watch_session(channel, started_at, account) VALUES(?,?,?)",
                    (channel, time.time(), account),
                )
                conn.commit()
                return int(cur.lastrowid or 0)

            return await asyncio.to_thread(run)

    async def end_session(self, session_id: int, minutes: int, reason: str) -> None:
        await self._write(
            "UPDATE watch_session SET ended_at=?, minutes=?, end_reason=? WHERE id=?",
            (time.time(), minutes, reason, session_id),
        )

    async def recent_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self._read(
            "SELECT * FROM watch_session ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]

    async def open_sessions(self, account: str | None = None) -> list[dict[str, Any]]:
        """Sessions left dangling by a crash — reconciled on startup."""
        if account is None:
            rows = await self._read("SELECT * FROM watch_session WHERE ended_at IS NULL")
        else:
            rows = await self._read(
                "SELECT * FROM watch_session WHERE ended_at IS NULL "
                "AND COALESCE(account,'')=?",
                (account,),
            )
        return [dict(r) for r in rows]

    async def reconcile_open_sessions(self, account: str | None = None) -> int:
        """Close sessions a crash or kill left open, and recover their minutes.

        A killed process never reaches ``end_session``, leaving a row reading
        "0 min, running" forever. That row then silently skews every statistic
        drawn from the table, so it's closed on the next startup using the last
        sample as the end time and minute count — the best evidence available.
        """
        # Scoped to one account: several watchers reconcile at the same moment on
        # startup, and an unscoped sweep would let the first one close sessions
        # the others are about to reopen — or, worse, ones they still hold.
        dangling = await self.open_sessions(account)
        if not dangling:
            return 0

        async with self._lock:
            conn = self._require()

            def run() -> None:
                for row in dangling:
                    sample = conn.execute(
                        "SELECT ts, minutes_sent FROM watch_sample "
                        "WHERE session_id=? ORDER BY ts DESC LIMIT 1",
                        (row["id"],),
                    ).fetchone()
                    ended = sample["ts"] if sample else row["started_at"]
                    minutes = (
                        sample["minutes_sent"] if sample else int(row["minutes"] or 0)
                    )
                    conn.execute(
                        "UPDATE watch_session SET ended_at=?, minutes=?, end_reason=? "
                        "WHERE id=?",
                        (ended, minutes, "interrupted (recovered)", row["id"]),
                    )
                conn.commit()

            await asyncio.to_thread(run)

        log.info("recovered %d interrupted session(s)", len(dangling))
        return len(dangling)

    # --------------------------------------------------------------- samples

    async def record_sample(
        self,
        *,
        session_id: int | None,
        channel: str,
        minutes_sent: int,
        credited: int | None,
        required: int | None,
        state: str | None,
        account: str = "",
    ) -> None:
        await self._write(
            "INSERT INTO watch_sample"
            "(ts, session_id, channel, minutes_sent, credited, required, state, account) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), session_id, channel, minutes_sent, credited, required,
             state, account),
        )

    async def samples(self, since: float | None = None, limit: int = 2000) -> list[dict[str, Any]]:
        if since is None:
            rows = await self._read(
                "SELECT * FROM watch_sample ORDER BY ts DESC LIMIT ?", (limit,)
            )
        else:
            rows = await self._read(
                "SELECT * FROM watch_sample WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (since, limit),
            )
        return [dict(r) for r in reversed(rows)]  # chronological for plotting

    async def daily_totals(self, days: int = 14) -> list[dict[str, Any]]:
        """Minutes reported per local day, from completed sessions."""
        cutoff = time.time() - days * 86400
        rows = await self._read(
            "SELECT date(started_at, 'unixepoch', 'localtime') AS day, "
            "       SUM(minutes) AS minutes, COUNT(*) AS sessions "
            "FROM watch_session WHERE started_at >= ? "
            "GROUP BY day ORDER BY day",
            (cutoff,),
        )
        return [dict(r) for r in rows]

    async def channel_totals(self, days: int = 30) -> list[dict[str, Any]]:
        cutoff = time.time() - days * 86400
        rows = await self._read(
            "SELECT channel, SUM(minutes) AS minutes, COUNT(*) AS sessions "
            "FROM watch_session WHERE started_at >= ? "
            "GROUP BY channel ORDER BY minutes DESC",
            (cutoff,),
        )
        return [dict(r) for r in rows]

    async def account_totals(self, days: int = 30) -> list[dict[str, Any]]:
        """Minutes reported per account. The headline number for a fleet."""
        cutoff = time.time() - days * 86400
        rows = await self._read(
            "SELECT COALESCE(account, '') AS account, SUM(minutes) AS minutes, "
            "       COUNT(*) AS sessions "
            "FROM watch_session WHERE started_at >= ? "
            "GROUP BY account ORDER BY minutes DESC",
            (cutoff,),
        )
        return [dict(r) for r in rows]

    # -------------------------------------------------- account preferences

    async def disabled_accounts(self) -> set[str]:
        rows = await self._read("SELECT name FROM account_pref WHERE enabled=0")
        return {r["name"] for r in rows}

    async def set_account_enabled(self, name: str, enabled: bool) -> None:
        await self._write(
            "INSERT INTO account_pref(name, enabled, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled, "
            "updated_at=excluded.updated_at",
            (name, 1 if enabled else 0, time.time()),
        )

    async def forget_account(self, name: str) -> None:
        """Drop the preference row. History rows are kept: the minutes were real."""
        await self._write("DELETE FROM account_pref WHERE name=?", (name,))

    async def transition_counts(self, days: int = 30) -> list[dict[str, Any]]:
        cutoff = time.time() - days * 86400
        rows = await self._read(
            "SELECT to_state, COUNT(*) AS n FROM state_transition "
            "WHERE ts >= ? GROUP BY to_state ORDER BY n DESC",
            (cutoff,),
        )
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- drops

    async def record_claim(
        self, drop_id: str, name: str, campaign_id: str | None, image_url: str | None
    ) -> bool:
        """Returns ``True`` if this is a new claim, ``False`` if already recorded."""
        async with self._lock:
            conn = self._require()

            def run() -> bool:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO drop_claim"
                    "(drop_id, campaign_id, name, image_url, claimed_at) VALUES(?,?,?,?,?)",
                    (drop_id, campaign_id, name, image_url, time.time()),
                )
                conn.commit()
                return cur.rowcount > 0

            return await asyncio.to_thread(run)

    async def is_claimed(self, drop_id: str) -> bool:
        rows = await self._read("SELECT 1 FROM drop_claim WHERE drop_id=?", (drop_id,))
        return bool(rows)

    async def claims(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = await self._read(
            "SELECT * FROM drop_claim ORDER BY claimed_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- transitions

    async def record_transition(self, t: Transition) -> None:
        await self._write(
            "INSERT INTO state_transition"
            "(ts, channel, from_state, to_state, reason, signals, account) "
            "VALUES(?,?,?,?,?,?,?)",
            (t.ts, t.channel, t.from_state, t.to_state, t.reason,
             json.dumps(t.signals), t.account),
        )

    async def recent_transitions(self, limit: int = 20) -> list[Transition]:
        rows = await self._read(
            "SELECT * FROM state_transition ORDER BY ts DESC LIMIT ?", (limit,)
        )
        out = []
        for r in rows:
            try:
                signals = json.loads(r["signals"] or "{}")
            except json.JSONDecodeError:
                signals = {}
            out.append(
                Transition(
                    ts=r["ts"],
                    channel=r["channel"],
                    from_state=r["from_state"],
                    to_state=r["to_state"],
                    reason=r["reason"],
                    signals=signals,
                    account=(r["account"] if "account" in r.keys() else "") or "",
                )
            )
        return out

    # ------------------------------------------------------------------ meta

    async def get_meta(self, key: str, default: str | None = None) -> str | None:
        rows = await self._read("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    async def set_meta(self, key: str, value: str) -> None:
        await self._write(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
