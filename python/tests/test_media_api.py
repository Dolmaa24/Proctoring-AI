"""SFU/recording pipeline through the real gateway: tokens, recording
lifecycle, webhook.

Uses `FakeRoomProvider` (selected automatically because `livekit_url` is
left unset) so recording start/stop never make a real network call.
`livekit_api_key`/`livekit_api_secret` are configured anyway: webhook
signature verification is checked against those settings directly, not
against whichever room provider happens to be wired in, so it is exercised
the same way it would be against a real LiveKit deployment.
"""

from __future__ import annotations

import base64
import hashlib
import json

import jwt
import pytest
from fastapi.testclient import TestClient

from proctor_gateway import Settings, create_app

MASTER = b"media-master-secret"
TOKEN = "media-console-token"
API_KEY = "test-livekit-key"
API_SECRET = "a-test-webhook-secret-at-least-32-bytes-long"


class FakeClock:
    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self.now = start_ms

    def __call__(self) -> int:
        return self.now


def settings(clock: FakeClock, **overrides) -> Settings:
    base = dict(
        master_secret=MASTER,
        clock=clock,
        tick_interval_ms=50,
        console_token=TOKEN,
        media_consent_notice="candidates shown notice X at exam start, 2026-06",
        livekit_api_key=API_KEY,
        livekit_api_secret=API_SECRET,
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


def decode(client: TestClient, token: str) -> dict:
    secret = client.app.state.media_provider.credentials.api_secret
    return jwt.decode(token, secret, algorithms=["HS256"], options={"verify_exp": False})


def sign_webhook(body: bytes, secret: str = API_SECRET, key: str = API_KEY) -> str:
    claims = {
        "iss": key,
        "exp": 9_999_999_999,
        "sha256": base64.b64encode(hashlib.sha256(body).digest()).decode("ascii"),
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def start_recording(client: TestClient, session_id: str) -> dict:
    return client.post(
        f"/v1/proctor/sessions/{session_id}/media/recording/start",
        headers={"authorization": f"Bearer {TOKEN}"},
    ).json()


def db_client(clock: FakeClock, tmp_path, **overrides) -> TestClient:
    """A client backed by a real, file-based store rather than the default
    `:memory:` `MemoryStore`. Needed for every test below that reads a
    recording back after writing it (stop, list, webhook) — `MemoryStore`
    is a true no-op and never returns anything on read, same reason
    `test_identity_api.py`/`test_audio_api.py` reach for `tmp_path` for
    their own read-after-write tests.
    """
    overrides.setdefault("db_path", str(tmp_path / "media.db"))
    return TestClient(create_app(settings(clock, **overrides)))


# -- consent gate --------------------------------------------------------------


def test_media_is_disabled_without_a_consent_notice(clock):
    app = create_app(settings(clock, media_consent_notice=""))
    with TestClient(app) as client:
        session_id = enrol_session(client)
        assert client.post(f"/v1/sessions/{session_id}/media/token").status_code == 503
        config = client.get(f"/v1/sessions/{session_id}/media/config").json()
        assert config["enabled"] is False


def test_media_config_never_exposes_the_api_secret(client):
    session_id = enrol_session(client)
    config = client.get(f"/v1/sessions/{session_id}/media/config").json()
    assert set(config) == {"enabled", "url"}


def test_media_config_for_unknown_session_is_404(client):
    assert client.get("/v1/sessions/sess-nope/media/config").status_code == 404


# -- candidate token -------------------------------------------------------------


def test_candidate_token_is_publish_only(client):
    session_id = enrol_session(client)
    body = client.post(f"/v1/sessions/{session_id}/media/token").json()
    claims = decode(client, body["token"])["video"]
    assert claims["canPublish"] is True
    assert claims["canSubscribe"] is False
    assert claims["roomRecord"] is False


def test_candidate_token_for_unknown_session_is_404(client):
    assert client.post("/v1/sessions/sess-nope/media/token").status_code == 404


# -- proctor token: the elevation-of-privilege boundary --------------------------


def test_a_candidate_cannot_obtain_a_proctor_grant(client):
    """The one privilege boundary this module exists to hold.

    Nothing a candidate's client can call — with no auth beyond knowing its
    own session_id — can ever produce a token that can subscribe or record.
    Only the console-token-gated proctor endpoint can, and it issues a
    structurally different grant shape.
    """
    session_id = enrol_session(client)
    candidate_claims = decode(
        client, client.post(f"/v1/sessions/{session_id}/media/token").json()["token"]
    )["video"]
    assert candidate_claims["canSubscribe"] is False
    assert candidate_claims["roomRecord"] is False

    unauth = client.post(f"/v1/proctor/sessions/{session_id}/media/token")
    assert unauth.status_code == 401

    proctor_claims = decode(
        client,
        client.post(
            f"/v1/proctor/sessions/{session_id}/media/token",
            headers={"authorization": f"Bearer {TOKEN}"},
        ).json()["token"],
    )["video"]
    assert proctor_claims["canSubscribe"] is True
    assert proctor_claims["roomRecord"] is True


def test_proctor_token_requires_the_console_token(client):
    session_id = enrol_session(client)
    assert client.post(f"/v1/proctor/sessions/{session_id}/media/token").status_code == 401


def test_proctor_token_for_unknown_session_is_404(client):
    response = client.post(
        "/v1/proctor/sessions/sess-nope/media/token",
        headers={"authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 404


# -- recording lifecycle ---------------------------------------------------------


def test_recording_start_requires_the_console_token(client):
    session_id = enrol_session(client)
    response = client.post(f"/v1/proctor/sessions/{session_id}/media/recording/start")
    assert response.status_code == 401


def test_recording_start_is_gated_on_media_consent(clock):
    app = create_app(settings(clock, media_consent_notice=""))
    with TestClient(app) as client:
        session_id = enrol_session(client)
        response = client.post(
            f"/v1/proctor/sessions/{session_id}/media/recording/start",
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 503


def test_recording_starts_and_can_be_stopped(clock, tmp_path):
    client = db_client(clock, tmp_path)
    session_id = enrol_session(client)
    body = start_recording(client, session_id)
    assert body["status"] == "requested"
    assert body["egress_id"]

    stopped = client.post(
        f"/v1/proctor/sessions/{session_id}/media/recording/{body['recording_id']}/stop",
        headers={"authorization": f"Bearer {TOKEN}"},
    )
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "stopping"


def test_stopping_an_unknown_recording_is_404(client):
    session_id = enrol_session(client)
    response = client.post(
        f"/v1/proctor/sessions/{session_id}/media/recording/nope/stop",
        headers={"authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 404


def test_stopping_an_already_available_recording_is_a_409(clock, tmp_path):
    """The corrected state machine, exercised through the API: a recording
    can reach AVAILABLE via webhook alone, with no local STOPPING step —
    see test_media.py's
    test_a_recording_can_complete_without_ever_passing_through_stopping.
    Stopping it afterwards must be a conflict, not a silent no-op.
    """
    client = db_client(clock, tmp_path)
    session_id = enrol_session(client)
    started = start_recording(client, session_id)

    body = json.dumps(
        {
            "event": "egress_ended",
            "room": {"name": f"proctor-{session_id}"},
            "egressInfo": {"egressId": started["egress_id"], "status": "EGRESS_COMPLETE"},
        }
    ).encode()
    webhook = client.post(
        "/v1/media/webhook", content=body, headers={"Authorize": sign_webhook(body)}
    )
    assert webhook.status_code == 200

    response = client.post(
        f"/v1/proctor/sessions/{session_id}/media/recording/{started['recording_id']}/stop",
        headers={"authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 409


def test_recordings_list_requires_the_console_token(client):
    session_id = enrol_session(client)
    response = client.get(f"/v1/proctor/sessions/{session_id}/media/recordings")
    assert response.status_code == 401


def test_recordings_list_reflects_started_recordings(clock, tmp_path):
    client = db_client(clock, tmp_path)
    session_id = enrol_session(client)
    start_recording(client, session_id)
    response = client.get(
        f"/v1/proctor/sessions/{session_id}/media/recordings",
        headers={"authorization": f"Bearer {TOKEN}"},
    )
    assert len(response.json()["recordings"]) == 1


# -- webhook ----------------------------------------------------------------------


def test_webhook_updates_recording_status_to_active(clock, tmp_path):
    client = db_client(clock, tmp_path)
    session_id = enrol_session(client)
    started = start_recording(client, session_id)

    body = json.dumps(
        {
            "event": "egress_started",
            "room": {"name": f"proctor-{session_id}"},
            "egressInfo": {"egressId": started["egress_id"], "status": "EGRESS_ACTIVE"},
        }
    ).encode()
    response = client.post(
        "/v1/media/webhook", content=body, headers={"Authorize": sign_webhook(body)}
    )
    assert response.status_code == 200

    recordings = client.get(
        f"/v1/proctor/sessions/{session_id}/media/recordings",
        headers={"authorization": f"Bearer {TOKEN}"},
    ).json()["recordings"]
    assert recordings[0]["status"] == "active"


def test_webhook_completion_stores_the_storage_reference(clock, tmp_path):
    client = db_client(clock, tmp_path)
    session_id = enrol_session(client)
    started = start_recording(client, session_id)

    body = json.dumps(
        {
            "event": "egress_ended",
            "room": {"name": f"proctor-{session_id}"},
            "egressInfo": {
                "egressId": started["egress_id"],
                "status": "EGRESS_COMPLETE",
                "fileResults": [{"location": "s3://bucket/rec.mp4"}],
            },
        }
    ).encode()
    client.post("/v1/media/webhook", content=body, headers={"Authorize": sign_webhook(body)})

    recordings = client.get(
        f"/v1/proctor/sessions/{session_id}/media/recordings",
        headers={"authorization": f"Bearer {TOKEN}"},
    ).json()["recordings"]
    assert recordings[0]["status"] == "available"
    assert recordings[0]["storage_ref"] == "s3://bucket/rec.mp4"


def test_webhook_failed_egress_marks_the_recording_failed(clock, tmp_path):
    client = db_client(clock, tmp_path)
    session_id = enrol_session(client)
    started = start_recording(client, session_id)

    body = json.dumps(
        {
            "event": "egress_ended",
            "room": {"name": f"proctor-{session_id}"},
            "egressInfo": {"egressId": started["egress_id"], "status": "EGRESS_FAILED"},
        }
    ).encode()
    client.post("/v1/media/webhook", content=body, headers={"Authorize": sign_webhook(body)})

    recordings = client.get(
        f"/v1/proctor/sessions/{session_id}/media/recordings",
        headers={"authorization": f"Bearer {TOKEN}"},
    ).json()["recordings"]
    assert recordings[0]["status"] == "failed"


def test_webhook_with_an_unknown_egress_id_is_ignored_not_errored(client):
    """LiveKit retries webhook deliveries; an event for a recording this
    gateway does not know about (or has already purged) must not 500 and
    invite a retry storm."""
    body = json.dumps(
        {
            "event": "egress_ended",
            "room": {"name": "proctor-nope"},
            "egressInfo": {"egressId": "eg-unknown", "status": "EGRESS_COMPLETE"},
        }
    ).encode()
    response = client.post(
        "/v1/media/webhook", content=body, headers={"Authorize": sign_webhook(body)}
    )
    assert response.status_code == 200


def test_webhook_with_a_bad_signature_is_rejected(client):
    body = b'{"event": "egress_started"}'
    header = sign_webhook(body, secret="a-completely-different-secret-32-bytes!")
    response = client.post("/v1/media/webhook", content=body, headers={"Authorize": header})
    assert response.status_code == 401


def test_webhook_missing_the_authorize_header_is_rejected(client):
    assert client.post("/v1/media/webhook", content=b"{}").status_code == 401


# -- retention: recording references expire on their own clock -------------------


def test_recording_references_expire_on_their_own_clock(clock, tmp_path):
    db = str(tmp_path / "media.db")
    app = create_app(settings(clock, db_path=db, recording_retention_days=1))
    with TestClient(app) as client:
        session_id = enrol_session(client)
        recording_id = start_recording(client, session_id)["recording_id"]

    clock.now += 3 * 86_400_000
    later = create_app(settings(clock, db_path=db, recording_retention_days=1))
    with TestClient(later):
        assert later.state.store.load_recording(recording_id) is None
