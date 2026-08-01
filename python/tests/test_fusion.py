"""Tests for the fusion engine's temporal behaviour.

These are the tests that matter most in the whole codebase. Every one of
them encodes a way the system could wrongly accuse someone, or wrongly
clear them.
"""

from __future__ import annotations

import pytest

from proctor_fusion import FusionEngine, Severity, load_policy
from proctor_fusion.rules import AutomatedAction, Condition, Policy, Rule
from proctor_protocol import (
    Envelope,
    EnvironmentSignal,
    FaceSignal,
    FrameQualitySignal,
    GazeSignal,
    Heartbeat,
    LockdownEvent,
    LockdownSignal,
    ObjectLabel,
    ObjectSignal,
)

SESSION = "sess-test-0001"


def envelope(payload, seq: int = 0, client_ms: int = 0) -> Envelope:
    return Envelope(
        session_id=SESSION,
        seq=seq,
        ts_client_ms=client_ms,
        ts_monotonic_ms=client_ms,
        payload=payload,
    )


def gaze(on_screen: bool, yaw: float = 0.0, confidence: float = 0.9) -> GazeSignal:
    return GazeSignal(yaw_deg=yaw, pitch_deg=0.0, on_screen=on_screen, confidence=confidence)


@pytest.fixture
def engine() -> FusionEngine:
    eng = FusionEngine(load_policy())
    eng.open_session(SESSION, now_ms=0)
    return eng


def feed(engine: FusionEngine, payload, at_ms: int):
    return engine.on_event(envelope(payload, seq=at_ms, client_ms=at_ms), now_ms=at_ms)


# -- onset ------------------------------------------------------------------


def test_brief_glance_away_does_not_fire(engine):
    """A 1.2s look away is a human glancing at a noise, not cheating."""
    fired = []
    for t in range(0, 1200, 100):
        fired += feed(engine, gaze(on_screen=False), t)
    feed(engine, gaze(on_screen=True), 1300)
    assert fired == []


def test_sustained_look_away_fires_after_onset(engine):
    fired = []
    for t in range(0, 3000, 100):
        fired += feed(engine, gaze(on_screen=False), t)

    assert len(fired) == 1
    violation = fired[0]
    assert violation.rule_id == "gaze_off_screen"
    assert violation.severity is Severity.SOFT
    assert violation.duration_ms >= 2500
    assert violation.requires_human_review is True
    assert violation.action is AutomatedAction.FLAG


def test_violation_carries_evidence(engine):
    fired = []
    for t in range(0, 3000, 100):
        fired += feed(engine, gaze(on_screen=False), t)
    evidence = fired[0].evidence
    assert evidence, "a violation with no evidence cannot be reviewed"
    assert all(s.payload["type"] == "signal.gaze" for s in evidence)


# -- hysteresis -------------------------------------------------------------


def test_blink_does_not_reset_onset_timer(engine):
    """The single most important test here.

    A blink drops iris tracking for a frame or two. If that reset the
    onset timer, a candidate could look away indefinitely and never trip
    the rule, and more importantly the system would be trivially defeated
    by anyone who noticed.
    """
    fired = []
    for t in range(0, 3000, 100):
        # One dropped frame at 1.4s, well inside the 800ms release window.
        looking_away = not (t == 1400)
        fired += feed(engine, gaze(on_screen=not looking_away), t)

    assert len(fired) == 1, "a single blink should not have prevented the violation"


def test_genuine_return_to_screen_resets_timer(engine):
    """The converse: an actual sustained return must clear the state."""
    fired = []
    for t in range(0, 2000, 100):
        fired += feed(engine, gaze(on_screen=False), t)
    # Look back for 1.5s — comfortably past the 800ms release window.
    for t in range(2000, 3500, 100):
        fired += feed(engine, gaze(on_screen=True), t)
    # Then look away again, but only for 2s: not enough on its own.
    for t in range(3500, 5500, 100):
        fired += feed(engine, gaze(on_screen=False), t)

    assert [v for v in fired if not v.resolved] == []


def test_resolution_event_emitted_after_release(engine):
    fired = []
    for t in range(0, 3000, 100):
        fired += feed(engine, gaze(on_screen=False), t)
    opened = fired[0]

    for t in range(3000, 4200, 100):
        fired += feed(engine, gaze(on_screen=True), t)

    resolved = [v for v in fired if v.resolved]
    assert len(resolved) == 1
    assert resolved[0].violation_id == opened.violation_id, "must correlate with the open event"
    assert resolved[0].session_id == SESSION


# -- confidence -------------------------------------------------------------


def test_low_confidence_samples_neither_fire_nor_clear(engine):
    """Low confidence is absence of information, not evidence of innocence."""
    fired = []
    for t in range(0, 2000, 100):
        fired += feed(engine, gaze(on_screen=False), t)
    # Detector loses confidence (glare, hand across face) for 2s.
    for t in range(2000, 4000, 100):
        fired += feed(engine, gaze(on_screen=True, confidence=0.1), t)
    assert fired == [], "low-confidence samples must not fire a violation"

    # Confidence returns, still looking away: now it should fire, using the
    # onset time from before the dropout.
    fired += feed(engine, gaze(on_screen=False), 4100)
    assert len(fired) == 1


