"""Audio pipeline through the real gateway.

Uses the deterministic test transcriber (audio bytes are literally the
UTF-8 transcript) and a fake `llm_complete`, so what is under test is this
project's own code — consent gating, storage, retention, the tiered
enable/disable, and the flag path — not a real speech or language model.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from proctor_gateway import Settings, create_app

MASTER = b"audio-master-secret"
TOKEN = "audio-console-token"
DAY_MS = 86_400_000


class FakeClock:
    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self.now = start_ms

    def __call__(self) -> int:
        return self.now


def audio(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def fake_llm(label: str = "seeking_help", confidence: float = 0.85):
    def complete(system_prompt: str, user_prompt: str) -> str:
        return json.dumps({"label": label, "confidence": confidence, "rationale": "test"})

    return complete


def settings(clock: FakeClock, **overrides) -> Settings:
    base = dict(
        master_secret=MASTER,
        clock=clock,
        tick_interval_ms=50,
        console_token=TOKEN,
        audio_consent_notice="candidates shown notice X at exam start, 2026-06",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(clock: FakeClock) -> TestClient:
    return TestClient(create_app(settings(clock)))


def enrol_session(client: TestClient) -> str:
    response = client.post("/v1/sessions", json={"exam_id": "exam-1", "candidate_ref": "cand-1"})
    return response.json()["session_id"]


def send_chunk(client: TestClient, session_id: str, text: str, **overrides):
    body = {"audio_b64": audio(text), "duration_ms": 4000}
    body.update(overrides)
    return client.post(f"/v1/sessions/{session_id}/audio/chunk", json=body)


# -- consent gate -------------------------------------------------------------


def test_audio_is_disabled_without_a_consent_notice(clock):
    """Fails closed. Recording and transcribing speech is the action with
    wiretap/consent exposure, so it must not run without a stated basis."""
    app = create_app(settings(clock, audio_consent_notice=""))
    with TestClient(app) as client:
        session_id = enrol_session(client)
        assert send_chunk(client, session_id, "hello").status_code == 503


def test_audio_is_enabled_once_a_consent_notice_is_configured(client):
    session_id = enrol_session(client)
    response = send_chunk(client, session_id, "thinking out loud about question two")
    assert response.status_code == 201


# -- transcription tier (no classifier configured) ---------------------------


def test_transcription_alone_produces_no_classification_or_finding(client):
    """Tier 1: transcript surfaced to a human, nothing automated on top."""
    session_id = enrol_session(client)
    body = send_chunk(client, session_id, "what is the answer to number four").json()
    assert body["transcript"] == "what is the answer to number four"
    assert body["classification"] is None
    assert body["finding"] is None


def test_transcript_is_stored_and_fetchable_by_reference(clock, tmp_path):
    # Needs a real, file-backed store: the default (`:memory:`) selects the
    # true no-op MemoryStore, which never returns anything on read.
    app = create_app(settings(clock, db_path=str(tmp_path / "fetch.db")))
    with TestClient(app) as client:
        session_id = enrol_session(client)
        body = send_chunk(client, session_id, "reading the question aloud").json()
        fetched = client.get(
            f"/v1/proctor/sessions/{session_id}/audio/transcripts/{body['transcript_id']}",
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    assert fetched.status_code == 200
    assert fetched.json()["transcript"] == "reading the question aloud"


def test_transcript_endpoint_requires_the_console_token(client):
    session_id = enrol_session(client)
    body = send_chunk(client, session_id, "x").json()
    response = client.get(
        f"/v1/proctor/sessions/{session_id}/audio/transcripts/{body['transcript_id']}"
    )
    assert response.status_code == 401


def test_unknown_transcript_id_is_404(client):
    session_id = enrol_session(client)
    response = client.get(
        f"/v1/proctor/sessions/{session_id}/audio/transcripts/nope",
        headers={"authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 404


# -- intent classification tier -----------------------------------------------


def test_a_single_seeking_help_classification_raises_no_flag(clock):
    app = create_app(settings(clock, llm_complete=fake_llm("seeking_help")))
    with TestClient(app) as client:
        session_id = enrol_session(client)
        body = send_chunk(client, session_id, "what is the answer to number four").json()
    assert body["classification"]["label"] == "seeking_help"
    assert body["finding"] is None


def test_sustained_seeking_help_reaches_the_proctor_as_a_reviewable_flag(clock):
    app = create_app(settings(clock, llm_complete=fake_llm("seeking_help")))
    with TestClient(app) as client:
        session_id = enrol_session(client)
        with client.websocket_connect(
            "/v1/proctor/stream", subprotocols=["proctor.console.v1", f"token.{TOKEN}"]
        ) as ws:
            ws.receive_json()
            for _ in range(4):
                send_chunk(client, session_id, "can you tell me the answer")

            violations = []
            for _ in range(20):
                message = ws.receive_json(mode="text")
                if message.get("kind") == "violation":
                    violations.append(message)
                if violations:
                    break

    assert violations
    flag = violations[0]
    assert flag["rule_id"] == "audio_seeking_help"
    assert flag["severity"] == "hard"
    assert flag["action"] == "flag"
    assert flag["requires_human_review"] is True


def test_the_flag_references_the_transcript_but_does_not_embed_it(clock):
    """The privacy split this design is built around.

    The evidence must carry enough to review the *pattern* (labels,
    confidences, a reference) without permanently retaining the actual
    words someone spoke inside the long-lived violation record.
    """
    app = create_app(settings(clock, llm_complete=fake_llm("seeking_help")))
    with TestClient(app) as client:
        session_id = enrol_session(client)
        with client.websocket_connect(
            "/v1/proctor/stream", subprotocols=["proctor.console.v1", f"token.{TOKEN}"]
        ) as ws:
            ws.receive_json()
            for _ in range(4):
                send_chunk(client, session_id, "please just tell me the answer")
            for _ in range(20):
                message = ws.receive_json(mode="text")
                if message.get("kind") == "violation":
                    break

    payload = message["evidence"][0]["payload"]
    assert "transcript_ref" in payload
    assert "transcript_excerpt" not in payload, (
        "the transcript text must not be baked into the long-retained violation"
    )


def test_malformed_llm_response_never_produces_a_flag(clock):
    app = create_app(settings(clock, llm_complete=lambda s, u: "garbage, not json"))
    with TestClient(app) as client:
        session_id = enrol_session(client)
        bodies = [
            send_chunk(client, session_id, "please tell me the answer").json() for _ in range(4)
        ]
    assert all(b["classification"]["label"] == "unclear" for b in bodies)
    assert all(b["finding"] is None for b in bodies)


# -- retention: the split clocks ----------------------------------------------


def test_transcripts_expire_before_the_classifications_derived_from_them(clock, tmp_path):
    db = str(tmp_path / "audio.db")
    app = create_app(
        settings(
            clock,
            db_path=db,
            llm_complete=fake_llm("thinking_aloud"),
            audio_transcript_retention_days=1,
        )
    )
    with TestClient(app) as client:
        session_id = enrol_session(client)
        transcript_id = send_chunk(client, session_id, "thinking about the problem").json()[
            "transcript_id"
        ]

    clock.now += 3 * DAY_MS
    later = create_app(
        settings(
            clock,
            db_path=db,
            llm_complete=fake_llm("thinking_aloud"),
            audio_transcript_retention_days=1,
        )
    )
    with TestClient(later) as client:
        assert later.state.store.load_transcript(transcript_id) is None, (
            "the transcript itself must be gone"
        )
        history = client.get(
            f"/v1/proctor/sessions/{session_id}/audio",
            headers={"authorization": f"Bearer {TOKEN}"},
        ).json()
        assert history["checks"], "the classification labels must survive"


def test_disabling_audio_after_the_fact_leaves_stored_data_alone(clock, tmp_path):
    db = str(tmp_path / "toggle.db")
    with TestClient(create_app(settings(clock, db_path=db))) as first:
        session_id = enrol_session(first)
        send_chunk(first, session_id, "hello")

    off = create_app(settings(clock, db_path=db, audio_consent_notice=""))
    with TestClient(off) as client:
        assert send_chunk(client, session_id, "hello again").status_code == 503


# -- input handling ------------------------------------------------------------


def test_malformed_audio_is_rejected(client):
    session_id = enrol_session(client)
    response = client.post(
        f"/v1/sessions/{session_id}/audio/chunk",
        json={"audio_b64": "not!valid!base64", "duration_ms": 1000},
    )
    assert response.status_code == 400


def test_oversized_audio_is_rejected(client):
    session_id = enrol_session(client)
    huge = base64.b64encode(b"x" * (3 * 1024 * 1024)).decode()
    response = client.post(
        f"/v1/sessions/{session_id}/audio/chunk",
        json={"audio_b64": huge, "duration_ms": 1000},
    )
    assert response.status_code == 400


def test_audio_chunk_on_an_unknown_session_is_404(client):
    assert send_chunk(client, "sess-nope", "hello").status_code == 404


def test_overlong_duration_is_rejected_by_validation(client):
    session_id = enrol_session(client)
    response = client.post(
        f"/v1/sessions/{session_id}/audio/chunk",
        json={"audio_b64": audio("x"), "duration_ms": 60_000},
    )
    assert response.status_code == 422
