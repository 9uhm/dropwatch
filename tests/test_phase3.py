"""Phase 3 tests: the liveness state machine and PubSub message handling.

These mirror the scenarios the console simulator was built to prove, now against
the real Python implementation. The three that matter most:

* an ad break must never rotate
* a dead PubSub socket must never read as offline
* stopped crediting must land in STALLED, not OFFLINE

The detector does no I/O, so the whole machine is driven synchronously with a
fake clock — no sleeping, no network, no flakiness.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from dropwatch.config import LivenessConfig
from dropwatch.liveness import (
    LivenessDetector,
    Observation,
    State,
    Vote,
)
from dropwatch.twitch.channels import StreamInfo
from dropwatch.twitch.pubsub import PlaybackState, PubSubClient

# ------------------------------------------------------------------- helpers


class Clock:
    """Manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def detector(**overrides: Any) -> tuple[LivenessDetector, Clock]:
    clock = Clock()
    config = LivenessConfig(**{
        "grace_period": 90.0, "confirm_reads": 2, "stall_cycles": 3, **overrides,
    })
    return LivenessDetector(config, clock=clock), clock


LIVE = StreamInfo(login="ow_esports", channel_id="1", live=True,
                  stream_id="9", stream_type="live")
OFFLINE = StreamInfo(login="ow_esports", channel_id="1", live=False)
RERUN = StreamInfo(login="ow_esports", channel_id="1", live=True,
                   stream_id="9", stream_type="rerun")


def up(**kw: Any) -> Observation:
    """A healthy cycle: PubSub up, stream live, telemetry accepted, crediting."""
    base: dict[str, Any] = {
        "stream": LIVE,
        "pubsub": PlaybackState(channel_id="1", online=True, updated_at=1000.0),
        "pubsub_connected": True,
        "telemetry_ok": True,
        "progress_delta": 1,
    }
    base.update(kw)
    return Observation(**base)


def settle(det: LivenessDetector) -> None:
    """Get from IDLE into WATCHING."""
    det.evaluate(up())
    assert det.state is State.WATCHING


# ------------------------------------------------------------- basic transitions


def test_idle_waits_for_an_eligible_target() -> None:
    det, _ = detector()
    verdict = det.evaluate(Observation(stream=OFFLINE))
    assert verdict.state is State.IDLE

    # A rerun is live but must not start a session.
    assert det.evaluate(Observation(stream=RERUN)).state is State.IDLE
    assert det.evaluate(Observation(stream=LIVE)).state is State.WATCHING


def test_watching_stays_watching_while_signals_agree() -> None:
    det, _ = detector()
    settle(det)
    for _ in range(5):
        verdict = det.evaluate(up())
    assert verdict.state is State.WATCHING and not verdict.should_rotate


# ------------------------------------------------------------------- ad breaks


def test_commercial_makes_pubsub_abstain_rather_than_vote_offline() -> None:
    """An ad break interrupts playback; it does not end a broadcast."""
    det, clock = detector()
    settle(det)

    ad = PlaybackState(channel_id="1", online=False, updated_at=clock.now,
                       in_commercial_until=clock.now + 40)
    verdict = det.evaluate(up(pubsub=ad, progress_delta=0))

    s1 = next(r for r in verdict.readings if r.id == "S1")
    assert s1.vote is Vote.UNKNOWN
    assert "commercial" in s1.detail
    assert verdict.state is State.WATCHING, "an ad break must not even reach SUSPECT"


def test_brief_blip_is_absorbed_without_rotating() -> None:
    """A PubSub stream-down that recovers inside grace must not rotate."""
    det, clock = detector()
    settle(det)

    down = PlaybackState(channel_id="1", online=False, updated_at=clock.now)
    verdict = det.evaluate(up(pubsub=down, stream=None, progress_delta=None))
    assert verdict.state is State.SUSPECT
    assert verdict.needs_probe, "must escalate to an active probe before committing"

    clock.advance(30)
    recovered = det.evaluate(up())
    assert recovered.state is State.WATCHING
    assert "flap absorbed" in recovered.reason
    assert not recovered.should_rotate


# ------------------------------------------------------------- dead socket rule


def test_dead_socket_abstains_and_never_rotates() -> None:
    det, clock = detector()
    settle(det)

    for _ in range(10):
        clock.advance(60)
        verdict = det.evaluate(Observation(
            stream=LIVE, pubsub=None, pubsub_connected=False,
            telemetry_ok=True, progress_delta=1,
        ))

    s1 = next(r for r in verdict.readings if r.id == "S1")
    assert s1.vote is Vote.UNKNOWN and "socket down" in s1.detail
    assert verdict.state is State.WATCHING
    assert verdict.offline_weight == 0


