"""Canonical telemetry event schema.

Design rule, and the single most important one in this codebase:

    The edge emits OBSERVATIONS. The server decides VIOLATIONS.

The client never says "the candidate cheated". It says "I measured yaw at
-42 degrees with confidence 0.88". All policy — thresholds, durations,
severity — lives server-side in the fusion engine, for two reasons:

1.  Policy can be retuned without reshipping a signed desktop binary to
    every candidate.
2.  A tampered client cannot suppress a verdict it never computed.

See ARCHITECTURE.md ("Trust model") for what this does and does not buy us.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------
# Signal payloads
# --------------------------------------------------------------------------


class ObjectLabel(StrEnum):
    """Objects the detector is trained to find.

    Deliberately a closed set. An open vocabulary invites the fusion engine
    to grow rules for classes the model was never evaluated on.
    """

    PHONE = "phone"
    PERSON = "person"
    SMARTWATCH = "smartwatch"
    HEADPHONES = "headphones"
    BOOK = "book"


class BoundingBox(_Frozen):
    """Normalised to [0, 1] against the frame, origin top-left."""

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)


class GazeSignal(_Frozen):
    """Where the eyes are pointing, from MediaPipe iris landmarks.

    Angles are relative to the camera axis, not the head: `yaw_deg` is
    eye-in-head rotation combined with head rotation, i.e. the direction of
    regard in camera space.
    """

    type: Literal["signal.gaze"] = "signal.gaze"
    yaw_deg: float = Field(ge=-90.0, le=90.0)
    pitch_deg: float = Field(ge=-90.0, le=90.0)
    on_screen: bool
    confidence: float = Field(ge=0.0, le=1.0)


class HeadPoseSignal(_Frozen):
    type: Literal["signal.head_pose"] = "signal.head_pose"
    yaw_deg: float = Field(ge=-180.0, le=180.0)
    pitch_deg: float = Field(ge=-180.0, le=180.0)
    roll_deg: float = Field(ge=-180.0, le=180.0)
    confidence: float = Field(ge=0.0, le=1.0)


class FaceSignal(_Frozen):
    """How many faces the edge sees, and where the primary one is.

    `face_count == 0` is as interesting as `face_count > 1` — it covers the
    candidate leaving the seat, and also the camera being covered.
    """

    type: Literal["signal.face"] = "signal.face"
    face_count: int = Field(ge=0)
    primary_bbox: BoundingBox | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class ObjectSignal(_Frozen):
    type: Literal["signal.object"] = "signal.object"
    label: ObjectLabel
    confidence: float = Field(ge=0.0, le=1.0)
    bbox: BoundingBox | None = None


class LivenessSignal(_Frozen):
    """Anti-spoofing score. 1.0 == confidently a live face in the room."""

    type: Literal["signal.liveness"] = "signal.liveness"
    score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)


class AudioSignal(_Frozen):
    """Local VAD output. Raw audio is NOT sent with this signal.

    Audio only leaves the machine when `speech_active` has been true long
    enough to be worth transcribing, and it travels on the separate media
    channel — never inline in telemetry.
    """

    type: Literal["signal.audio"] = "signal.audio"
    speech_active: bool
    energy_db: float
    confidence: float = Field(ge=0.0, le=1.0)


class EnvironmentSignal(_Frozen):
    """OS-level state from the Electron/Tauri host process.

    This is the only signal that cannot be produced by a browser tab, and
    the main reason the client is a desktop app rather than a web page.
    """

    type: Literal["signal.environment"] = "signal.environment"
    window_focused: bool
    monitor_count: int = Field(ge=0)
    blacklisted_processes: tuple[str, ...] = ()
    screen_share_active: bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class FrameQualitySignal(_Frozen):
    """How usable the camera frame is, independent of what is in it.

    Exists so that "we cannot see the candidate" is never silently scored
    as "the candidate did something". A smeared lens, a dark room, or a
    deliberately obscured camera all degrade every other signal in this
    file, and a reviewer needs to know that the inputs were bad rather
    than reading a low gaze confidence as evasion.

    `sharpness` is a normalised variance-of-Laplacian: near 0.0 is a flat
    or heavily blurred image, higher is more edge detail. It is not
    calibrated to any physical unit and is only meaningful relative to the
    threshold in the policy.
    """

    type: Literal["signal.frame_quality"] = "signal.frame_quality"
    sharpness: float = Field(ge=0.0, le=1.0)
    brightness: float = Field(ge=0.0, le=1.0)
    face_covered: bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class LockdownEvent(StrEnum):
    FULLSCREEN_EXIT = "fullscreen_exit"
    RESTRICTED_KEY = "restricted_key"
    CLIPBOARD = "clipboard"
    CONTEXT_MENU = "context_menu"
    TAB_SWITCH = "tab_switch"


class LockdownSignal(_Frozen):
    """A candidate action the exam shell blocked, and how many it has blocked.

    Reported, never adjudicated, like everything else here. The client
    counts locally so it can show the candidate an accurate warning
    immediately, but `strike` is an observation the server re-derives from
    the stream — see the `lockdown_strikes_exhausted` policy rule. A
    client that under-reports its own strike count does not lower the
    number the server arrives at.

    `detail` carries the specific combination attempted (e.g. "Meta+C")
    for the reviewer, not for matching on.
    """

    type: Literal["signal.lockdown"] = "signal.lockdown"
    event: LockdownEvent
    strike: int = Field(ge=0)
    allowance: int = Field(ge=0)
    detail: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


# --------------------------------------------------------------------------
# Non-signal payloads
# --------------------------------------------------------------------------


class Heartbeat(_Frozen):
    """Proof of life.

    Emitted on a fixed cadence even when nothing interesting is happening,
    so that a stream going quiet is distinguishable from a candidate
    sitting still. Silence is a violation; see fusion rule `stream_silent`.
    """

    type: Literal["heartbeat"] = "heartbeat"
    frames_processed: int = Field(ge=0)
    edge_fps: float = Field(ge=0.0)
    dropped_frames: int = Field(ge=0, default=0)


class LifecyclePhase(StrEnum):
    SESSION_START = "session_start"
    IDENTITY_VERIFIED = "identity_verified"
    EXAM_START = "exam_start"
    EXAM_END = "exam_end"
    SESSION_END = "session_end"


class Lifecycle(_Frozen):
    type: Literal["lifecycle"] = "lifecycle"
    phase: LifecyclePhase
    detail: str | None = None


class Attestation(_Frozen):
    """What the client claims to be running.

    Hashes are checked server-side against the expected build manifest. A
    mismatch does not prove cheating — it may be a stale client — but it
    does mean the telemetry cannot be taken at face value, so the session
    gets marked for mandatory human review.
    """

    type: Literal["attestation"] = "attestation"
    client_build: str
    model_hashes: dict[str, str]
    platform: str


Payload = Annotated[
    GazeSignal
    | HeadPoseSignal
    | FaceSignal
    | ObjectSignal
    | LivenessSignal
    | AudioSignal
    | EnvironmentSignal
    | FrameQualitySignal
    | LockdownSignal
    | Heartbeat
    | Lifecycle
    | Attestation,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------
# Envelope
# --------------------------------------------------------------------------


class Envelope(_Frozen):
    """A single authenticated telemetry event.

    `seq` must increase strictly and without gaps for the lifetime of a
    session. The gateway rejects reordering and replays on this field, and
    treats a gap as evidence the client dropped events on purpose.

    Two clocks are carried on purpose. `ts_client_ms` is wall clock and is
    useful but trivially manipulated; `ts_monotonic_ms` counts from session
    start and cannot go backwards on a well-behaved client. Disagreement
    between the two is itself a signal that the clock was moved.
    """

    v: int = PROTOCOL_VERSION
    session_id: str = Field(min_length=8, max_length=64)
    seq: int = Field(ge=0)
    ts_client_ms: int = Field(ge=0)
    ts_monotonic_ms: int = Field(ge=0)
    payload: Payload

    def is_signal(self) -> bool:
        return self.payload.type.startswith("signal.")
