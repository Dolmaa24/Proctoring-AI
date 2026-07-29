"""Tests for triage ordering and score decay.

The thing under test is what floats to the top of an invigilator's screen.
Getting it wrong does not crash anything — it quietly directs human
attention at the wrong person, which is a worse failure than a stack trace.
"""

from __future__ import annotations

from proctor_gateway.triage import (
    SCORE_HALF_LIFE_MS,
    Band,
    TriageBoard,
)

SESSION = "sess-triage-0001"
OTHER = "sess-triage-0002"


def violation(
    session_id: str = SESSION,
    rule_id: str = "gaze_off_screen",
    severity: str = "soft",
    violation_id: str = "v1",
    resolved: bool = False,
    duration_ms: int = 2500,
) -> dict:
    return {
        "session_id": session_id,
        "rule_id": rule_id,
        "severity": severity,
        "message": f"{rule_id} fired",
        "duration_ms": duration_ms,
        "violation_id": violation_id,
        "resolved": resolved,
    }


# -- decay ------------------------------------------------------------------


def test_a_clean_candidate_cools_down():
    """The point of decay: one bad moment must not pin someone all exam."""
    board = TriageBoard()
    board.record_violation(violation(severity="hard"), now_ms=0)
    session = board.get(SESSION)

    hot = session.score(0)
    assert hot > 0

    # One half-life later, exactly half.
    assert session.score(SCORE_HALF_LIFE_MS) == hot / 2

    # Twenty minutes of clean behaviour and they are effectively quiet.
    assert session.band(20 * 60_000) is Band.QUIET


def test_score_never_goes_negative_or_nan():
    board = TriageBoard()
    session = board.ensure(SESSION, now_ms=0)
    assert session.score(0) == 0.0
    assert session.score(10**9) == 0.0


def test_repeated_flags_accumulate_rather_than_reset():
    board = TriageBoard()
    board.record_violation(violation(violation_id="a"), now_ms=0)
    first = board.get(SESSION).score(0)
    board.record_violation(violation(violation_id="b"), now_ms=1000)
    second = board.get(SESSION).score(1000)
    assert second > first


def test_score_is_bounded():
    board = TriageBoard()
    for i in range(500):
        board.record_violation(violation(severity="hard", violation_id=f"v{i}"), now_ms=i)
    assert board.get(SESSION).score(500) <= 100.0


# -- weighting --------------------------------------------------------------


def test_hard_outranks_soft():
    board = TriageBoard()
    board.record_violation(violation(session_id=SESSION, severity="soft"), now_ms=0)
    board.record_violation(violation(session_id=OTHER, severity="hard"), now_ms=0)
    order = [s.session_id for s in board.ordered(0)]
    assert order[0] == OTHER


def test_info_does_not_affect_ordering():
    """A bad webcam must not push a candidate up the review queue."""
    board = TriageBoard()
    for i in range(10):
        board.record_violation(
            violation(severity="info", rule_id="poor_capture_conditions", violation_id=f"i{i}"),
            now_ms=i * 100,
        )
    session = board.get(SESSION)
    assert session.score(1000) == 0.0
    assert session.band(1000) is Band.QUIET


def test_bands_are_coarse_not_a_percentage():
    """The console renders the band; the raw score is for ordering only."""
    board = TriageBoard()
    session = board.ensure(SESSION, now_ms=0)
    assert session.band(0) is Band.QUIET

    board.record_violation(violation(severity="soft"), now_ms=0)
    assert board.get(SESSION).band(0) is Band.NOTICE

    for i in range(3):
        board.record_violation(violation(severity="hard", violation_id=f"h{i}"), now_ms=0)
    assert board.get(SESSION).band(0) is Band.REVIEW


# -- open violations and timeline -------------------------------------------


def test_resolution_closes_the_open_flag_but_keeps_the_history():
    board = TriageBoard()
    board.record_violation(violation(violation_id="v9"), now_ms=0)
    assert len(board.get(SESSION).open_violations) == 1

    board.record_violation(violation(violation_id="v9", resolved=True), now_ms=5000)
    session = board.get(SESSION)
    assert session.open_violations == {}
    assert len(session.timeline) == 2, "the resolved event must stay in the audit trail"


def test_resolution_does_not_reduce_the_score():
    """Stopping is not exoneration; it is what the timeline is for."""
    board = TriageBoard()
    board.record_violation(violation(violation_id="v1", severity="hard"), now_ms=0)
    before = board.get(SESSION).score(0)
    board.record_violation(violation(violation_id="v1", severity="hard", resolved=True), now_ms=0)
    assert board.get(SESSION).score(0) == before


def test_timeline_is_newest_first_and_bounded():
    board = TriageBoard()
    for i in range(300):
        board.record_violation(violation(violation_id=f"v{i}"), now_ms=i * 10)
    view = board.get(SESSION).as_dict(3000)
    assert len(view["timeline"]) <= 200
    assert view["timeline"][0]["violation_id"] == "v299"


# -- ordering ---------------------------------------------------------------


def test_disconnected_sessions_outrank_quiet_connected_ones():
    """An unattended session is the state most likely to need a human."""
    board = TriageBoard()
    board.ensure(SESSION, now_ms=0, connected=True)
    board.ensure(OTHER, now_ms=0, connected=False)
    order = [s.session_id for s in board.ordered(0)]
    assert order[0] == OTHER


def test_ordering_is_stable_for_equal_scores():
    board = TriageBoard()
    board.ensure("sess-b", now_ms=0, connected=True)
    board.ensure("sess-a", now_ms=0, connected=True)
    assert [s.session_id for s in board.ordered(0)] == ["sess-a", "sess-b"]


def test_a_hot_session_sorts_above_a_cooled_one():
    board = TriageBoard()
    board.record_violation(violation(session_id=SESSION, severity="hard"), now_ms=0)
    board.record_violation(
        violation(session_id=OTHER, severity="hard"), now_ms=SCORE_HALF_LIFE_MS * 4
    )
    now = SCORE_HALF_LIFE_MS * 4
    assert [s.session_id for s in board.ordered(now)][0] == OTHER


def test_snapshot_shape_is_serialisable():
    board = TriageBoard()
    board.ensure(SESSION, now_ms=0, exam_id="exam-1", candidate_ref="cand-1", connected=True)
    board.record_violation(violation(), now_ms=100)
    snapshot = board.snapshot(200)
    assert snapshot[0]["session_id"] == SESSION
    assert snapshot[0]["exam_id"] == "exam-1"
    assert snapshot[0]["band"] in {"quiet", "notice", "review"}
    assert isinstance(snapshot[0]["open_violations"], list)


def test_unknown_severity_does_not_crash_ordering():
    board = TriageBoard()
    board.record_violation(violation(severity="catastrophic"), now_ms=0)
    assert board.get(SESSION).score(0) == 0.0