def test_dead_socket_does_not_block_a_real_offline_verdict() -> None:
    """Abstaining must not make the bot unable to ever detect an ending."""
    det, clock = detector()
    settle(det)

    obs = Observation(stream=OFFLINE, pubsub=None, pubsub_connected=False,
                      telemetry_ok=False)
    assert det.evaluate(obs).state is State.SUSPECT

    clock.advance(120)
    verdict = det.evaluate(Observation(
        stream=OFFLINE, pubsub=None, pubsub_connected=False, playback_ok=False,
    ))
    assert verdict.state is State.OFFLINE
    assert set(verdict.offline_voters()) == {"S2", "S3"}


# ------------------------------------------------------------------- stalling


def test_no_crediting_reaches_stalled_not_offline() -> None:
    det, _ = detector(stall_cycles=3)
    settle(det)

    for _ in range(3):
        verdict = det.evaluate(up(progress_delta=0))

    assert verdict.state is State.STALLED
    assert verdict.state is not State.OFFLINE
    assert "no progress" in verdict.reason


def test_rerun_stalls_with_a_reason_that_names_the_cause() -> None:
    """The message must not send someone hunting a network fault."""
    det, _ = detector(stall_cycles=2)
    settle(det)

    for _ in range(2):
        verdict = det.evaluate(up(stream=RERUN, progress_delta=0))

    assert verdict.state is State.STALLED
    assert "rerun" in verdict.reason and "credit" in verdict.reason


def test_crediting_again_resets_the_stall_counter() -> None:
    det, _ = detector(stall_cycles=3)
    settle(det)

    det.evaluate(up(progress_delta=0))
    det.evaluate(up(progress_delta=0))
    assert det.zero_delta_runs == 2

    det.evaluate(up(progress_delta=2))
    assert det.zero_delta_runs == 0
    assert det.state is State.WATCHING


def test_unread_progress_does_not_count_toward_stalling() -> None:
    """Not reading progress is not the same as reading zero."""
    det, _ = detector(stall_cycles=2)
    settle(det)
    for _ in range(6):
        det.evaluate(up(progress_delta=None))
    assert det.state is State.WATCHING and det.zero_delta_runs == 0


# ------------------------------------------------- confirmation and grace period


def test_offline_requires_both_confirmation_and_grace() -> None:
    det, clock = detector(grace_period=90, confirm_reads=2)
    settle(det)

    assert det.evaluate(Observation(stream=OFFLINE, pubsub_connected=False)).state \
        is State.SUSPECT

    # Confirmed, but grace has not elapsed — must hold.
    clock.advance(10)
    held = det.evaluate(Observation(stream=OFFLINE, playback_ok=False))
    assert held.state is State.SUSPECT
    assert held.grace_remaining == pytest.approx(80.0)

    clock.advance(100)
    done = det.evaluate(Observation(stream=OFFLINE, playback_ok=False))
    assert done.state is State.OFFLINE


def test_a_single_read_never_rotates_even_after_grace() -> None:
    """confirm_reads is a floor independent of elapsed time."""
    det, clock = detector(grace_period=0, confirm_reads=3)
    settle(det)

    first = det.evaluate(Observation(stream=OFFLINE, pubsub_connected=False))
    assert first.state is State.SUSPECT
    clock.advance(600)

    second = det.evaluate(Observation(stream=OFFLINE, playback_ok=False))
    assert second.state is State.SUSPECT, "two reads is still short of three"

    third = det.evaluate(Observation(stream=OFFLINE, playback_ok=False))
    assert third.state is State.OFFLINE


def test_online_outweighing_offline_blocks_confirmation() -> None:
    """A minority offline signal must not carry a rotation."""
    det, clock = detector(grace_period=0, confirm_reads=1)
    settle(det)

    down = PlaybackState(channel_id="1", online=False, updated_at=clock.now)
    assert det.evaluate(up(pubsub=down)).state is State.SUSPECT

    clock.advance(300)
    # S1 says offline (3) but S2 live + S3 probe ok (3+2=5) outvote it.
    verdict = det.evaluate(up(pubsub=down, playback_ok=True))
    assert verdict.offline_weight == 3 and verdict.online_weight == 5
    assert verdict.state is State.SUSPECT, "held, because online outweighs offline"


# ------------------------------------------------------------ terminal states


def test_terminal_states_are_observable_before_rotating() -> None:
    """Rotating in the same cycle would make OFFLINE/STALLED invisible.

    This is the bug the console simulator surfaced first: the state has to survive
    one evaluation so /status and the transitions log can show it.
    """
    for reach, expected in (
        (lambda d, c: _drive_offline(d, c), State.OFFLINE),
        (lambda d, c: _drive_stalled(d, c), State.STALLED),
    ):
        det, clock = detector(stall_cycles=2, confirm_reads=1, grace_period=0)
        settle(det)
        verdict = reach(det, clock)

        assert verdict.state is expected
        assert not verdict.should_rotate, "first sighting must not rotate yet"

        after = det.evaluate(up())
        assert after.state is expected, "state persists so it can be reported"
        assert after.should_rotate, "rotation is requested on the following cycle"


