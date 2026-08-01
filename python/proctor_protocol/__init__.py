"""Shared telemetry contract between the edge client and the cloud backend.

This package is the single source of truth for the wire format. The
TypeScript client types are generated from it (`make protocol-ts`) rather
than hand-maintained, so the two halves cannot drift.
"""

from .events import (
    PROTOCOL_VERSION,
    Attestation,
    AudioSignal,
    BoundingBox,
    Envelope,
    EnvironmentSignal,
    FaceSignal,
    FrameQualitySignal,
    GazeSignal,
    HeadPoseSignal,
    Heartbeat,
    Lifecycle,
    LifecyclePhase,
    LivenessSignal,
    LockdownEvent,
    LockdownSignal,
    ObjectLabel,
    ObjectSignal,
    Payload,
)
from .signing import (
    SignatureError,
    derive_session_key,
    sign_envelope,
    verify_frame,
)

__all__ = [
    "PROTOCOL_VERSION",
    "Attestation",
    "AudioSignal",
    "BoundingBox",
    "Envelope",
    "EnvironmentSignal",
    "FaceSignal",
    "FrameQualitySignal",
    "GazeSignal",
    "HeadPoseSignal",
    "Heartbeat",
    "Lifecycle",
    "LifecyclePhase",
    "LivenessSignal",
    "LockdownEvent",
    "LockdownSignal",
    "ObjectLabel",
    "ObjectSignal",
    "Payload",
    "SignatureError",
    "derive_session_key",
    "sign_envelope",
    "verify_frame",
]
