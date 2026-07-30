"""Restart survival, and the security property that depends on it.

The headline test here is not "the board looks the same afterwards". It is
that a gateway restart cannot be used to launder a replay: `last_seq` is
what makes replay detection work, and if it lived only in memory, bouncing
the process would reset it to -1 and let a client re-send its entire
earlier stream unchallenged.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from proctor_gateway import Settings, create_app
from proctor_gateway.store import SqliteStore, open_store
from proctor_sim import BEHAVIOURAL, SimulatedClient

MASTER = b"persistence-master-secret"
CONSOLE_TOKEN = "persistence-console-token"
CONSOLE_PROTOCOLS = ["proctor.console.v1", f"token.{CONSOLE_TOKEN}"]
DAY_MS = 86_400_000


class FakeClock:
    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self.now = start_ms

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "proctor.db")


def build(db_path: str, clock: FakeClock) -> TestClient:
    """A fresh gateway process against the same database file."""
    return TestClient(
        create_app(
            Settings(
                master_secret=MASTER,
                clock=clock,
                tick_interval_ms=50,
                console_token=CONSOLE_TOKEN,
                db_path=db_path,
            )
        )
    )


def enrol(client: TestClient) -> tuple[str, str]:
    response = client.post("/v1/sessions", json={"exam_id": "exam-1", "candidate_ref": "cand-1"})
    assert response.status_code == 201
    return response.json()["session_id"], response.json()["session_key_b64"]


def drive(client: TestClient, clock: FakeClock, session_id: str, key_b64: str, scenario: str):
    """Run a scenario and return the frames that were sent."""
    sim = SimulatedClient.from_enrolment(session_id, key_b64)
    base = clock.now
    frames = list(sim.frames(BEHAVIOURAL[scenario][1]()))
    with client.websocket_connect(f"/v1/sessions/{session_id}/telemetry") as ws:
        for t_ms, frame in frames:
            clock.now = base + t_ms
            ws.send_text(frame)
    return frames


def board(client: TestClient) -> list[dict]:
    response = client.get(
        "/v1/proctor/sessions", headers={"authorization": f"Bearer {CONSOLE_TOKEN}"}
    )
    assert response.status_code == 200
    return response.json()["sessions"]


# -- the security property --------------------------------------------------


def test_restart_does_not_launder_a_replay(db_path):
    """The reason this module exists.

    A client streams an exam, the gateway restarts, and the client re-sends
    frames it has already used. Without persisted sequence state the new
    process starts at last_seq=-1 and accepts every one of them, so a
    restart becomes a free replay window — and replaying "face present,
    looking at the screen" is exactly what a candidate would want to do.
    """
    clock = FakeClock()
    with build(db_path, clock) as first:
        session_id, key_b64 = enrol(first)
        frames = drive(first, clock, session_id, key_b64, "honest")
        before = first.get(f"/v1/sessions/{session_id}").json()

    assert before["last_seq"] > 10

    with build(db_path, clock) as second:
        after = second.get(f"/v1/sessions/{session_id}").json()
        assert after["last_seq"] == before["last_seq"], "sequence state must survive"

        # Replay a frame from the middle of the original stream.
        replayed = frames[len(frames) // 2][1]
        with second.websocket_connect(f"/v1/sessions/{session_id}/telemetry") as ws:
            ws.send_text(replayed)

        status = second.get(f"/v1/sessions/{session_id}").json()

    assert status["breach_counts"].get("stream_replay", 0) >= 1, (
        "a replayed frame after a restart must still be rejected"
    )


def test_restart_preserves_the_monotonic_clock_floor(db_path):
    """The other half: a rewind must stay detectable across a restart."""
    clock = FakeClock()
    with build(db_path, clock) as first:
        session_id, key_b64 = enrol(first)
        drive(first, clock, session_id, key_b64, "honest")

    with build(db_path, clock) as second:
        sim = SimulatedClient.from_enrolment(session_id, key_b64)
        # A fresh client restarting its monotonic counter at zero, with a
        # sequence number beyond the stored high-water mark.
        rewound = list(sim.frames(BEHAVIOURAL["honest"][1](500)))[-1][1]
        with second.websocket_connect(f"/v1/sessions/{session_id}/telemetry") as ws:
            ws.send_text(rewound)
        status = second.get(f"/v1/sessions/{session_id}").json()

    breaches = status["breach_counts"]
    assert breaches, "a rewound counter after a restart must raise something"


# -- board survival ---------------------------------------------------------


def test_the_board_survives_a_restart(db_path):
    clock = FakeClock()
    with build(db_path, clock) as first:
        session_id, key_b64 = enrol(first)
        drive(first, clock, session_id, key_b64, "phone")
        original = board(first)

    assert original, "precondition: the first run produced a board"
    assert original[0]["band"] in {"notice", "review"}

    with build(db_path, clock) as second:
        restored = board(second)

    assert len(restored) == len(original)
    assert restored[0]["session_id"] == original[0]["session_id"]
    assert restored[0]["candidate_ref"] == "cand-1"
    assert restored[0]["timeline"], "the audit trail must survive"
    assert restored[0]["band"] == original[0]["band"]


def test_restored_sessions_are_never_marked_connected(db_path):
    """No client is attached after a restart, and the board must not lie.

    Showing a proctor a live candidate who is not there is bad on its own;
    worse, a session restored as connected would have the silence rule
    evaluated against a stream that does not exist.
    """
    clock = FakeClock()
    with build(db_path, clock) as first:
        session_id, key_b64 = enrol(first)
        drive(first, clock, session_id, key_b64, "honest")

    with build(db_path, clock) as second:
        assert all(not s["connected"] for s in board(second))
        assert second.get(f"/v1/sessions/{session_id}").json()["connected"] is False


def test_evidence_survives_a_restart(db_path):
    """A flag whose evidence is gone is just an accusation."""
    clock = FakeClock()
    with build(db_path, clock) as first:
        session_id, key_b64 = enrol(first)
        drive(first, clock, session_id, key_b64, "phone")

    with build(db_path, clock) as second:
        entries = board(second)[0]["timeline"]

    assert any(e["rule_id"] == "phone_detected" for e in entries)


def test_a_new_session_after_restart_still_works(db_path):
    clock = FakeClock()
    with build(db_path, clock) as first:
        enrol(first)

    with build(db_path, clock) as second:
        session_id, key_b64 = enrol(second)
        drive(second, clock, session_id, key_b64, "honest")
        status = second.get(f"/v1/sessions/{session_id}").json()

    assert status["events_received"] > 0
    assert status["integrity_breaches"] == []


def test_signal_and_breach_counts_survive(db_path):
    clock = FakeClock()
    with build(db_path, clock) as first:
        session_id, key_b64 = enrol(first)
        drive(first, clock, session_id, key_b64, "honest")
        before = first.get(f"/v1/sessions/{session_id}").json()["signal_counts"]

    with build(db_path, clock) as second:
        after = second.get(f"/v1/sessions/{session_id}").json()["signal_counts"]

    assert after == before
    assert after.get("signal.gaze", 0) > 0


# -- retention --------------------------------------------------------------


def test_retention_purges_old_data_on_startup(db_path):
    """Evidence is personal data; keeping it forever is the failure mode."""
    clock = FakeClock()
    with build(db_path, clock) as first:
        session_id, key_b64 = enrol(first)
        drive(first, clock, session_id, key_b64, "phone")
        assert board(first)

    # Restart 31 days later with the default 30-day retention.
    clock.now += 31 * DAY_MS
    with build(db_path, clock) as second:
        assert board(second) == [], "data past the retention window must be gone"
        assert second.get(f"/v1/sessions/{session_id}").status_code == 404


def test_retention_keeps_data_inside_the_window(db_path):
    clock = FakeClock()
    with build(db_path, clock) as first:
        session_id, key_b64 = enrol(first)
        drive(first, clock, session_id, key_b64, "phone")

    clock.now += 5 * DAY_MS
    with build(db_path, clock) as second:
        assert board(second), "data inside the retention window must be kept"
        assert second.get(f"/v1/sessions/{session_id}").status_code == 200


def test_retention_can_be_disabled_explicitly(db_path):
    clock = FakeClock()
    with build(db_path, clock) as first:
        session_id, key_b64 = enrol(first)
        drive(first, clock, session_id, key_b64, "phone")

    clock.now += 400 * DAY_MS
    settings = Settings(
        master_secret=MASTER,
        clock=clock,
        console_token=CONSOLE_TOKEN,
        db_path=db_path,
        retention_days=0,
    )
    with TestClient(create_app(settings)) as forever:
        assert forever.get(f"/v1/sessions/{session_id}").status_code == 200


# -- store unit behaviour ---------------------------------------------------


def test_memory_store_is_selected_for_in_memory_path():
    store = open_store(":memory:")
    assert store.load_sessions() == []
    store.append_violation({"session_id": "x"}, 0)  # must not raise
    assert store.load_violations(10) == {}


def test_sqlite_store_is_reopenable(tmp_path):
    path = str(tmp_path / "reopen.db")
    first = SqliteStore(path)
    first.close()
    second = SqliteStore(path)
    assert second.load_sessions() == []
    second.close()


def test_violations_are_capped_per_session_on_load(tmp_path):
    """A long exam must not replay ten thousand rows into the board."""
    store = SqliteStore(str(tmp_path / "many.db"))
    for i in range(500):
        store.append_violation(
            {
                "session_id": "sess-x",
                "violation_id": f"v{i}",
                "rule_id": "gaze_off_screen",
                "severity": "soft",
                "message": "m",
                "opened_at_ms": i,
                "fired_at_ms": i,
                "duration_ms": 1,
                "resolved": False,
                "evidence": [],
            },
            now_ms=i,
        )
    loaded = store.load_violations(50)["sess-x"]
    store.close()

    assert len(loaded) == 50
    assert loaded[0]["violation_id"] == "v450", "must keep the most recent"
    assert loaded[-1]["violation_id"] == "v499"


def test_load_violation_returns_the_firing_row_not_the_resolution(tmp_path):
    """A violation_id is reused for its resolution event, which carries no
    evidence. The lookup must prefer the row a reviewer actually wants."""
    store = SqliteStore(str(tmp_path / "single.db"))
    store.append_violation(
        {
            "session_id": "sess-x",
            "violation_id": "v1",
            "rule_id": "gaze_off_screen",
            "severity": "soft",
            "message": "fired",
            "duration_ms": 2500,
            "resolved": False,
            "evidence": [
                {"server_ts_ms": 1, "client_ts_ms": 1, "seq": 0, "payload": {"yaw_deg": -40}}
            ],
        },
        now_ms=100,
    )
    store.append_violation(
        {
            "session_id": "sess-x",
            "violation_id": "v1",
            "rule_id": "gaze_off_screen",
            "severity": "soft",
            "message": "resolved",
            "duration_ms": 2500,
            "resolved": True,
            "evidence": [],
        },
        now_ms=200,
    )

    record = store.load_violation("v1")
    store.close()

    assert record is not None
    assert record["message"] == "fired"
    assert record["evidence"], (
        "the firing row's evidence must be returned, not the empty resolution row"
    )


def test_load_violation_returns_none_for_an_unknown_id(tmp_path):
    store = SqliteStore(str(tmp_path / "none.db"))
    assert store.load_violation("nope") is None
    store.close()


def test_unknown_session_is_not_resurrected_by_restore(db_path):
    clock = FakeClock()
    with build(db_path, clock) as client:
        assert client.get("/v1/sessions/sess-never-existed").status_code == 404


def test_websocket_for_a_purged_session_is_rejected(db_path):
    clock = FakeClock()
    with build(db_path, clock) as first:
        session_id, key_b64 = enrol(first)
        drive(first, clock, session_id, key_b64, "honest")

    clock.now += 31 * DAY_MS
    with build(db_path, clock) as second, pytest.raises(WebSocketDisconnect):
        with second.websocket_connect(f"/v1/sessions/{session_id}/telemetry") as ws:
            ws.send_text("{}")
            ws.receive_text()
