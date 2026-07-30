"""Tests for proctor_media: token shape, webhook verification, recording lifecycle.

Token and webhook tests decode/construct JWTs directly with `pyjwt` rather
than only round-tripping through this package's own functions — the point
is to pin the exact wire shape a real LiveKit server would check, not just
confirm this code agrees with itself.
"""

from __future__ import annotations

import base64
import hashlib
import json

import jwt
import pytest

from proctor_media import (
    FakeRoomProvider,
    InvalidTransition,
    LiveKitCredentials,
    RecordingRecord,
    RecordingStatus,
    TokenError,
    VideoGrant,
    build_access_token,
    verify_webhook,
)

CREDS = LiveKitCredentials(api_key="key123", api_secret="a-test-secret-at-least-32-bytes-long-000")


# -- access tokens ------------------------------------------------------------


def decode(token: str) -> dict:
    # These tests pin `now` to fixed, often-past timestamps to assert exact
    # claim values — real expiry validation is exercised separately, in
    # test_a_token_signed_with_a_different_secret_is_rejected's sibling below.
    return jwt.decode(token, CREDS.api_secret, algorithms=["HS256"], options={"verify_exp": False})


def test_token_carries_the_documented_claim_shape():
    grant = VideoGrant.publisher("proctor-sess-1")
    token = build_access_token(CREDS, "cand-1", grant, now=1_700_000_000)
    claims = decode(token)

    assert claims["iss"] == "key123"
    assert claims["sub"] == "cand-1"
    assert claims["iat"] == 1_700_000_000
    assert claims["nbf"] == 1_700_000_000
    assert claims["exp"] == 1_700_000_000 + 6 * 60 * 60
    assert claims["video"]["room"] == "proctor-sess-1"
    assert claims["video"]["roomJoin"] is True


def test_a_candidate_grant_can_never_subscribe_or_record():
    """The security property this module exists to hold, structurally.

    A candidate's client is the least-trusted part of this system — see
    ARCHITECTURE.md's process-boundary discussion. A leaked candidate
    token must not be useful for watching or recording anyone.
    """
    grant = VideoGrant.publisher("proctor-sess-1")
    claims = decode(build_access_token(CREDS, "cand-1", grant))["video"]
    assert claims["canPublish"] is True
    assert claims["canSubscribe"] is False
    assert claims["roomRecord"] is False
    assert claims["roomAdmin"] is False


def test_a_proctor_grant_can_never_publish():
    grant = VideoGrant.subscriber("proctor-sess-1")
    claims = decode(build_access_token(CREDS, "proctor-1", grant))["video"]
    assert claims["canPublish"] is False
    assert claims["canSubscribe"] is True
    assert claims["roomRecord"] is True


def test_custom_ttl_is_honoured():
    grant = VideoGrant.publisher("r")
    token = build_access_token(CREDS, "x", grant, ttl_seconds=60, now=1000)
    assert decode(token)["exp"] == 1060


def test_an_expired_token_is_rejected_by_a_real_verifier():
    """The other half of test_custom_ttl_is_honoured: the exp claim must
    actually be enforced, not just present with the right value."""
    grant = VideoGrant.publisher("r")
    token = build_access_token(CREDS, "x", grant, ttl_seconds=60, now=1_000_000)
    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(token, CREDS.api_secret, algorithms=["HS256"])


def test_a_token_signed_with_a_different_secret_is_rejected():
    grant = VideoGrant.publisher("r")
    token = build_access_token(CREDS, "x", grant)
    wrong = LiveKitCredentials(
        api_key="key123", api_secret="a-different-test-secret-32-bytes-long11"
    )
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, wrong.api_secret, algorithms=["HS256"])


# -- webhook verification -----------------------------------------------------


def sign_webhook(body: bytes, creds: LiveKitCredentials = CREDS, *, bad_hash: bool = False) -> str:
    digest = b"tampered-hash-value-of-wrong-length!!" if bad_hash else hashlib.sha256(body).digest()
    claims = {
        "iss": creds.api_key,
        "exp": 9_999_999_999,
        "sha256": base64.b64encode(digest).decode("ascii"),
    }
    return jwt.encode(claims, creds.api_secret, algorithm="HS256")