def _drive_offline(det: LivenessDetector, clock: Clock) -> Any:
    det.evaluate(Observation(stream=OFFLINE, pubsub_connected=False))
    clock.advance(10)
    return det.evaluate(Observation(stream=OFFLINE, playback_ok=False))


def _drive_stalled(det: LivenessDetector, clock: Clock) -> Any:
    det.evaluate(up(progress_delta=0))
    return det.evaluate(up(progress_delta=0))


# ------------------------------------------------------------------- quorum


def test_health_signals_cannot_trigger_a_rotation() -> None:
    """S4 and S5 report health, not liveness, and must stay out of the quorum."""
    det, _ = detector()
    settle(det)

    verdict = det.evaluate(up(telemetry_ok=False, telemetry_degraded=True,
                              progress_delta=0))
    s4 = next(r for r in verdict.readings if r.id == "S4")
    s5 = next(r for r in verdict.readings if r.id == "S5")

    assert s4.vote is Vote.DEGRADED and s5.vote is Vote.DEGRADED
    assert verdict.offline_weight == 0, "degraded health is not an offline vote"
    assert verdict.state is State.WATCHING


def test_verdict_carries_why_it_switched() -> None:
    det, clock = detector(confirm_reads=1, grace_period=0)
    settle(det)
    det.evaluate(Observation(stream=OFFLINE, pubsub_connected=False))
    clock.advance(5)
    verdict = det.evaluate(Observation(stream=OFFLINE, playback_ok=False))

    assert verdict.signal_map()["S2"] == "OFFLINE"
    assert "S2" in verdict.offline_voters() and "S3" in verdict.offline_voters()
    assert "OFFLINE" in verdict.explain() and "S2" in verdict.explain()


def test_pause_and_resume_clear_hysteresis() -> None:
    det, _ = detector()
    settle(det)
    det.evaluate(up(progress_delta=0))
    assert det.zero_delta_runs == 1

    det.pause()
    assert det.evaluate(up()).state is State.PAUSED, "paused ignores every signal"

    det.resume()
    assert det.state is State.IDLE and det.zero_delta_runs == 0


# -------------------------------------------------------------------- pubsub


def _client() -> PubSubClient:
    return PubSubClient(session=None)  # type: ignore[arg-type]


def _message(channel_id: str, payload: dict[str, Any]) -> str:
    return json.dumps({
        "type": "MESSAGE",
        "data": {
            "topic": f"video-playback-by-id.{channel_id}",
            "message": json.dumps(payload),
        },
    })


def test_pubsub_tracks_stream_up_and_down() -> None:
    client = _client()
    client._handle(_message("1", {"type": "stream-up"}))
    assert client.state("1").online is True  # type: ignore[union-attr]

    client._handle(_message("1", {"type": "stream-down"}))
    assert client.state("1").online is False  # type: ignore[union-attr]


def test_pubsub_commercial_sets_a_window_without_going_offline() -> None:
    client = _client()
    client._handle(_message("1", {"type": "stream-up"}))
    client._handle(_message("1", {"type": "commercial", "length": 30}))

    state = client.state("1")
    assert state is not None
    assert state.in_commercial
    assert state.online is True, "an ad break leaves the stream up"


def test_pubsub_viewcount_implies_the_stream_is_up() -> None:
    client = _client()
    client._handle(_message("1", {"type": "viewcount", "viewers": 1234}))
    state = client.state("1")
    assert state is not None and state.online is True and state.viewers == 1234


def test_pubsub_ignores_junk_without_raising() -> None:
    client = _client()
    for raw in (
        "not json",
        json.dumps({"type": "MESSAGE", "data": {"topic": "other.thing"}}),
        json.dumps({"type": "MESSAGE", "data": {"topic": "video-playback-by-id.1",
                                                "message": "not json"}}),
        json.dumps({"type": "PONG"}),
        json.dumps({"type": "MESSAGE", "data": {}}),
    ):
        client._handle(raw)
    assert client.state("1") is None


def test_pubsub_records_topic_rejection() -> None:
    from dropwatch.twitch.pubsub import _Topic

    client = _client()
    client._topics = {"1": _Topic(channel_id="1", nonce="abc")}
    client._handle(json.dumps({"type": "RESPONSE", "nonce": "abc", "error": "ERR_BADAUTH"}))
    assert client._topics["1"].confirmed is False

    client._handle(json.dumps({"type": "RESPONSE", "nonce": "abc", "error": ""}))
    assert client._topics["1"].confirmed is True
