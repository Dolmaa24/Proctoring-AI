"""Scripted candidate behaviour, as pure data.

Every scenario is a deterministic list of timestamped observations, which
means the whole system can be exercised without a webcam, a model, or a
volunteer. Scenarios come in two families:

*Behavioural* — what an honest or dishonest candidate looks like to the
edge models. These test whether policy does the right thing.

*Adversarial* — what a tampered client looks like on the wire. These test
whether the transport notices, and they are the ones worth staring at,
because a proctoring system that only handles the cooperative case is
theatre.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from proctor_protocol import (
    AudioSignal,
    EnvironmentSignal,
    FaceSignal,
    GazeSignal,
    HeadPoseSignal,
    Heartbeat,
    LivenessSignal,
    ObjectLabel,
    ObjectSignal,
    Payload,
)

TICK_MS = 100
"""Edge emits a bundle of signals at 10Hz. Real clients run detectors at
30fps but downsample telemetry — sending every frame's landmarks would
defeat the point of doing inference at the edge."""


@dataclass(frozen=True, slots=True)
class ScriptedEvent:
    t_ms: int
    payload: Payload


def _nominal(t_ms: int) -> Iterator[Payload]:
    """What a candidate quietly working looks like."""
    yield GazeSignal(yaw_deg=2.0, pitch_deg=-3.0, on_screen=True, confidence=0.93)
    yield HeadPoseSignal(yaw_deg=3.0, pitch_deg=5.0, roll_deg=1.0, confidence=0.94)
    yield FaceSignal(face_count=1, confidence=0.96)
    if t_ms % 1000 == 0:
        yield LivenessSignal(score=0.93, confidence=0.88)
        yield EnvironmentSignal(window_focused=True, monitor_count=1)
        yield Heartbeat(frames_processed=t_ms // 33, edge_fps=30.0)


def honest(duration_ms: int = 10_000) -> list[ScriptedEvent]:
    """A candidate doing nothing wrong. Must produce zero violations.

    Includes two blinks and a natural glance away, because the real test of
    a proctoring system is not whether it catches cheating — it is whether
    it leaves innocent people alone.
    """
    events: list[ScriptedEvent] = []
    for t in range(0, duration_ms, TICK_MS):
        blinking = t in (2100, 2200, 5600)
        glancing = 4000 <= t < 5200  # 1.2s glance, under the 2.5s threshold
        for payload in _nominal(t):
            if isinstance(payload, GazeSignal):
                if blinking:
                    payload = GazeSignal(
                        yaw_deg=0.0, pitch_deg=0.0, on_screen=True, confidence=0.15
                    )
                elif glancing:
                    payload = GazeSignal(
                        yaw_deg=-22.0, pitch_deg=-5.0, on_screen=False, confidence=0.9
                    )
            events.append(ScriptedEvent(t, payload))
    return events


def sustained_look_away(duration_ms: int = 12_000) -> list[ScriptedEvent]:
    """Off-screen for 5 continuous seconds, with a blink in the middle.

    The blink is the point: it must not reset the onset timer.
    """
    events: list[ScriptedEvent] = []
    for t in range(0, duration_ms, TICK_MS):
        away = 3000 <= t < 8000
        blink = t == 5000
        for payload in _nominal(t):
            if isinstance(payload, GazeSignal) and away:
                payload = GazeSignal(
                    yaw_deg=-41.0,
                    pitch_deg=-8.0,
                    on_screen=False,
                    confidence=0.12 if blink else 0.91,
                )
            events.append(ScriptedEvent(t, payload))
    return events


def phone_in_frame(duration_ms: int = 10_000) -> list[ScriptedEvent]:
    events: list[ScriptedEvent] = []
    for t in range(0, duration_ms, TICK_MS):
        events.extend(ScriptedEvent(t, p) for p in _nominal(t))
        if 4000 <= t < 7000:
            events.append(ScriptedEvent(t, ObjectSignal(label=ObjectLabel.PHONE, confidence=0.91)))
    return events


def phone_false_positive(duration_ms: int = 6_000) -> list[ScriptedEvent]:
    """A single spurious high-confidence phone detection. Must NOT flag."""
    events: list[ScriptedEvent] = []
    for t in range(0, duration_ms, TICK_MS):
        events.extend(ScriptedEvent(t, p) for p in _nominal(t))
        if t == 3000:
            events.append(ScriptedEvent(t, ObjectSignal(label=ObjectLabel.PHONE, confidence=0.94)))
    return events


def candidate_leaves(duration_ms: int = 12_000) -> list[ScriptedEvent]:
    events: list[ScriptedEvent] = []
    for t in range(0, duration_ms, TICK_MS):
        gone = 4000 <= t < 10_000
        if gone:
            events.append(ScriptedEvent(t, FaceSignal(face_count=0, confidence=0.9)))
            if t % 1000 == 0:
                events.append(ScriptedEvent(t, Heartbeat(frames_processed=t // 33, edge_fps=30.0)))
        else:
            events.extend(ScriptedEvent(t, p) for p in _nominal(t))
    return events


def second_person(duration_ms: int = 10_000) -> list[ScriptedEvent]:
    events: list[ScriptedEvent] = []
    for t in range(0, duration_ms, TICK_MS):
        crowded = 3000 <= t < 8000
        for payload in _nominal(t):
            if isinstance(payload, FaceSignal) and crowded:
                payload = FaceSignal(face_count=2, confidence=0.92)
            events.append(ScriptedEvent(t, payload))
    return events


def someone_walks_past(duration_ms: int = 8_000) -> list[ScriptedEvent]:
    """A housemate crosses behind the candidate for 1.5s.

    Under the shipped policy this is below the 2s onset for `multiple_faces`
    and must not flag. Shared housing is not misconduct.
    """
    events: list[ScriptedEvent] = []
    for t in range(0, duration_ms, TICK_MS):
        passing = 3000 <= t < 4500
        for payload in _nominal(t):
            if isinstance(payload, FaceSignal) and passing:
                payload = FaceSignal(face_count=2, confidence=0.9)
            events.append(ScriptedEvent(t, payload))
    return events


def thinking_aloud(duration_ms: int = 10_000) -> list[ScriptedEvent]:
    """Candidate mutters to themselves for 2s. Below the 4s speech threshold."""
    events: list[ScriptedEvent] = []
    for t in range(0, duration_ms, TICK_MS):
        events.extend(ScriptedEvent(t, p) for p in _nominal(t))
        speaking = 3000 <= t < 5000
        if t % 500 == 0:
            events.append(
                ScriptedEvent(
                    t,
                    AudioSignal(
                        speech_active=speaking,
                        energy_db=-28.0 if speaking else -58.0,
                        confidence=0.85,
                    ),
                )
            )
    return events


def conversation(duration_ms: int = 14_000) -> list[ScriptedEvent]:
    """Sustained speech for 6s — long past muttering."""
    events: list[ScriptedEvent] = []
    for t in range(0, duration_ms, TICK_MS):
        events.extend(ScriptedEvent(t, p) for p in _nominal(t))
        speaking = 4000 <= t < 10_000
        if t % 500 == 0:
            events.append(
                ScriptedEvent(
                    t,
                    AudioSignal(
                        speech_active=speaking,
                        energy_db=-22.0 if speaking else -58.0,
                        confidence=0.9,
                    ),
                )
            )
    return events


def poor_lighting(duration_ms: int = 15_000) -> list[ScriptedEvent]:
    """Everything detectable but at low confidence throughout.

    Should produce an INFO-level capture-quality note and nothing punitive.
    A candidate with a bad webcam has not done anything wrong.
    """
    events: list[ScriptedEvent] = []
    for t in range(0, duration_ms, TICK_MS):
        events.append(ScriptedEvent(t, FaceSignal(face_count=1, confidence=0.3)))
        events.append(
            ScriptedEvent(
                t, GazeSignal(yaw_deg=1.0, pitch_deg=0.0, on_screen=True, confidence=0.25)
            )
        )
        if t % 1000 == 0:
            events.append(ScriptedEvent(t, Heartbeat(frames_processed=t // 33, edge_fps=22.0)))
    return events


def screen_sharing(duration_ms: int = 6_000) -> list[ScriptedEvent]:
    events: list[ScriptedEvent] = []
    for t in range(0, duration_ms, TICK_MS):
        events.extend(ScriptedEvent(t, p) for p in _nominal(t))
        if t % 1000 == 0 and t >= 2000:
            events.append(
                ScriptedEvent(
                    t,
                    EnvironmentSignal(
                        window_focused=True,
                        monitor_count=2,
                        blacklisted_processes=("obs64.exe", "AnyDesk"),
                        screen_share_active=True,
                    ),
                )
            )
    return events


BEHAVIOURAL: dict[str, tuple[str, object]] = {
    "honest": ("Candidate working normally; blinks and one brief glance.", honest),
    "look_away": ("Sustained 5s look away with a blink mid-way.", sustained_look_away),
    "phone": ("Phone visible in frame for 3s.", phone_in_frame),
    "phone_blip": ("Single spurious phone detection; must not flag.", phone_false_positive),
    "absent": ("Candidate leaves the camera view for 6s.", candidate_leaves),
    "two_faces": ("A second face present for 5s.", second_person),
    "walk_past": ("Housemate crosses behind for 1.5s; must not flag.", someone_walks_past),
    "mutter": ("Candidate thinks aloud for 2s; must not flag.", thinking_aloud),
    "conversation": ("Sustained 6s of speech.", conversation),
    "dim": ("Poor lighting; low confidence throughout.", poor_lighting),
    "screen_share": ("Screen sharing and remote-desktop tooling running.", screen_sharing),
}
"""Scenarios expected to produce (or conspicuously not produce) violations."""