def test_a_correctly_signed_webhook_is_accepted_and_parsed():
    body = json.dumps(
        {
            "event": "egress_ended",
            "room": {"name": "proctor-sess-1"},
            "egressInfo": {
                "egressId": "eg-1",
                "status": "EGRESS_COMPLETE",
                "fileResults": [{"location": "s3://bucket/proctor-sess-1.mp4"}],
            },
        }
    ).encode()
    header = sign_webhook(body)

    event = verify_webhook(body, header, CREDS)
    assert event.event == "egress_ended"
    assert event.room_name == "proctor-sess-1"
    assert event.egress_id == "eg-1"
    assert event.storage_ref == "s3://bucket/proctor-sess-1.mp4"


def test_missing_authorize_header_is_rejected():
    with pytest.raises(TokenError, match="missing"):
        verify_webhook(b"{}", None, CREDS)


def test_webhook_signed_with_the_wrong_secret_is_rejected():
    body = b'{"event": "room_started"}'
    wrong_creds = LiveKitCredentials(
        api_key="key123", api_secret="a-different-test-secret-32-bytes-long11"
    )
    header = sign_webhook(body, wrong_creds)
    with pytest.raises(TokenError, match="invalid webhook signature"):
        verify_webhook(body, header, CREDS)


def test_a_tampered_body_is_rejected_even_with_a_validly_signed_header():
    """The check that actually matters: a valid signature on a swapped body.

    Verifying only the JWT's own signature would pass here — the header
    is genuinely signed by the right secret. The body-hash comparison is
    what catches the payload having been changed after signing.
    """
    original = b'{"event": "room_started", "room": {"name": "a"}}'
    header = sign_webhook(original)
    tampered = b'{"event": "room_started", "room": {"name": "b"}}'
    with pytest.raises(TokenError, match="does not match"):
        verify_webhook(tampered, header, CREDS)


def test_webhook_with_wrong_issuer_is_rejected():
    body = b'{"event": "room_started"}'
    claims = {
        "iss": "someone-elses-key",
        "exp": 9_999_999_999,
        "sha256": base64.b64encode(hashlib.sha256(body).digest()).decode("ascii"),
    }
    header = jwt.encode(claims, CREDS.api_secret, algorithm="HS256")
    with pytest.raises(TokenError):
        verify_webhook(body, header, CREDS)


def test_malformed_json_body_is_rejected_even_if_the_hash_matches():
    body = b"not json at all"
    header = sign_webhook(body)
    with pytest.raises(TokenError, match="not valid JSON"):
        verify_webhook(body, header, CREDS)


def test_webhook_missing_optional_egress_fields_parses_cleanly():
    body = json.dumps({"event": "room_started", "room": {"name": "r"}}).encode()
    event = verify_webhook(body, sign_webhook(body), CREDS)
    assert event.egress_id is None
    assert event.storage_ref is None


# -- recording lifecycle -------------------------------------------------------


def new_recording(now_ms: int = 1000) -> RecordingRecord:
    return RecordingRecord(
        recording_id="rec-1",
        session_id="sess-1",
        status=RecordingStatus.REQUESTED,
        requested_ms=now_ms,
    )


def test_the_documented_lifecycle_succeeds():
    record = new_recording()
    record.transition(RecordingStatus.ACTIVE, now_ms=2000)
    assert record.started_ms == 2000
    record.transition(RecordingStatus.STOPPING, now_ms=3000)
    record.transition(RecordingStatus.AVAILABLE, now_ms=4000)
    assert record.stopped_ms == 4000
    assert record.status is RecordingStatus.AVAILABLE


@pytest.mark.parametrize(
    ("start", "attempted"),
    [
        (RecordingStatus.AVAILABLE, RecordingStatus.ACTIVE),
        (RecordingStatus.FAILED, RecordingStatus.ACTIVE),
    ],
)
def test_out_of_order_transitions_are_rejected(start, attempted):
    """Guards against an out-of-order webhook delivery corrupting state.

    LiveKit does not guarantee webhook delivery order across retries; a
    late-arriving "started" event after a "failed" one must not silently
    resurrect a recording that already failed.
    """
    record = new_recording()
    record.status = start
    with pytest.raises(InvalidTransition):
        record.transition(attempted, now_ms=9999)