# -- cooldown ---------------------------------------------------------------


def test_cooldown_prevents_flag_spam(engine):
    """Two minutes of looking away is one reviewable event, not fifty."""
    fired = []
    for t in range(0, 120_000, 100):
        fired += feed(engine, gaze(on_screen=False), t)
    opened = [v for v in fired if not v.resolved]
    assert len(opened) == 1


# -- hard rules -------------------------------------------------------------


def test_single_frame_phone_false_positive_does_not_flag(engine):
    """One bad frame from the object detector must not accuse anyone."""
    fired = feed(
        engine,
        ObjectSignal(label=ObjectLabel.PHONE, confidence=0.95),
        100,
    )
    assert fired == []


def test_sustained_phone_detection_flags_as_hard(engine):
    fired = []
    for t in range(0, 1500, 100):
        fired += feed(engine, ObjectSignal(label=ObjectLabel.PHONE, confidence=0.95), t)
    assert len(fired) == 1
    assert fired[0].severity is Severity.HARD
    assert fired[0].rule_id == "phone_detected"


def test_low_confidence_phone_detection_ignored(engine):
    fired = []
    for t in range(0, 3000, 100):
        fired += feed(engine, ObjectSignal(label=ObjectLabel.PHONE, confidence=0.4), t)
    assert fired == []


def test_multiple_faces_flags(engine):
    fired = []
    for t in range(0, 3000, 100):
        fired += feed(engine, FaceSignal(face_count=2, confidence=0.9), t)
    assert [v.rule_id for v in fired if not v.resolved] == ["multiple_faces"]


def test_candidate_absent_flags(engine):
    fired = []
    for t in range(0, 5000, 100):
        fired += feed(engine, FaceSignal(face_count=0, confidence=0.9), t)
    assert [v.rule_id for v in fired if not v.resolved] == ["candidate_absent"]


def test_environment_signal_flags_blacklisted_process(engine):
    fired = []
    for t in range(0, 2000, 100):
        fired += feed(
            engine,
            EnvironmentSignal(
                window_focused=True,
                monitor_count=1,
                blacklisted_processes=("obs64.exe",),
            ),
            t,
        )
    assert "blacklisted_process" in {v.rule_id for v in fired}


# -- liveness / silence -----------------------------------------------------


def test_silent_stream_is_a_violation(engine):
    """A client that stops reporting must not look like a well-behaved one."""
    feed(engine, Heartbeat(frames_processed=30, edge_fps=30.0), 0)
    assert engine.on_tick(now_ms=3000) == []

    fired = engine.on_tick(now_ms=6000)
    assert len(fired) == 1
    assert fired[0].rule_id == "stream_silent"
    assert fired[0].severity is Severity.HARD


def test_silence_violation_respects_cooldown(engine):
    feed(engine, Heartbeat(frames_processed=30, edge_fps=30.0), 0)
    first = engine.on_tick(now_ms=6000)
    again = engine.on_tick(now_ms=10_000)
    later = engine.on_tick(now_ms=40_000)
    assert len(first) == 1
    assert again == []
    assert len(later) == 1


def test_closed_session_stops_evaluating(engine):
    engine.close_session(SESSION)
    assert feed(engine, gaze(on_screen=False), 100) == []
    assert engine.on_tick(now_ms=60_000) == []


def test_unknown_session_is_rejected(engine):
    other = Envelope(
        session_id="sess-unknown-1",
        seq=0,
        ts_client_ms=0,
        ts_monotonic_ms=0,
        payload=gaze(on_screen=True),
    )
    with pytest.raises(KeyError):
        engine.on_event(other, now_ms=0)


# -- policy validation ------------------------------------------------------


def test_default_policy_loads_and_is_flag_only():
    policy = load_policy()
    assert policy.rules
    assert all(r.action is AutomatedAction.FLAG for r in policy.rules), (
        "shipped defaults must not automate consequences"
    )
    assert all(r.requires_human_review or r.severity is Severity.INFO for r in policy.rules)


def lockdown(strike: int, event: LockdownEvent = LockdownEvent.FULLSCREEN_EXIT) -> LockdownSignal:
    return LockdownSignal(event=event, strike=strike, allowance=3, confidence=1.0)


def test_strikes_within_the_allowance_raise_nothing(engine):
    """The whole point of an allowance.

    A candidate who hits Escape once in a stressful exam has not cheated,
    and a system that flags them trains reviewers to dismiss flags.
    """
    fired = []
    for strike in (1, 2, 3):
        fired += feed(engine, lockdown(strike), strike * 1000)
    assert fired == []


