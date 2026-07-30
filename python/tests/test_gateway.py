"""End-to-end tests through the real gateway: HTTP enrolment, WS ingest, fan-out.

Uses Starlette's TestClient so the WebSocket path, the signature check, the
integrity checks and the fusion engine all run for real. Nothing here is
mocked except the passage of time.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from proctor_gateway import Settings, create_app
from proctor_sim import BEHAVIOURAL, SimulatedClient, Tamper

MASTER = b"test-master-secret-not-for-production"
CONSOLE_TOKEN = "test-console-token"
CONSOLE_PROTOCOLS = ["proctor.console.v1", f"token.{CONSOLE_TOKEN}"]


class FakeClock:
    """Server clock the harness advances in lockstep with scripted time.

    Lets a 15-second exam run through the real gateway in milliseconds while
    the skew detector still sees a plausible relationship between client and
    server elapsed time.
    """

    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self.now = start_ms

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(clock: FakeClock) -> TestClient:
    settings = Settings(
        master_secret=MASTER,
        clock=clock,
        tick_interval_ms=50,
        console_token=CONSOLE_TOKEN,
    )
    return TestClient(create_app(settings))


def enrol(client: TestClient) -> tuple[str, str]:
    response = client.post(
        "/v1/sessions", json={"exam_id": "exam-1", "candidate_ref": "cand-ref-1"}
    )
    assert response.status_code == 201
    body = response.json()
    return body["session_id"], body["session_key_b64"]


def run_scenario(
    client: TestClient,
    clock: FakeClock,
    scenario: str,
    tamper: Tamper = Tamper.NONE,
) -> list[dict]:
    """Drive one scenario through the gateway; return violations the proctor saw."""
    session_id, key_b64 = enrol(client)
    sim = SimulatedClient.from_enrolment(session_id, key_b64, tamper=tamper)
    script = BEHAVIOURAL[scenario][1]()
    base = clock.now

    violations: list[dict] = []
    with client.websocket_connect("/v1/proctor/stream", subprotocols=CONSOLE_PROTOCOLS) as proctor:
        hello = proctor.receive_json()
        assert hello["kind"] == "hello"

        with client.websocket_connect(f"/v1/sessions/{session_id}/telemetry") as telemetry:
            for t_ms, frame in sim.frames(script):
                # Server time advances with scripted time, as it would in a
                # real run. Tampered clocks then diverge from it exactly as
                # they would in production.
                clock.now = base + t_ms
                telemetry.send_text(frame)

        # The gateway publishes inline on the ingest path and emits
        # `stream_disconnected` when the telemetry socket closes, so that
        # message is a reliable end-of-stream marker: everything this run
        # produced is already queued ahead of it.
        for _ in range(5_000):
            message = proctor.receive_json(mode="text")
            if message.get("kind") == "violation":
                violations.append(message)
            elif message.get("kind") == "stream_disconnected":
                break
        else:
            raise AssertionError("never saw stream_disconnected")
    return violations


def rule_ids(violations: list[dict]) -> set[str]:
    return {v["rule_id"] for v in violations if not v["resolved"]}


# -- plumbing ---------------------------------------------------------------


def test_health(client, clock):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["rules"] > 0


def test_enrolment_returns_distinct_keys(client, clock):
    _, key_a = enrol(client)
    _, key_b = enrol(client)
    assert key_a != key_b, "session keys must not be reused across sessions"


def test_unknown_session_rejected(client, clock):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/sessions/sess-nope/telemetry") as ws:
            ws.send_text("{}")
            ws.receive_text()


def test_session_status_reports_progress(client, clock):
    session_id, key_b64 = enrol(client)
    sim = SimulatedClient.from_enrolment(session_id, key_b64)
    script = BEHAVIOURAL["honest"][1](3_000)
    base = clock.now
    with client.websocket_connect(f"/v1/sessions/{session_id}/telemetry") as ws:
        for t_ms, frame in sim.frames(script):
            clock.now = base + t_ms
            ws.send_text(frame)

    status = client.get(f"/v1/sessions/{session_id}").json()
    assert status["events_received"] > 0
    assert status["last_seq"] == status["events_received"] - 1
    assert status["integrity_breaches"] == []


# -- behavioural: the system must leave innocent people alone ---------------


@pytest.mark.parametrize(
    "scenario",
    ["honest", "phone_blip", "walk_past", "mutter"],
    ids=[
        "working-normally",
        "single-frame-detector-false-positive",
        "housemate-walks-past",
        "candidate-mutters-while-thinking",
    ],
)
def test_innocent_behaviour_produces_no_violations(client, clock, scenario):
    """The most important test file in the repo, and this is the top of it.

    Each of these is something a candidate does that is not misconduct. A
    system that flags them is worse than no system, because every false
    flag lands on a real person.
    """
    violations = run_scenario(client, clock, scenario)
    punitive = [v for v in violations if v["severity"] != "info"]
    assert punitive == [], f"{scenario} wrongly flagged: {rule_ids(violations)}"


def test_poor_lighting_is_informational_only(client, clock):
    violations = run_scenario(client, clock, "dim")
    assert all(v["severity"] == "info" for v in violations), "a bad webcam is not misconduct"


# -- behavioural: the system must catch what it claims to -------------------


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("look_away", "gaze_off_screen"),
        ("phone", "phone_detected"),
        ("absent", "candidate_absent"),
        ("two_faces", "multiple_faces"),
        ("conversation", "sustained_speech"),
        ("screen_share", "blacklisted_process"),
    ],
)
def test_suspicious_behaviour_is_flagged(client, clock, scenario, expected):
    violations = run_scenario(client, clock, scenario)
    assert expected in rule_ids(violations)


def test_every_flag_requires_human_review(client, clock):
    """No rule shipped in this repo may act on its own."""
    for scenario in ("look_away", "phone", "absent", "two_faces"):
        for violation in run_scenario(client, clock, scenario):
            if violation["severity"] == "info":
                continue
            assert violation["requires_human_review"] is True
            assert violation["action"] == "flag", (
                f"{violation['rule_id']} would take automated action"
            )


def test_flagged_violations_carry_evidence(client, clock):
    violations = [v for v in run_scenario(client, clock, "phone") if not v["resolved"]]
    assert violations
    assert all(v["evidence"] for v in violations), (
        "a flag a human cannot review is just an accusation"
    )


# -- console evidence lookup -------------------------------------------------
#
# The board snapshot never carries raw evidence (see triage.TimelineEntry) —
# only a count. These tests exercise the dedicated per-violation endpoint
# the console calls when a proctor actually opens a flag.


def test_board_snapshot_carries_a_count_not_the_samples(clock, tmp_path):
    settings = Settings(
        master_secret=MASTER,
        clock=clock,
        console_token=CONSOLE_TOKEN,
        db_path=str(tmp_path / "evidence.db"),
    )
    with TestClient(create_app(settings)) as client:
        run_scenario_settings(client, clock, "phone")
        board = client.get(
            "/v1/proctor/sessions", headers={"authorization": f"Bearer {CONSOLE_TOKEN}"}
        ).json()["sessions"]

    entry = next(e for e in board[0]["timeline"] if e["rule_id"] == "phone_detected")
    assert entry["evidence_count"] > 0
    assert "evidence" not in entry, "raw samples must not ride along in the snapshot"


def test_violation_evidence_is_fetchable_by_reference(clock, tmp_path):
    settings = Settings(
        master_secret=MASTER,
        clock=clock,
        console_token=CONSOLE_TOKEN,
        db_path=str(tmp_path / "evidence2.db"),
    )
    with TestClient(create_app(settings)) as client:
        session_id = run_scenario_settings(client, clock, "phone")
        board = client.get(
            "/v1/proctor/sessions", headers={"authorization": f"Bearer {CONSOLE_TOKEN}"}
        ).json()["sessions"]
        entry = next(e for e in board[0]["timeline"] if e["rule_id"] == "phone_detected")

        response = client.get(
            f"/v1/proctor/sessions/{session_id}/violations/{entry['violation_id']}",
            headers={"authorization": f"Bearer {CONSOLE_TOKEN}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body["evidence"]) == entry["evidence_count"], (
        "the count in the snapshot must match the full record's actual length"
    )
    # Evidence is the session's whole recent-signal window, not filtered to
    # the firing rule's own signal type — see FusionEngine.on_event — so
    # this checks shape rather than assuming which signal comes first.
    assert all("type" in item["payload"] for item in body["evidence"])
    assert any(item["payload"]["type"] == "signal.object" for item in body["evidence"]), (
        "the phone detection itself must be somewhere in the window"
    )


def test_violation_evidence_requires_the_console_token(clock, tmp_path):
    settings = Settings(
        master_secret=MASTER,
        clock=clock,
        console_token=CONSOLE_TOKEN,
        db_path=str(tmp_path / "evidence3.db"),
    )
    with TestClient(create_app(settings)) as client:
        session_id = run_scenario_settings(client, clock, "phone")
        response = client.get(f"/v1/proctor/sessions/{session_id}/violations/whatever")
    assert response.status_code == 401


def test_unknown_violation_id_is_404(client, clock):
    session_id, _ = enrol(client)
    response = client.get(
        f"/v1/proctor/sessions/{session_id}/violations/nope",
        headers={"authorization": f"Bearer {CONSOLE_TOKEN}"},
    )
    assert response.status_code == 404


def test_a_violation_from_another_session_is_not_returned(clock, tmp_path):
    """The session_id in the path is a real check, not decoration."""
    settings = Settings(
        master_secret=MASTER,
        clock=clock,
        console_token=CONSOLE_TOKEN,
        db_path=str(tmp_path / "evidence4.db"),
    )
    with TestClient(create_app(settings)) as client:
        run_scenario_settings(client, clock, "phone")
        board = client.get(
            "/v1/proctor/sessions", headers={"authorization": f"Bearer {CONSOLE_TOKEN}"}
        ).json()["sessions"]
        entry = next(e for e in board[0]["timeline"] if e["rule_id"] == "phone_detected")

        other_session, _ = enrol(client)
        response = client.get(
            f"/v1/proctor/sessions/{other_session}/violations/{entry['violation_id']}",
            headers={"authorization": f"Bearer {CONSOLE_TOKEN}"},
        )
    assert response.status_code == 404


def run_scenario_settings(client: TestClient, clock: FakeClock, scenario: str) -> str:
    """Like `run_scenario`, but returns the session_id instead of draining
    violations off the WS — these tests read the board and store directly."""
    session_id, key_b64 = enrol(client)
    sim = SimulatedClient.from_enrolment(session_id, key_b64)
    script = BEHAVIOURAL[scenario][1]()
    base = clock.now
    with client.websocket_connect(f"/v1/sessions/{session_id}/telemetry") as ws:
        for t_ms, frame in sim.frames(script):
            clock.now = base + t_ms
            ws.send_text(frame)
    return session_id


# -- adversarial: a hostile client ------------------------------------------


def test_forged_signature_is_rejected(client, clock):
    violations = run_scenario(client, clock, "phone", tamper=Tamper.FORGE_SIGNATURE)
    assert "stream_bad_signature" in rule_ids(violations)


def test_dropped_events_leave_a_visible_gap(client, clock):
    """The cheapest attack: drop the telemetry covering the phone use."""
    violations = run_scenario(client, clock, "phone", tamper=Tamper.DROP_EVENTS)
    assert "stream_sequence_gap" in rule_ids(violations)


def test_a_gap_does_not_blind_the_rest_of_the_exam(client, clock):
    """Evaluation must resynchronise after a gap, not stop.

    Found in a live run. `last_seq` was not advanced past a gap, so every
    later frame also read as a gap and was discarded unevaluated — meaning
    one dropped event near the start left the candidate effectively
    unproctored for the remainder, while raising only a rate-limited
    trickle of identical gap flags.
    """
    session_id, key_b64 = enrol(client)
    sim = SimulatedClient.from_enrolment(session_id, key_b64)
    base = clock.now
    frames = list(sim.frames(BEHAVIOURAL["phone"][1]()))

    seen: set[str] = set()
    with client.websocket_connect("/v1/proctor/stream", subprotocols=CONSOLE_PROTOCOLS) as proctor:
        proctor.receive_json()
        with client.websocket_connect(f"/v1/sessions/{session_id}/telemetry") as ws:
            for index, (t_ms, frame) in enumerate(frames):
                # Drop a single early frame, long before the phone appears.
                if index == 3:
                    continue
                clock.now = base + t_ms
                ws.send_text(frame)
        for _ in range(5_000):
            message = proctor.receive_json(mode="text")
            if message.get("kind") == "violation":
                seen.add(message["rule_id"])
            elif message.get("kind") == "stream_disconnected":
                break

    assert "stream_sequence_gap" in seen, "the gap itself must still be flagged"
    assert "phone_detected" in seen, (
        "one dropped frame must not disable proctoring for the rest of the exam"
    )


def test_replayed_frames_are_rejected(client, clock):
    """Re-sending an old 'all clear' frame must not clear a real violation."""
    violations = run_scenario(client, clock, "phone", tamper=Tamper.REPLAY)
    assert "stream_replay" in rule_ids(violations)


def test_sequence_stall_is_rejected(client, clock):
    violations = run_scenario(client, clock, "phone", tamper=Tamper.SEQUENCE_STALL)
    assert "stream_replay" in rule_ids(violations)


def test_clock_manipulation_is_detected(client, clock):
    """Client rewinds its clock to make a long violation look brief."""
    violations = run_scenario(client, clock, "look_away", tamper=Tamper.CLOCK_SKEW)
    assert "stream_clock_skew" in rule_ids(violations)


def test_clock_manipulation_cannot_hide_misconduct_silently(client, clock):
    """Rewinding the clock trades a soft flag for a hard one — a bad trade.

    Rules are timed on the client's monotonic counter, so a client that
    rewinds it genuinely does shrink the measured violation. What it cannot
    do is stay quiet about it: the rewind itself raises a HARD integrity
    breach. The candidate swaps a `gaze_off_screen` soft flag for a
    `stream_clock_skew` hard one plus a session marked untrustworthy.

    This is the security property that actually holds. Asserting the
    stronger-sounding "the original violation still fires" would be a lie
    about a system timed on a clock the candidate controls.
    """
    violations = run_scenario(client, clock, "look_away", tamper=Tamper.CLOCK_SKEW)
    fired = rule_ids(violations)
    assert "stream_clock_skew" in fired
    hard = [v for v in violations if v["severity"] == "hard" and not v["resolved"]]
    assert hard, "clock manipulation must produce a hard, human-reviewed flag"
    assert all(v["requires_human_review"] for v in hard)


def test_monotonic_rewind_is_caught_regardless_of_size(client, clock):
    """A rewind smaller than the skew tolerance must still be detected.

    Before the monotonicity check existed, tolerance acted as an
    undetectable-cheating budget: any violation shorter than
    `clock_skew_tolerance_ms` could be erased by a sub-tolerance rewind
    without tripping anything. The shortest onset in the shipped policy is
    800ms, well inside a 2000ms tolerance, so this was reachable.
    """
    session_id, key_b64 = enrol(client)
    sim = SimulatedClient.from_enrolment(session_id, key_b64)
    script = BEHAVIOURAL["honest"][1](3_000)
    base = clock.now

    frames = list(sim.frames(script))
    with client.websocket_connect("/v1/proctor/stream", subprotocols=CONSOLE_PROTOCOLS) as proctor:
        proctor.receive_json()
        with client.websocket_connect(f"/v1/sessions/{session_id}/telemetry") as telemetry:
            for t_ms, frame in frames:
                clock.now = base + t_ms
                telemetry.send_text(frame)

            # Replay an envelope from 500ms earlier under a fresh sequence
            # number: a rewind far below the 2000ms tolerance.
            rewound = SimulatedClient(session_id=sim.session_id, session_key=sim.session_key)
            late = list(rewound.frames(BEHAVIOURAL["honest"][1](3_000)))
            clock.now = base + 3_500
            telemetry.send_text(late[-6][1])

        seen = set()
        for _ in range(5_000):
            message = proctor.receive_json(mode="text")
            if message.get("kind") == "violation":
                seen.add(message["rule_id"])
            elif message.get("kind") == "stream_disconnected":
                break

    assert "stream_clock_skew" in seen or "stream_replay" in seen


def test_replay_does_not_clear_an_active_violation(client, clock):
    """Specifically: a replayed 'face present' must not resolve an absence."""
    violations = run_scenario(client, clock, "absent", tamper=Tamper.REPLAY)
    assert "candidate_absent" in rule_ids(violations) or "stream_replay" in rule_ids(violations)


def test_clean_session_end_is_not_treated_as_abandonment(client, clock):
    violations = run_scenario(client, clock, "honest")
    assert "stream_abandoned" not in rule_ids(violations)
    assert "stream_silent" not in rule_ids(violations)


def test_killing_the_client_raises_abandonment(client, clock):
    """Closing the socket must not be cheaper than falling silent.

    Otherwise the cheapest way to stop being proctored is to quit the app.
    """
    session_id, key_b64 = enrol(client)
    sim = SimulatedClient.from_enrolment(session_id, key_b64)
    sim.end_cleanly = False
    base = clock.now

    seen: set[str] = set()
    with client.websocket_connect("/v1/proctor/stream", subprotocols=CONSOLE_PROTOCOLS) as proctor:
        proctor.receive_json()
        with client.websocket_connect(f"/v1/sessions/{session_id}/telemetry") as ws:
            for t_ms, frame in sim.frames(BEHAVIOURAL["honest"][1](2_000)):
                clock.now = base + t_ms
                ws.send_text(frame)
        for _ in range(5_000):
            message = proctor.receive_json(mode="text")
            if message.get("kind") == "violation":
                seen.add(message["rule_id"])
            elif message.get("kind") == "stream_disconnected":
                assert message["ended_cleanly"] is False
                break

    assert "stream_abandoned" in seen


def test_finished_sessions_stop_generating_silence_flags(client, clock):
    """Found in a live run: abandoned sessions ticked silence flags forever.

    Every completed exam kept firing `stream_silent` on the wall-clock tick
    and its state was never released — console noise plus an unbounded
    accumulation of sessions in the engine.
    """
    engine = client.app.state.engine
    session_id, key_b64 = enrol(client)
    sim = SimulatedClient.from_enrolment(session_id, key_b64)
    base = clock.now
    with client.websocket_connect(f"/v1/sessions/{session_id}/telemetry") as ws:
        for t_ms, frame in sim.frames(BEHAVIOURAL["honest"][1](2_000)):
            clock.now = base + t_ms
            ws.send_text(frame)

    # Long after the client has gone, ticking must produce nothing.
    assert engine.on_tick(base + 600_000) == []


def test_repeated_breaches_are_rate_limited_but_fully_counted(client, clock):
    """A persistent breach must not bury the proctor console.

    Found by running the simulator against a live gateway rather than the
    test harness: a skewed clock raised `stream_clock_skew` on every single
    frame, producing hundreds of identical rows. Under that volume the one
    flag that mattered — a real phone detection — was unfindable, which
    makes this a safety bug rather than a cosmetic one.
    """
    violations = run_scenario(client, clock, "phone", tamper=Tamper.FORGE_SIGNATURE)
    skew_rows = [v for v in violations if v["rule_id"] == "stream_bad_signature"]
    assert skew_rows, "the breach must still be reported at least once"
    assert len(skew_rows) <= 5, f"{len(skew_rows)} rows for one persistent breach is console spam"


def test_breach_counts_survive_rate_limiting(client, clock):
    session_id, key_b64 = enrol(client)
    sim = SimulatedClient.from_enrolment(session_id, key_b64, tamper=Tamper.FORGE_SIGNATURE)
    base = clock.now
    with client.websocket_connect(f"/v1/sessions/{session_id}/telemetry") as ws:
        for t_ms, frame in sim.frames(BEHAVIOURAL["phone"][1]()):
            clock.now = base + t_ms
            ws.send_text(frame)

    status = client.get(f"/v1/sessions/{session_id}").json()
    counts = status["breach_counts"]
    assert counts.get("stream_bad_signature", 0) > 5, (
        "suppressed broadcasts must still be counted for review"
    )
    assert len(status["integrity_breaches"]) <= 51, (
        "the durable breach log must be bounded; a hostile client controls its rate"
    )


def test_integrity_breaches_are_recorded_on_the_session(client, clock):
    session_id, key_b64 = enrol(client)
    sim = SimulatedClient.from_enrolment(session_id, key_b64, tamper=Tamper.FORGE_SIGNATURE)
    with client.websocket_connect(f"/v1/sessions/{session_id}/telemetry") as ws:
        for _t, frame in sim.frames(BEHAVIOURAL["phone"][1]()):
            ws.send_text(frame)

    status = client.get(f"/v1/sessions/{session_id}").json()
    assert status["integrity_breaches"], "breaches must be durable, not just broadcast"


# -- proctor authentication -------------------------------------------------


def test_proctor_stream_rejects_a_missing_token(client, clock):
    """The stream carries every candidate's flags; it must not be open.

    Without this the port is a live feed of who is being accused of what,
    readable by anything that can reach it.
    """
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/proctor/stream") as ws:
            ws.receive_json()


def test_proctor_stream_rejects_a_wrong_token(client, clock):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/v1/proctor/stream",
            subprotocols=["proctor.console.v1", "token.not-the-right-token"],
        ) as ws:
            ws.receive_json()


def test_proctor_sessions_endpoint_requires_a_token(client, clock):
    assert client.get("/v1/proctor/sessions").status_code == 401
    assert (
        client.get("/v1/proctor/sessions", headers={"authorization": "Bearer wrong"}).status_code
        == 401
    )
    ok = client.get("/v1/proctor/sessions", headers={"authorization": f"Bearer {CONSOLE_TOKEN}"})
    assert ok.status_code == 200
    assert "sessions" in ok.json()


def test_an_empty_configured_token_does_not_authorise(clock):
    """Fail closed. An unset token must never mean 'allow everyone'."""
    settings = Settings(master_secret=MASTER, clock=clock, console_token="")
    blank = TestClient(create_app(settings))
    assert blank.get("/v1/proctor/sessions").status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with blank.websocket_connect(
            "/v1/proctor/stream", subprotocols=["proctor.console.v1", "token."]
        ) as ws:
            ws.receive_json()


# -- triage surface ---------------------------------------------------------


def test_triage_reflects_a_flagged_session(client, clock):
    run_scenario(client, clock, "phone")
    body = client.get(
        "/v1/proctor/sessions", headers={"authorization": f"Bearer {CONSOLE_TOKEN}"}
    ).json()
    assert body["sessions"]
    flagged = body["sessions"][0]
    assert flagged["band"] in {"notice", "review"}
    assert flagged["timeline"], "a flagged session must carry a reviewable timeline"


def test_triage_leaves_an_honest_session_quiet(client, clock):
    run_scenario(client, clock, "honest")
    body = client.get(
        "/v1/proctor/sessions", headers={"authorization": f"Bearer {CONSOLE_TOKEN}"}
    ).json()
    assert all(s["band"] == "quiet" for s in body["sessions"])


def test_hello_frame_carries_the_current_board(client, clock):
    """A console joining mid-exam must not start blind."""
    run_scenario(client, clock, "phone")
    with client.websocket_connect("/v1/proctor/stream", subprotocols=CONSOLE_PROTOCOLS) as ws:
        hello = ws.receive_json()
    assert hello["kind"] == "hello"
    assert hello["sessions"], "hello must include a snapshot, not just live updates"
