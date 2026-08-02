"""Target selection, the watch loop, and rotation.

Owns the I/O the detector deliberately doesn't: polling stream state, posting
telemetry, reading progress, running the active probe the detector asks for, and
choosing what to watch next when a target dies.

One constraint drives the whole shape of this: **Twitch credits drop progress for
one stream at a time per account.** Watching five channels in parallel earns
exactly what watching one earns, so this is a single-target watcher with smart
rotation, not a fan-out miner.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .events import EventType
from .liveness import LivenessDetector, Observation, State, Verdict
from .log import get_logger
from .store import Transition
from .twitch.channels import StreamInfo
from .twitch.gql import GQLError, IntegrityChallengeError, SchemaDriftError

if TYPE_CHECKING:
    from .config import ConfigManager
    from .events import EventBus
    from .store import Store
    from .twitch.auth import AuthManager
    from .twitch.channels import ChannelClient
    from .twitch.drops import DropsClient
    from .twitch.pubsub import PubSubClient
    from .twitch.spade import SpadeClient

log = get_logger("watcher")


@dataclass(slots=True)
class SessionStats:
    """Live counters for the current target, surfaced by ``/status``."""

    channel: str | None = None
    started_at: float = 0.0
    cycles: int = 0
    minutes_sent: int = 0
    telemetry_rejected: int = 0
    credited_start: int | None = None
    credited_now: int | None = None
    required: int | None = None
    session_id: int | None = None

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.started_at if self.started_at else 0.0

    @property
    def credited_gain(self) -> int | None:
        if self.credited_start is None or self.credited_now is None:
            return None
        return self.credited_now - self.credited_start


@dataclass(slots=True)
class WatcherStatus:
    state: str
    channel: str | None
    reason: str
    signals: dict[str, str]
    stats: SessionStats
    rotations: int
    paused: bool
    grace_remaining: float = 0.0
    pubsub_connected: bool = False
    last_verdict_at: float = 0.0


class Watcher:
    def __init__(
        self,
        *,
        config_manager: ConfigManager,
        bus: EventBus,
        store: Store,
        auth: AuthManager,
        channels: ChannelClient,
        spade: SpadeClient,
        drops: DropsClient,
        pubsub: PubSubClient,
        account: str = "",
    ) -> None:
        self._cfg_manager = config_manager
        self._bus = bus
        self._account = account
        # A logger named for the account, so a combined log stays readable when
        # several watchers are talking at once. The dashboard shows this name.
        self._log = get_logger(f"watcher.{account}" if account else "watcher")
        self._store = store
        self._auth = auth
        self._channels = channels
        self._spade = spade
        self._drops = drops
        self._pubsub = pubsub

        self._detector = LivenessDetector(config_manager.current.liveness)
        self._target: StreamInfo | None = None
        self._stats = SessionStats()
        self._rotations = 0
        self._paused = False
        self._stop = asyncio.Event()
        self._last_verdict: Verdict | None = None
        self._last_verdict_at = 0.0
        self._last_stream_poll = 0.0
        self._announced_no_targets = False
        self._select_reason = ""
        self._milestones: set[int] = set()
        # Persist across rotations: a reward stays earned regardless of what the
        # bot is watching now, so re-announcing it on every target switch is noise.
        self._seen_claimable: set[str] = set()
        self._claim_blocked_announced = False

    # ---------------------------------------------------------------- config

    @property
    def _cfg(self) -> Any:
        return self._cfg_manager.current

    @property
    def account(self) -> str:
        return self._account

    def rename(self, account: str, bus: Any) -> None:
        """Adopt an identity discovered after construction (a fresh sign-in)."""
        self._account = account
        self._bus = bus
        self._log = get_logger(f"watcher.{account}" if account else "watcher")

    def status(self) -> WatcherStatus:
        verdict = self._last_verdict
        return WatcherStatus(
            state=(State.PAUSED if self._paused else self._detector.state).value,
            channel=self._target.login if self._target else None,
            reason=verdict.reason if verdict else "not started",
            signals=verdict.signal_map() if verdict else {},
            stats=self._stats,
            rotations=self._rotations,
            paused=self._paused,
            grace_remaining=verdict.grace_remaining if verdict else 0.0,
            pubsub_connected=self._pubsub.connected,
            last_verdict_at=self._last_verdict_at,
        )

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        """Main loop. Returns when :meth:`stop` is called."""
        # A previous run that was killed leaves a session row reading "running"
        # forever, which skews every statistic drawn from the table. Close those
        # out before adding to them.
        recovered = await self._store.reconcile_open_sessions(self._account)
        if recovered:
            self._log.info("closed %d session(s) left open by an earlier run", recovered)

        await self._pubsub.start()
        self._log.info("watcher started")
        try:
            while not self._stop.is_set():
                if self._paused:
                    await self._sleep(2.0)
                    continue
                try:
                    await self._tick()
                except SchemaDriftError as exc:
                    # Retrying cannot help; back off hard and stay loud.
                    self._log.error("schema drift: %s", exc)
                    await self._bus.publish(
                        EventType.SCHEMA_DRIFT, operation="watch loop", reason=str(exc)
                    )
                    await self._sleep(120.0)
                except (GQLError, TimeoutError, OSError) as exc:
                    self._log.warning("watch cycle failed: %s", exc)
                    await self._bus.publish(
                        EventType.ERROR, where="watch loop", error=str(exc)
                    )
                    await self._sleep(30.0)
        finally:
            await self._close_session("watcher stopped")
            await self._pubsub.stop()
            self._log.info("watcher stopped")

    async def stop(self) -> None:
        self._stop.set()

    async def pause(self) -> None:
        if self._paused:
            return
        self._paused = True
        self._detector.pause()
        await self._close_session("paused")
        await self._pubsub.unwatch_all()
        await self._bus.publish(EventType.WATCH_STOPPED, reason="paused by operator")
        self._log.info("watcher paused")

    async def resume(self) -> None:
        if not self._paused:
            return
        self._paused = False
        self._detector.resume()
        self._target = None
        self._announced_no_targets = False
        await self._bus.publish(EventType.WATCH_STARTED, reason="resumed by operator")
        self._log.info("watcher resumed")

    async def _sleep(self, seconds: float) -> None:
        """Interruptible sleep — stop() must not wait out a full poll interval."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    # ------------------------------------------------------------ target pick

    async def _resolve_target(self, exclude: str | None = None) -> StreamInfo | None:
        """Highest-priority live, drops-eligible channel, else discovery.

        ``exclude`` keeps a target that just failed from being re-selected
        immediately, which would otherwise rotate in place forever.
        """
        configured = [c.login for c in self._cfg.watch.ordered()]
        if configured:
            states = await self._channels.fetch_many(configured)
            for login in configured:  # already in priority order
                info = states.get(login)
                if info and info.drops_eligible and info.login != exclude:
                    self._select_reason = f"priority {self._priority_of(login)}"
                    return info

        if not self._cfg.watch.auto_discovery:
            return None

        try:
            found = await self._channels.discover()
        except GQLError as exc:
            self._log.warning("discovery failed: %s", exc)
            return None

        for info in found:  # already sorted by viewers
            if info.login != exclude:
                self._select_reason = f"auto-discovered, {info.viewers or 0:,} viewers"
                return info
        return None

    def _priority_of(self, login: str) -> int:
        for channel in self._cfg.watch.ordered():
            if channel.login == login:
                return channel.priority
        return 0

    # -------------------------------------------------------------- the loop

    async def _tick(self) -> None:
        if self._target is None:
            await self._acquire_target()
            if self._target is None:
                await self._sleep(self._cfg.watch.idle_poll_interval)
            return

        obs = await self._observe()
        verdict = self._detector.evaluate(obs)
        await self._apply(verdict)

        # An active probe is the detector's own escalation: it asked for stronger
        # evidence before committing to a rotation, so answer it now rather than
        # waiting a whole cycle.
        if verdict.needs_probe and not self._stop.is_set():
            probe = await self._probe()
            verdict = self._detector.evaluate(probe)
            await self._apply(verdict)

        if verdict.should_rotate:
            await self._rotate(verdict)
            return

        await self._sleep(self._spade.next_interval())

    async def _acquire_target(self) -> None:
        target = await self._resolve_target()
        if target is None:
            if not self._announced_no_targets:
                self._announced_no_targets = True
                await self._bus.publish(
                    EventType.NO_TARGETS, game=self._cfg.watch.game_slug
                )
                self._log.info("no eligible targets; idling")
            return

        self._announced_no_targets = False
        await self._begin_session(target)

    async def _begin_session(self, target: StreamInfo) -> None:
        self._target = target
        self._detector.reset(State.IDLE)
        self._spade.reset()
        self._stats = SessionStats(channel=target.login, started_at=time.monotonic())
        self._last_stream_poll = 0.0
        self._milestones = set()

        if target.channel_id:
            await self._pubsub.watch_channel(target.channel_id)

        self._stats.session_id = await self._store.start_session(target.login, self._account)

        baseline = await self._read_progress()
        if baseline is not None:
            self._stats.credited_start = baseline[0]
            self._stats.credited_now = baseline[0]
            self._stats.required = baseline[1]

        await self._bus.publish(
            EventType.WATCH_STARTED,
            channel=target.login,
            reason=self._select_reason,
            viewers=target.viewers,
        )
        self._log.info("watching %s (%s)", target.login, self._select_reason)

    async def _observe(self) -> Observation:
        """Run one telemetry cycle and gather every signal's input."""
        assert self._target is not None
        tokens = self._auth.tokens
        user_id = tokens.user_id if tokens else None

        telemetry_ok: bool | None = None
        if user_id:
            result = await self._spade.send_minute_watched(self._target, user_id)
            telemetry_ok = result.accepted
            self._stats.cycles += 1
            if result.accepted:
                self._stats.minutes_sent += 1
            else:
                self._stats.telemetry_rejected += 1

        # Stream state on its own cadence, not every telemetry cycle.
        stream = None
        now = time.monotonic()
        if now - self._last_stream_poll >= self._cfg.liveness.stream_poll_interval:
            self._last_stream_poll = now
            stream = await self._fetch_stream()

        delta: int | None = None
        every = self._cfg.watch.progress_check_every
        if self._stats.cycles and self._stats.cycles % every == 0:
            progress = await self._read_progress()
            if progress is not None:
                current, required = progress
                previous = self._stats.credited_now
                self._stats.credited_now = current
                self._stats.required = required
                if self._stats.credited_start is None:
                    self._stats.credited_start = current
                delta = 0 if previous is None else current - previous
                await self._maybe_announce_progress(current, required)
            else:
                # No active drop session is itself "nothing is crediting".
                delta = 0
            await self._check_claimable()
            await self._store.record_sample(
                session_id=self._stats.session_id,
                channel=self._target.login,
                minutes_sent=self._stats.minutes_sent,
                credited=self._stats.credited_now,
                required=self._stats.required,
                state=self._detector.state.value,
                account=self._account,
            )

        return Observation(
            stream=stream,
            pubsub=(
                self._pubsub.state(self._target.channel_id)
                if self._target.channel_id else None
            ),
            pubsub_connected=self._pubsub.connected,
            telemetry_ok=telemetry_ok,
            telemetry_degraded=self._spade.degraded,
            progress_delta=delta,
        )

    async def _probe(self) -> Observation:
        """Force S2 and S3: the strongest evidence available on demand."""
        assert self._target is not None
        stream = await self._fetch_stream()
        try:
            playback = await self._channels.playback_probe(self._target.login)
        except SchemaDriftError:
            raise
        except GQLError:
            playback = None

        return Observation(
            stream=stream,
            pubsub=(
                self._pubsub.state(self._target.channel_id)
                if self._target.channel_id else None
            ),
            pubsub_connected=self._pubsub.connected,
            telemetry_degraded=self._spade.degraded,
            playback_ok=playback,
        )

    async def _fetch_stream(self) -> StreamInfo | None:
        assert self._target is not None
        try:
            info = await self._channels.fetch(self._target.login)
        except SchemaDriftError:
            raise
        except (GQLError, TimeoutError) as exc:
            self._log.debug("stream poll failed: %s", exc)
            return None

        # Keep the broadcast id current: a reconnecting broadcaster gets a new one,
        # and telemetry against a stale id credits nothing.
        if info.live and info.stream_id and info.stream_id != self._target.stream_id:
            self._log.info("%s has a new broadcast id — telemetry retargeted", info.login)
            self._target = info
        return info

    async def _read_progress(self) -> tuple[int, int] | None:
        assert self._target is not None
        try:
            progress = await self._drops.current_session(self._target.login)
        except SchemaDriftError:
            raise
        except (GQLError, TimeoutError) as exc:
            self._log.debug("progress read failed: %s", exc)
            return None
        if not progress.active:
            return None
        return progress.current_minutes, progress.required_minutes

    async def _check_claimable(self) -> None:
        """Find earned rewards and try to collect them.

        Claiming is gated behind Twitch's integrity challenge, so the realistic
        outcome is CLAIM_BLOCKED rather than DROP_CLAIMED. The attempt is still
        made once per drop because the code is correct and would simply start
        working if Twitch stopped gating it — but the blocked notice is only
        emitted once, not every cycle, or it would bury the self._log.
        """
        try:
            pending = await self._drops.claimable()
        except SchemaDriftError:
            raise
        except (GQLError, TimeoutError) as exc:
            self._log.debug("claimable check failed: %s", exc)
            return

        for drop in pending:
            if drop.id in self._seen_claimable:
                continue
            self._seen_claimable.add(drop.id)

            await self._bus.publish(
                EventType.DROP_CLAIMABLE,
                channel=self._target.login if self._target else None,
                reward=drop.reward_name,
                drop=drop.name,
                minutes=drop.current_minutes,
                required=drop.required_minutes,
            )
            self._log.info("reward earned and waiting: %s", drop.reward_name)

            if not self._cfg.watch.auto_claim:
                continue

            try:
                status = await self._drops.claim(drop)
            except IntegrityChallengeError:
                if not self._claim_blocked_announced:
                    self._claim_blocked_announced = True
                    await self._bus.publish(
                        EventType.CLAIM_BLOCKED,
                        reward=drop.reward_name,
                        reason="Twitch requires a Client-Integrity token to claim",
                        where="https://www.twitch.tv/drops/inventory",
                    )
                    self._log.warning(
                        "cannot auto-claim %s — Twitch gates claiming behind an "
                        "integrity check. Collect it at twitch.tv/drops/inventory",
                        drop.reward_name,
                    )
            except (GQLError, TimeoutError, ValueError) as exc:
                self._log.warning("claim failed for %s: %s", drop.reward_name, exc)
            else:
                await self._bus.publish(
                    EventType.DROP_CLAIMED,
                    reward=drop.reward_name, drop=drop.name, status=status,
                )

    async def _maybe_announce_progress(self, current: int, required: int) -> None:
        """Announce each milestone once per session, not on every read."""
        if required <= 0:
            return
        percent = int(current / required * 100)
        for milestone in (25, 50, 75, 100):
            if percent >= milestone and milestone not in self._milestones:
                self._milestones.add(milestone)
                await self._bus.publish(
                    EventType.DROP_PROGRESS,
                    channel=self._target.login if self._target else None,
                    percent=milestone,
                    current=current,
                    required=required,
                )
                if milestone == 100:
                    await self._bus.publish(
                        EventType.DROP_COMPLETED,
                        channel=self._target.login if self._target else None,
                        current=current,
                        required=required,
                    )

    # ------------------------------------------------------------ transitions

    async def _apply(self, verdict: Verdict) -> None:
        self._last_verdict = verdict
        self._last_verdict_at = time.time()
        if not verdict.changed:
            return

        self._log.info("%s", verdict.explain())
        await self._store.record_transition(Transition(
            ts=time.time(),
            channel=self._target.login if self._target else None,
            from_state=verdict.previous.value,
            to_state=verdict.state.value,
            reason=verdict.reason,
            signals=verdict.signal_map(),
            account=self._account,
        ))
        await self._bus.publish(
            EventType.STATE_CHANGED,
            channel=self._target.login if self._target else None,
            from_state=verdict.previous.value,
            to_state=verdict.state.value,
            reason=verdict.reason,
            signals=verdict.signal_map(),
            offline_weight=verdict.offline_weight,
            total_weight=verdict.total_weight,
        )

        if verdict.state is State.OFFLINE:
            await self._bus.publish(
                EventType.STREAM_ENDED,
                channel=self._target.login if self._target else None,
                reason=verdict.reason,
                signals=verdict.signal_map(),
            )
        elif verdict.state is State.STALLED:
            # The wording matters: someone reading this must not go looking for a
            # network fault when the stream is up and simply not crediting.
            await self._bus.publish(
                EventType.STREAM_STALLED,
                channel=self._target.login if self._target else None,
                reason=verdict.reason,
                detail="live but not crediting",
                signals=verdict.signal_map(),
            )

    async def _rotate(self, verdict: Verdict) -> None:
        previous = self._target.login if self._target else None
        await self._close_session(verdict.state.value.lower())

        nxt = await self._resolve_target(exclude=previous)
        self._rotations += 1

        if nxt is None:
            self._target = None
            self._detector.reset(State.IDLE)
            await self._bus.publish(
                EventType.TARGET_SWITCHED,
                from_channel=previous, to_channel=None, reason=verdict.reason,
            )
            self._log.info("nothing else eligible after %s; idling", previous)
            await self._sleep(self._cfg.watch.idle_poll_interval)
            return

        await self._bus.publish(
            EventType.TARGET_SWITCHED,
            from_channel=previous,
            to_channel=nxt.login,
            reason=verdict.reason,
            state=verdict.state.value,
        )
        await self._begin_session(nxt)

    async def _close_session(self, reason: str) -> None:
        if self._stats.session_id is not None:
            await self._store.end_session(
                self._stats.session_id, self._stats.minutes_sent, reason
            )
            self._stats.session_id = None
        if self._target is not None:
            await self._pubsub.unwatch_all()