def test_exceeding_the_allowance_flags_for_review(engine):
    fired = []
    for strike in (1, 2, 3, 4):
        fired += feed(engine, lockdown(strike), strike * 1000)
    assert len(fired) == 1
    assert fired[0].rule_id == "lockdown_strikes_exhausted"
    assert fired[0].severity is Severity.HARD
    assert fired[0].requires_human_review is True


def test_the_lockdown_flag_never_ends_the_exam_itself(engine):
    """Consistent with every other rule shipped here — see § 6."""
    fired = []
    for strike in range(1, 8):
        fired += feed(engine, lockdown(strike), strike * 1000)
    assert all(v.action is AutomatedAction.FLAG for v in fired)


def test_a_briefly_blurred_frame_is_not_flagged(engine):
    """A hand passing the lens, or a moment of refocus. Extremely common,
    and not evidence of anything."""
    fired = []
    for t in range(0, 2000, 200):
        fired += feed(engine, FrameQualitySignal(sharpness=0.01, brightness=0.5), t)
    assert fired == []


def test_a_sustained_unusable_camera_is_reported_softly(engine):
    """Soft, not hard: an unusable camera is overwhelmingly equipment, and
    the value is telling a reviewer 'we could not see' rather than letting
    the degraded signals read as evasion."""
    fired = []
    for t in range(0, 9000, 200):
        fired += feed(engine, FrameQualitySignal(sharpness=0.01, brightness=0.5), t)
    assert len(fired) == 1
    assert fired[0].rule_id == "camera_unusable"
    assert fired[0].severity is Severity.SOFT


def test_an_ordinary_webcam_frame_is_never_flagged_as_unusable(engine):
    fired = []
    for t in range(0, 15000, 200):
        fired += feed(engine, FrameQualitySignal(sharpness=0.4, brightness=0.55), t)
    assert fired == []


def test_hard_rule_with_zero_onset_is_rejected():
    """Guards against someone 'tightening' policy into a false-positive cannon."""
    with pytest.raises(ValueError, match="single frame"):
        Rule(
            id="instant",
            description="fires on one frame",
            signal="signal.object",
            when=Condition(field="label", op="==", value="phone"),
            severity=Severity.HARD,
            onset_ms=0,
        )


def test_the_discrete_escape_hatch_does_not_weaken_the_guard_by_default():
    """`discrete` must be opt-in per rule, never the default.

    The onset guard is the main thing standing between a noisy detector
    and an accusation. If adding the escape hatch had made it default-on,
    every existing rule would have silently lost that protection.
    """
    assert Rule.model_fields["discrete"].default is False
    with pytest.raises(ValueError, match="single frame"):
        Rule(
            id="still-guarded",
            description="an ordinary detector rule",
            signal="signal.object",
            when=Condition(field="label", op="==", value="phone"),
            severity=Severity.HARD,
            onset_ms=0,
            discrete=False,
        )


def test_a_discrete_signal_may_fire_instantly():
    """A keystroke either happened or it did not.

    There is no "sustained Ctrl+C" to confirm, so requiring an onset window
    would mean this rule fires on the second press or never. This is the
    one case the guard is not protecting against.
    """
    rule = Rule(
        id="lockdown",
        description="a discrete shell event",
        signal="signal.lockdown",
        when=Condition(field="strike", op=">", value=3),
        severity=Severity.HARD,
        onset_ms=0,
        discrete=True,
    )
    assert rule.onset_ms == 0
    assert rule.severity is Severity.HARD


def test_lock_exam_without_human_review_is_rejected():
    with pytest.raises(ValueError, match="human review"):
        Rule(
            id="lock",
            description="locks without review",
            signal="signal.object",
            when=Condition(field="label", op="==", value="phone"),
            severity=Severity.HARD,
            onset_ms=1000,
            action=AutomatedAction.LOCK_EXAM,
            requires_human_review=False,
        )


def test_duplicate_rule_ids_rejected():
    rule = Rule(
        id="dup",
        description="x",
        signal="signal.gaze",
        when=Condition(field="on_screen", op="==", value=False),
        severity=Severity.SOFT,
        onset_ms=1000,
    )
    with pytest.raises(ValueError, match="duplicate rule id"):
        Policy(name="bad", rules=[rule, rule])


def test_misconfigured_field_does_not_crash_the_engine():
    """A rule pointing at a nonexistent field must not take down an exam."""
    policy = Policy(
        name="typo",
        rules=[
            Rule(
                id="typo_rule",
                description="references a field that does not exist",
                signal="signal.gaze",
                when=Condition(field="on_scren", op="==", value=False),
                severity=Severity.SOFT,
                onset_ms=1000,
            )
        ],
    )
    eng = FusionEngine(policy)
    eng.open_session(SESSION, now_ms=0)
    fired = []
    for t in range(0, 3000, 100):
        fired += eng.on_event(envelope(gaze(on_screen=False), seq=t), now_ms=t)
    assert fired == []