def test_a_recording_can_complete_without_ever_passing_through_stopping():
    """The realistic path this state machine had to be corrected for.

    STOPPING represents *us* asking the SFU to stop, set locally when a
    proctor clicks "stop". A recording can also end on its own — the
    candidate's client disconnects and the room closes — and the
    completion webhook then arrives with no local STOPPING step ever
    having happened. That must not be an InvalidTransition.
    """
    record = new_recording()
    record.transition(RecordingStatus.ACTIVE, now_ms=2000)
    record.transition(RecordingStatus.AVAILABLE, now_ms=3000)
    assert record.status is RecordingStatus.AVAILABLE
    assert record.stopped_ms == 3000


def test_a_recording_can_complete_or_be_stopped_before_its_started_webhook_arrives():
    """The same webhook-ordering problem, one step earlier.

    A proctor can click "stop" the instant after clicking "start", well
    before an `egress_started` webhook has arrived. A very short recording
    can likewise finish and deliver `egress_ended` before `egress_started`
    is processed. Neither is out-of-order in any sense this system can
    detect or should reject — REQUESTED must reach STOPPING and AVAILABLE
    directly, the same as ACTIVE does.
    """
    stopped = new_recording()
    stopped.transition(RecordingStatus.STOPPING, now_ms=1500)
    assert stopped.status is RecordingStatus.STOPPING

    completed = new_recording()
    completed.transition(RecordingStatus.AVAILABLE, now_ms=1500)
    assert completed.status is RecordingStatus.AVAILABLE
    assert completed.stopped_ms == 1500


def test_a_terminal_status_has_no_further_transitions():
    for terminal in (RecordingStatus.AVAILABLE, RecordingStatus.FAILED):
        record = new_recording()
        record.status = terminal
        for target in RecordingStatus:
            with pytest.raises(InvalidTransition):
                record.transition(target, now_ms=1)


def test_recording_can_fail_from_any_non_terminal_state():
    for start in (RecordingStatus.REQUESTED, RecordingStatus.ACTIVE, RecordingStatus.STOPPING):
        record = new_recording()
        record.status = start
        record.transition(RecordingStatus.FAILED, now_ms=1)
        assert record.status is RecordingStatus.FAILED


def test_as_dict_never_includes_recording_bytes_or_a_playable_url_field():
    """The evidence, not the payload — same principle as identity/audio.

    This assertion is about what the *shape* promises, not just today's
    field list: as_dict must never grow a field that hands back media
    content directly, only a reference to where it lives.
    """
    record = new_recording()
    record.storage_ref = "s3://bucket/key.mp4"
    body = record.as_dict()
    assert body["storage_ref"] == "s3://bucket/key.mp4"
    assert all(key not in body for key in ("bytes", "data", "url", "playback_url"))


# -- fake provider --------------------------------------------------------------


def test_fake_provider_issues_real_shaped_tokens():
    # FakeRoomProvider() defaults to its own throwaway credentials, distinct
    # from the module-level CREDS — decode with the ones it actually signed
    # with, the same way a real verifier would use the matching secret.
    provider = FakeRoomProvider()
    token = provider.candidate_token("sess-1", "cand-1")
    claims = jwt.decode(
        token,
        provider.credentials.api_secret,
        algorithms=["HS256"],
        options={"verify_exp": False},
    )
    assert claims["video"]["canPublish"] is True
    assert claims["video"]["canSubscribe"] is False


def test_fake_provider_records_start_and_stop_calls():
    provider = FakeRoomProvider()
    started = provider.start_recording("sess-1")
    assert provider.started == ["sess-1"]
    provider.stop_recording("sess-1", started.egress_id)
    assert provider.stopped == [("sess-1", started.egress_id)]


def test_fake_provider_issues_unique_egress_ids():
    provider = FakeRoomProvider()
    a = provider.start_recording("sess-1")
    b = provider.start_recording("sess-2")
    assert a.egress_id != b.egress_id
