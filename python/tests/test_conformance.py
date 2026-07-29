"""Cross-language conformance: TypeScript signs, Python verifies.

The edge client and the backend are written in different languages against
the same wire format. Nothing else in the test suite would notice if they
drifted — the Python tests sign with Python and the client would simply
start failing in production, at which point the symptom is "the gateway
ignores this candidate's telemetry".

So this test runs the actual TypeScript signing code and feeds its output
to the actual Python verifier. It is skipped, loudly, when Node is not
available rather than passing vacuously.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from proctor_protocol import (
    SignatureError,
    derive_session_key,
    sign_envelope,
    verify_frame,
)

REPO = Path(__file__).resolve().parents[2]
CONFORMANCE_TS = REPO / "tools" / "conformance.ts"
MASTER = b"conformance-master-secret"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(
    node is None, reason="node is required for cross-language conformance"
)


@pytest.fixture(scope="module")
def ts_frames() -> dict:
    session_id = "sess-conformance01"
    key = derive_session_key(MASTER, session_id)
    result = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "--no-warnings",
            str(CONFORMANCE_TS),
            base64.b64encode(key).decode("ascii"),
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(f"TypeScript conformance harness failed:\n{result.stderr}")
    return json.loads(result.stdout)


def test_typescript_signed_frames_verify_in_python(ts_frames):
    """Every payload type, signed by the client code, accepted by the server."""
    key = derive_session_key(MASTER, ts_frames["session_id"])
    assert len(ts_frames["frames"]) == 10, "expected one frame per payload type"

    seen: set[str] = set()
    for index, frame in enumerate(ts_frames["frames"]):
        envelope = verify_frame(frame, key)
        assert envelope.session_id == ts_frames["session_id"]
        assert envelope.seq == index
        seen.add(envelope.payload.type)

    assert seen == {
        "signal.gaze",
        "signal.head_pose",
        "signal.face",
        "signal.object",
        "signal.liveness",
        "signal.audio",
        "signal.environment",
        "heartbeat",
        "lifecycle",
        "attestation",
    }


def test_awkward_floats_survive_the_round_trip(ts_frames):
    """The values that would break a canonical-JSON signing scheme.

    `0.1 + 0.2` and friends do not have identical shortest representations
    across Python and JavaScript. Signing the transmitted bytes rather than
    a re-serialisation is what makes this pass.
    """
    key = derive_session_key(MASTER, ts_frames["session_id"])
    gaze = verify_frame(ts_frames["frames"][0], key).payload
    assert gaze.yaw_deg == pytest.approx(0.1 + 0.2)
    assert gaze.confidence == pytest.approx(0.8800000000000001)

    head = verify_frame(ts_frames["frames"][1], key).payload
    assert head.roll_deg == pytest.approx(89.99999999999999)


def test_non_ascii_payloads_survive_the_round_trip(ts_frames):
    """UTF-8 handling must agree; a mismatch here breaks real process names."""
    key = derive_session_key(MASTER, ts_frames["session_id"])
    environment = verify_frame(ts_frames["frames"][6], key).payload
    assert "Ünïcodé-Prøcess" in environment.blacklisted_processes

    lifecycle = verify_frame(ts_frames["frames"][8], key).payload
    assert lifecycle.detail == "candidate finished — done"


def test_tampering_with_a_typescript_frame_is_rejected(ts_frames):
    """Confirms the test is actually checking the signature, not just parsing."""
    key = derive_session_key(MASTER, ts_frames["session_id"])
    frame = json.loads(ts_frames["frames"][0])
    body = base64.urlsafe_b64decode(frame["b"] + "=" * (-len(frame["b"]) % 4))
    mutated = body.replace(b'"on_screen":false', b'"on_screen":true')
    assert mutated != body, "the fixture no longer contains the field being mutated"

    frame["b"] = base64.urlsafe_b64encode(mutated).decode("ascii").rstrip("=")
    with pytest.raises(SignatureError):
        verify_frame(json.dumps(frame), key)


def test_generated_typescript_types_are_not_stale():
    """The generated types must match the current Python schema.

    Without this, adding a field to a signal in Python and forgetting to
    regenerate leaves the client unable to construct it, with no error
    until someone notices the signal never arrives.
    """
    result = subprocess.run(
        ["python3", str(REPO / "tools" / "generate_ts_protocol.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_python_signed_frames_are_byte_compatible(ts_frames):
    """The reverse direction: the server can produce frames the client reads.

    Needed for any future server-to-client signed message (round-trip clock
    probes, policy pushes), so the scheme is verified both ways now rather
    than discovered to be one-directional later.
    """
    key = derive_session_key(MASTER, ts_frames["session_id"])
    original = verify_frame(ts_frames["frames"][2], key)
    resigned = sign_envelope(original, key)
    assert verify_frame(resigned, key) == original
