"""One suite, run against every `Store` backend that persists.

The point is that these are the *same* assertions, parametrised over the
backend rather than written twice. `SqliteStore` and `PostgresStore` hold
the same audit trail and the same replay-protection counters, and the
whole value of the `Store` protocol is that a handler cannot tell them
apart. A behaviour that holds in one and not the other is a bug that
would otherwise surface only in whichever environment runs the backend
with less test coverage.

`MemoryStore` is deliberately excluded: it is a genuine no-op that returns
nothing on read, so "saving then loading returns what was saved" is false
for it by design.

The Postgres tests skip when no database is reachable, so the ordinary
`make test` on a laptop without Docker stays green. They are not optional
where it matters — CI and the compose stack both have one.
"""

from __future__ import annotations

import os

import pytest

from proctor_gateway.store import SessionRecord, SqliteStore

POSTGRES_DSN = os.environ.get("PROCTOR_TEST_POSTGRES_DSN")

DAY_MS = 86_400_000
NOW = 1_700_000_000_000


def _postgres_store():
    from proctor_gateway.postgres_store import PostgresStore

    store = PostgresStore(POSTGRES_DSN)
    # Each test gets a clean slate; these tables are shared across the
    # parametrised runs and a leftover row from a previous test would make
    # failures depend on execution order.
    with store._pool.connection() as conn:
        conn.execute(
            "TRUNCATE sessions, violations, identity_templates, identity_checks, "
            "audio_transcripts, audio_checks, recordings"
        )
        conn.commit()
    return store


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "sqlite":
        store = SqliteStore(str(tmp_path / "backend.db"))
        yield store
        store.close()
        return

    if not POSTGRES_DSN:
        pytest.skip("PROCTOR_TEST_POSTGRES_DSN is unset; no Postgres to test against")
    store = _postgres_store()
    yield store
    store.close()


def session(session_id: str = "sess-1", **overrides) -> SessionRecord:
    base = dict(
        session_id=session_id,
        exam_id="exam-1",
        candidate_ref="cand-1",
        created_ms=NOW,
        last_seq=42,
        last_monotonic_ms=5_000,
        first_client_ms=NOW,
        first_server_ms=NOW,
        events_received=100,
        ended_cleanly=False,
        attested_build="0.1.0",
        integrity_breaches=["stream_replay"],
        breach_counts={"stream_replay": 2},
        signal_counts={"signal.gaze": 40},
    )
    base.update(overrides)
    return SessionRecord(**base)


def violation(violation_id: str = "v-1", **overrides) -> dict:
    base = dict(
        session_id="sess-1",
        violation_id=violation_id,
        rule_id="phone_detected",
        severity="hard",
        message="A mobile phone was detected in frame.",
        opened_at_ms=NOW,
        fired_at_ms=NOW + 800,
        duration_ms=800,
        resolved=False,
        evidence=[{"seq": 1, "payload": {"label": "phone", "confidence": 0.91}}],
    )
    base.update(overrides)
    return base


# -- sessions and the replay counter -------------------------------------------


def test_a_saved_session_round_trips(store):
    store.save_session(session(), NOW)
    [loaded] = store.load_sessions()
    assert loaded.session_id == "sess-1"
    assert loaded.exam_id == "exam-1"
    assert loaded.events_received == 100
    assert loaded.attested_build == "0.1.0"


def test_last_seq_survives_because_replay_protection_depends_on_it(store):
    """The security property, not a convenience.

    If `last_seq` did not survive, restarting the gateway would reset it
    and a hostile client could re-send its whole earlier stream.
    """
    store.save_session(session(last_seq=500, last_monotonic_ms=90_000), NOW)
    [loaded] = store.load_sessions()
    assert loaded.last_seq == 500
    assert loaded.last_monotonic_ms == 90_000


def test_saving_the_same_session_updates_rather_than_duplicates(store):
    store.save_session(session(last_seq=1), NOW)
    store.save_session(session(last_seq=2), NOW + 1000)
    sessions = store.load_sessions()
    assert len(sessions) == 1
    assert sessions[0].last_seq == 2


def test_structured_session_fields_survive_the_round_trip(store):
    """These are JSON in one backend and JSONB in the other; a decoding
    difference between them would silently change what a reviewer sees."""
    store.save_session(
        session(
            integrity_breaches=["stream_replay", "stream_sequence_gap"],
            breach_counts={"stream_replay": 3, "stream_sequence_gap": 1},
            signal_counts={"signal.gaze": 120, "signal.face": 118},
        ),
        NOW,
    )
    [loaded] = store.load_sessions()
    assert list(loaded.integrity_breaches) == ["stream_replay", "stream_sequence_gap"]
    assert loaded.breach_counts == {"stream_replay": 3, "stream_sequence_gap": 1}
    assert loaded.signal_counts == {"signal.gaze": 120, "signal.face": 118}


# -- violations ------------------------------------------------------------------


def test_a_violation_round_trips_with_its_evidence(store):
    store.append_violation(violation(), NOW)
    loaded = store.load_violation("v-1")
    assert loaded is not None
    assert loaded["rule_id"] == "phone_detected"
    assert loaded["evidence"][0]["payload"]["confidence"] == 0.91


def test_load_violation_returns_the_firing_row_not_the_resolution(store):
    """A violation id is reused for its later resolution row, which carries
    no evidence. Returning the latest would show a reviewer nothing for
    every resolved flag."""
    store.append_violation(violation(), NOW)
    store.append_violation(violation(resolved=True, evidence=[]), NOW + 5_000)
    loaded = store.load_violation("v-1")
    assert loaded["resolved"] is False
    assert loaded["evidence"] != []


