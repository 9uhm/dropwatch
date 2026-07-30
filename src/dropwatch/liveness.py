"""Multi-signal stream-end detection.

No single signal is trusted. Five independent observations feed a state machine
with hysteresis, and the reasons behind every transition are carried on the
verdict so ``/status`` and the logs can explain *why* the bot switched.

The behaviour here was specified and tested in ``ui/console.html`` before it was
written in Python, and the three rules that simulator exists to prove are:

* an **ad break** must never cause a rotation
* a **dead PubSub socket** must never read as an offline stream
* **stopped crediting** must land in ``STALLED``, not ``OFFLINE`` — the stream is
  genuinely live, so calling it offline would send someone debugging the wrong
  thing entirely

This module performs no I/O. It takes an :class:`Observation` and returns a
:class:`Verdict`, which keeps the whole state machine synchronously testable; the
watcher owns the network calls, including the active probe the detector asks for.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from .log import get_logger

if TYPE_CHECKING:
    from .config import LivenessConfig
    from .twitch.channels import StreamInfo
    from .twitch.pubsub import PlaybackState

log = get_logger("liveness")


class State(StrEnum):
    IDLE = "IDLE"
    WATCHING = "WATCHING"
    SUSPECT = "SUSPECT"
    OFFLINE = "OFFLINE"
    STALLED = "STALLED"
    PAUSED = "PAUSED"


class Vote(StrEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    #: The signal has no opinion. Abstaining is a first-class answer, not a
    #: failure — it's what keeps a dead socket from looking like a dead stream.
    UNKNOWN = "UNKNOWN"
    #: Working but not doing its job: telemetry rejected, or nothing crediting.
    DEGRADED = "DEGRADED"


@dataclass(frozen=True, slots=True)
class Signal:
    id: str
    name: str
    weight: int
    #: Whether this signal's vote counts toward the online/offline quorum. S4 and
    #: S5 report health, not liveness, and must not be able to trigger a rotation
    #: on their own.
    counts_for_liveness: bool = True


SIGNALS: tuple[Signal, ...] = (
    Signal("S1", "PubSub stream-down", weight=3),
    Signal("S2", "user.stream is null", weight=3),
    Signal("S3", "playback token", weight=2),
    Signal("S4", "spade rejected", weight=1, counts_for_liveness=False),
    Signal("S5", "progress delta", weight=1, counts_for_liveness=False),
)

_BY_ID = {s.id: s for s in SIGNALS}


@dataclass(frozen=True, slots=True)
class Reading:
    signal: Signal
    vote: Vote
    detail: str = ""

    @property
    def id(self) -> str:
        return self.signal.id


@dataclass(frozen=True, slots=True)
class Observation:
    """Everything the detector needs for one evaluation.

    Every field is optional because a cycle where one source didn't answer is
    normal — that source abstains rather than the cycle failing.
    """

    #: Latest GQL stream state, or ``None`` if not polled this cycle.
    stream: StreamInfo | None = None
    #: Latest PubSub state for the watched channel.
    pubsub: PlaybackState | None = None
    #: Whether the PubSub socket is currently up at all.
    pubsub_connected: bool = False
    #: ``True`` if the last telemetry POST was accepted.
    telemetry_ok: bool | None = None
    #: Whether telemetry has failed enough times to distrust it.
    telemetry_degraded: bool = False
    #: Minutes credited since the previous progress read; ``None`` if unread.
    progress_delta: int | None = None
    #: Result of an active playback probe; ``None`` if not probed this cycle.
    playback_ok: bool | None = None


@dataclass(frozen=True, slots=True)
class Verdict:
    state: State
    previous: State
    reason: str
    readings: list[Reading] = field(default_factory=list)
    offline_weight: int = 0
    online_weight: int = 0
    total_weight: int = 0
    #: The detector wants an active probe before it will commit to OFFLINE.
    needs_probe: bool = False
    #: Terminal for this target — the watcher should rotate.
    should_rotate: bool = False
    grace_remaining: float = 0.0
    confirmations: int = 0

    @property
    def changed(self) -> bool:
        return self.state is not self.previous

    def offline_voters(self) -> list[str]:
        return [
            r.id for r in self.readings
            if r.vote is Vote.OFFLINE and r.signal.counts_for_liveness
        ]

    def signal_map(self) -> dict[str, str]:
        """Compact form for the event payload and the transitions table."""
        return {r.id: r.vote.value for r in self.readings}

    def explain(self) -> str:
        voters = "+".join(self.offline_voters()) or "none"
        return (
            f"{self.state.value} (was {self.previous.value}): {self.reason} "
            f"[offline {self.offline_weight}/{self.total_weight}, voters {voters}]"
        )


class LivenessDetector:
    """The state machine. Owns hysteresis state; performs no I/O."""

    def __init__(
        self,
        config: LivenessConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._clock = clock
        self._state = State.IDLE
        self._suspect_since: float | None = None
        self._confirmations = 0
        self._zero_delta_runs = 0

    @property
    def state(self) -> State:
        return self._state

    @property
    def zero_delta_runs(self) -> int:
        return self._zero_delta_runs

    def reset(self, state: State = State.IDLE) -> None:
        """Clear hysteresis. Called on rotation, pause and resume."""
        self._state = state
        self._suspect_since = None
        self._confirmations = 0
        self._zero_delta_runs = 0

    # -------------------------------------------------------------- signals

    def read(self, obs: Observation) -> list[Reading]:
        return [
            self._read_pubsub(obs),
            self._read_stream(obs),
            self._read_playback(obs),
            self._read_telemetry(obs),
            self._read_progress(obs),
        ]

    def _read_pubsub(self, obs: Observation) -> Reading:
        signal = _BY_ID["S1"]
        state = obs.pubsub

        if not obs.pubsub_connected:
            # The single most important abstention in the whole detector.
            return Reading(signal, Vote.UNKNOWN, "socket down — cannot observe")
        if state is None or state.online is None:
            return Reading(signal, Vote.UNKNOWN, "no playback message yet")
        # Compared against the detector's own clock rather than PlaybackState's
        # convenience property: the detector is the single time source here, which
        # is also what makes the machine testable without sleeping.
        if self._clock() < state.in_commercial_until:
            # An ad break interrupts playback without ending the broadcast.
            return Reading(signal, Vote.UNKNOWN, "commercial in progress")
        if state.online:
            return Reading(signal, Vote.ONLINE, "stream-up")
        return Reading(signal, Vote.OFFLINE, "stream-down")

    def _read_stream(self, obs: Observation) -> Reading:
        signal = _BY_ID["S2"]
        stream = obs.stream
        if stream is None:
            return Reading(signal, Vote.UNKNOWN, "not polled this cycle")
        if not stream.live:
            return Reading(signal, Vote.OFFLINE, "stream is null")
        if stream.is_rerun:
            # Live, so it does not vote offline — but it cannot credit, which S5
            # is what actually catches.
            return Reading(signal, Vote.ONLINE, f"live but {stream.stream_type}")
        return Reading(signal, Vote.ONLINE, "live")

    def _read_playback(self, obs: Observation) -> Reading:
        signal = _BY_ID["S3"]
        if obs.playback_ok is None:
            return Reading(signal, Vote.UNKNOWN, "not probed")
        if obs.playback_ok:
            return Reading(signal, Vote.ONLINE, "token issued")
        return Reading(signal, Vote.OFFLINE, "token refused")

    def _read_telemetry(self, obs: Observation) -> Reading:
        signal = _BY_ID["S4"]
        if obs.telemetry_degraded:
            return Reading(signal, Vote.DEGRADED, "repeatedly rejected")
        if obs.telemetry_ok is None:
            return Reading(signal, Vote.UNKNOWN, "not sent this cycle")
        if obs.telemetry_ok:
            return Reading(signal, Vote.ONLINE, "accepted")
        return Reading(signal, Vote.DEGRADED, "rejected once")

    def _read_progress(self, obs: Observation) -> Reading:
        signal = _BY_ID["S5"]
        if obs.progress_delta is None:
            return Reading(signal, Vote.UNKNOWN, "not read this cycle")
        if obs.progress_delta > 0:
            return Reading(signal, Vote.ONLINE, f"+{obs.progress_delta} min")
        return Reading(signal, Vote.DEGRADED, "no minutes credited")

    @staticmethod
    def quorum(readings: list[Reading]) -> tuple[int, int, int]:
        """Weighted (offline, online, total) over liveness-bearing signals only."""
        offline = online = total = 0
        for reading in readings:
            if not reading.signal.counts_for_liveness:
                continue
            if reading.vote in (Vote.UNKNOWN, Vote.DEGRADED):
                continue
            total += reading.signal.weight
            if reading.vote is Vote.OFFLINE:
                offline += reading.signal.weight
            else:
                online += reading.signal.weight
        return offline, online, total

    # -------------------------------------------------------- state machine

    def evaluate(self, obs: Observation) -> Verdict:
        readings = self.read(obs)
        offline, online, total = self.quorum(readings)
        previous = self._state
        now = self._clock()

        def verdict(
            state: State,
            reason: str,
            *,
            probe: bool = False,
            rotate: bool = False,
        ) -> Verdict:
            self._state = state
            remaining = 0.0
            if state is State.SUSPECT and self._suspect_since is not None:
                remaining = max(0.0, self._config.grace_period - (now - self._suspect_since))
            return Verdict(
                state=state,
                previous=previous,
                reason=reason,
                readings=readings,
                offline_weight=offline,
                online_weight=online,
                total_weight=total,
                needs_probe=probe,
                should_rotate=rotate,
                grace_remaining=remaining,
                confirmations=self._confirmations,
            )

        if self._state is State.PAUSED:
            return verdict(State.PAUSED, "paused by operator")

        # Terminal states rotate on the cycle after they're entered, so they are
        # observable in /status rather than flashing past between polls.
        if self._state in (State.OFFLINE, State.STALLED):
            return verdict(self._state, "awaiting rotation", rotate=True)

        if self._state is State.IDLE:
            if obs.stream is not None and obs.stream.drops_eligible:
                self._suspect_since = None
                self._confirmations = 0
                return verdict(State.WATCHING, f"{obs.stream.login} is live and eligible")
            return verdict(State.IDLE, "no eligible target")

        # --- track crediting -------------------------------------------------
        credited = obs.progress_delta is not None and obs.progress_delta > 0
        if credited:
            self._zero_delta_runs = 0
        elif obs.progress_delta is not None:
            self._zero_delta_runs += 1

        # --- STALLED: live, telemetry accepted, but nothing credited ---------
        # Checked before the offline path: a rerun reports live on every liveness
        # signal, so only this catches it.
        if self._state is State.WATCHING and offline == 0:
            if self._zero_delta_runs >= self._config.stall_cycles:
                rerun = obs.stream is not None and obs.stream.is_rerun
                reason = (
                    "target is a rerun — drops never credit on vodcasts" if rerun
                    else f"no progress across {self._zero_delta_runs} checks while live"
                )
                return verdict(State.STALLED, reason)

        # --- WATCHING -> SUSPECT ---------------------------------------------
        if self._state is State.WATCHING:
            if offline > 0:
                self._suspect_since = now
                self._confirmations = 1
                voters = "+".join(
                    r.id for r in readings
                    if r.vote is Vote.OFFLINE and r.signal.counts_for_liveness
                )
                return verdict(
                    State.SUSPECT,
                    f"offline vote from {voters} — probing to confirm",
                    probe=True,
                )
            return verdict(State.WATCHING, "all liveness signals agree the stream is up")

        # --- SUSPECT: confirm, or absorb the flap ----------------------------
        if self._state is State.SUSPECT:
            elapsed = now - (self._suspect_since or now)

            if offline == 0:
                self._suspect_since = None
                self._confirmations = 0
                return verdict(
                    State.WATCHING,
                    f"signals recovered after {elapsed:.0f}s — flap absorbed, no rotation",
                )

            self._confirmations += 1
            confirmed = (
                self._confirmations >= self._config.confirm_reads and offline > online
            )
            if confirmed and elapsed >= self._config.grace_period:
                voters = "+".join(
                    r.id for r in readings
                    if r.vote is Vote.OFFLINE and r.signal.counts_for_liveness
                )
                return verdict(
                    State.OFFLINE,
                    f"confirmed offline by {voters} after {elapsed:.0f}s grace",
                )

            waiting = "grace period" if confirmed else "confirmation"
            return verdict(
                State.SUSPECT,
                f"holding {elapsed:.0f}s of {self._config.grace_period:.0f}s — "
                f"awaiting {waiting}",
                probe=True,
            )

        return verdict(self._state, "no change")

    # ------------------------------------------------------------- controls

    def pause(self) -> None:
        self.reset(State.PAUSED)

    def resume(self) -> None:
        self.reset(State.IDLE)