def test_violations_are_grouped_by_session_oldest_first(store):
    """The caller replays these into the triage board in order, and both
    the timeline and the decaying score depend on arrival order."""
    store.append_violation(violation("v-1"), NOW)
    store.append_violation(violation("v-2"), NOW + 1_000)
    store.append_violation(violation("v-3", session_id="sess-2"), NOW + 2_000)

    grouped = store.load_violations(per_session=10)
    assert [v["violation_id"] for v in grouped["sess-1"]] == ["v-1", "v-2"]
    assert [v["violation_id"] for v in grouped["sess-2"]] == ["v-3"]


def test_load_violations_caps_per_session(store):
    for index in range(6):
        store.append_violation(violation(f"v-{index}"), NOW + index * 1_000)
    grouped = store.load_violations(per_session=3)
    assert len(grouped["sess-1"]) == 3
    # The most recent three, still oldest-first among themselves.
    assert [v["violation_id"] for v in grouped["sess-1"]] == ["v-3", "v-4", "v-5"]


def test_an_unknown_violation_id_is_none(store):
    assert store.load_violation("nope") is None


# -- identity: the split retention clocks -----------------------------------------


def test_a_template_round_trips(store):
    store.save_template("sess-1", "test-embedder", (0.1, 0.2, 0.3), 3, 0.95, NOW)
    templates = store.load_templates()
    assert templates["sess-1"]["reference"] == (0.1, 0.2, 0.3)
    assert templates["sess-1"]["captures"] == 3


def test_templates_expire_without_taking_their_scores_with_them(store):
    """The privacy split this schema exists for: the biometric goes, the
    audit trail stays."""
    store.save_template("sess-1", "test-embedder", (0.1, 0.2), 3, 0.9, NOW)
    store.append_identity_check(
        "sess-1",
        {
            "outcome": "match",
            "similarity": 0.81,
            "threshold": 0.55,
            "calibrated_on": "test fixture",
            "issues": [],
        },
        NOW,
    )

    removed = store.purge_templates_older_than(NOW + DAY_MS)
    assert removed == 1
    assert store.load_templates() == {}
    assert store.load_identity_checks("sess-1")[0]["similarity"] == 0.81


# -- audio: the same split, applied to transcripts ---------------------------------


def test_a_transcript_round_trips(store):
    store.save_transcript("t-1", "sess-1", "reading the question", "test", 4000, NOW)
    loaded = store.load_transcript("t-1")
    assert loaded["transcript"] == "reading the question"


def test_transcripts_expire_without_taking_their_labels_with_them(store):
    store.save_transcript("t-1", "sess-1", "what is the answer", "test", 4000, NOW)
    store.append_audio_check("sess-1", "t-1", "seeking_help", 0.88, "test-llm", NOW)

    removed = store.purge_transcripts_older_than(NOW + DAY_MS)
    assert removed == 1
    assert store.load_transcript("t-1") is None
    assert store.load_audio_checks("sess-1")[0]["label"] == "seeking_help"


# -- recordings --------------------------------------------------------------------


def recording(recording_id: str = "rec-1", **overrides) -> dict:
    base = dict(
        recording_id=recording_id,
        session_id="sess-1",
        status="requested",
        requested_ms=NOW,
        started_ms=None,
        stopped_ms=None,
        egress_id="eg-1",
        storage_ref=None,
        failure_reason=None,
    )
    base.update(overrides)
    return base


def test_a_recording_round_trips(store):
    store.save_recording(recording(), NOW)
    loaded = store.load_recording("rec-1")
    assert loaded["status"] == "requested"
    assert loaded["egress_id"] == "eg-1"


def test_saving_a_recording_again_updates_its_status(store):
    store.save_recording(recording(), NOW)
    store.save_recording(
        recording(status="available", storage_ref="s3://bucket/a.mp4", stopped_ms=NOW + 60_000),
        NOW + 60_000,
    )
    loaded = store.load_recording("rec-1")
    assert loaded["status"] == "available"
    assert loaded["storage_ref"] == "s3://bucket/a.mp4"
    assert len(store.load_recordings_for_session("sess-1")) == 1


def test_a_recording_is_findable_by_egress_id(store):
    """The webhook lookup path: LiveKit keys its deliveries on egressId,
    not on anything this system chose."""
    store.save_recording(recording(egress_id="eg-xyz"), NOW)
    found = store.find_recording_by_egress_id("eg-xyz")
    assert found is not None
    assert found["recording_id"] == "rec-1"
    assert store.find_recording_by_egress_id("eg-nope") is None


def test_recordings_expire_on_their_own_clock(store):
    store.save_recording(recording(), NOW)
    removed = store.purge_recordings_older_than(NOW + DAY_MS)
    assert removed == 1
    assert store.load_recording("rec-1") is None


# -- retention -----------------------------------------------------------------------


def test_purge_removes_old_violations_and_sessions(store):
    store.save_session(session(), NOW)
    store.append_violation(violation(), NOW)

    violations, sessions = store.purge_older_than(NOW + 30 * DAY_MS)
    assert violations == 1
    assert sessions == 1
    assert store.load_sessions() == []
    assert store.load_violation("v-1") is None


def test_purge_leaves_recent_data_alone(store):
    store.save_session(session(), NOW)
    store.append_violation(violation(), NOW)

    violations, sessions = store.purge_older_than(NOW - DAY_MS)
    assert (violations, sessions) == (0, 0)
    assert len(store.load_sessions()) == 1
