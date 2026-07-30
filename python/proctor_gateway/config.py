"""Gateway configuration."""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field


def wall_clock_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class Settings:
    master_secret: bytes
    """Root secret for per-session key derivation. Never leaves the server."""

    clock: Callable[[], int] = field(default=wall_clock_ms)
    """Server clock, injectable.

    Exists so the test suite can drive a 15-minute exam through the real
    gateway in milliseconds. Without it, any test of a sustained violation
    would have to sleep for the duration it is testing, which in practice
    means those tests do not get written.
    """

    tick_interval_ms: int = 1_000
    """How often absence-of-telemetry rules are evaluated."""

    clock_skew_tolerance_ms: int = 2_000
    """Permitted drift between client-reported and server-observed elapsed time.

    Generous, because laptops suspend, throttle background timers, and lose
    scheduling slices under load. This is looking for a clock being *moved*,
    not for imprecision.
    """

    max_sequence_gap: int = 0
    """Tolerated gap in the telemetry sequence. Zero: every event must arrive.

    The transport is an ordered, reliable WebSocket, so a gap is not packet
    loss. It means events were dropped before transmission, which is exactly
    the cheapest attack a hostile client has.
    """

    policy_path: str | None = None

    db_path: str = ":memory:"
    """Where durable state lives. `:memory:` disables persistence entirely.

    Defaults to ephemeral so constructing `Settings` directly — as the test
    suite does — never touches the filesystem. `from_env` defaults to a real
    file, because a gateway that forgets `last_seq` on restart lets a client
    replay its whole earlier stream.
    """

    face_model_path: str = ""
    """ONNX face-embedding model. Empty selects the deterministic test double.

    No model is bundled: the commonly available ArcFace weights, including
    the InsightFace model zoo, are licensed for non-commercial research use
    only, so shipping one would hand every downstream user a licence
    violation. Supply your own licensed export.
    """

    identity_threshold: float | None = None
    """Cosine cutoff for a face match. No default, deliberately.

    Face-match error rates vary by one to two orders of magnitude across
    demographic groups, so there is no cutoff that is correct everywhere.
    Identity verification stays disabled until this and
    `identity_calibrated_on` are both set.
    """

    identity_calibrated_on: str = ""
    """What population and conditions the threshold was measured against."""

    template_retention_days: int = 1
    """How long face templates are kept, separate from everything else.

    Much shorter than `retention_days` because a template has no review
    value once the exam is over — a human compares recordings, not vectors
    — while being the highest-risk row in the database. The similarity
    scores derived from it survive on the longer clock, so the audit trail
    outlives the biometric.
    """

    retention_days: int = 30
    """How long violations and finished sessions are kept.

    Evidence rows are observations about identifiable people derived from
    their faces. Keeping them past the point they are needed for review
    turns a proctoring system into a biometric archive, so retention is a
    first-class setting rather than a cleanup script someone might run.
    Institutions with a shorter statutory limit should lower it.
    """

    audio_model_path: str = ""
    """Speech-to-text backend. Empty selects the deterministic test double.

    Unlike `face_model_path`, an empty value here is not a licensing
    workaround: Whisper's weights are MIT-licensed by OpenAI, and a real
    model is simply not bundled to keep the base install light. See
    `proctor_audio.transcription` for the full reasoning.
    """

    audio_consent_notice: str = ""
    """A record that candidates were shown a specific notice before their
    microphone was recorded and transcribed. Non-empty is required to
    enable the audio pipeline at all.

    This is a different gate from identity's, on purpose. Identity's
    problem is measurement accuracy varying by population; audio's added
    legal exposure is primarily *consent* — recording and transcribing a
    person's voice implicates wiretap and all-party-consent statutes in
    the US and GDPR Art. 6/7 in a way a webcam frame comparison does not.
    Failing closed on a missing consent record follows the same logic as
    failing closed on a missing calibration record: an unrecorded
    justification is not a justification.
    """

    llm_complete: Callable[[str, str], str] | None = None
    """Injected `(system_prompt, user_prompt) -> completion_text` for audio
    intent classification. None disables outward-help detection entirely;
    transcripts are still produced and shown to the proctor for human
    judgement, and the coarse `sustained_speech` fusion rule (VAD-only,
    keyword-free) still applies regardless.

    Deliberately not configurable via environment variable: it is code, not
    a value, and this repository does not hardcode a call to any specific
    model vendor. Wiring a real classifier means writing a small adapter
    around your own model client and constructing `Settings` with it
    directly — see ARCHITECTURE.md § 5.5.
    """

    llm_model_name: str = ""
    """Label recorded alongside audio classifications, e.g. "claude-x-2026-06".
    Purely descriptive — for a reviewer to know what produced a finding."""

    audio_help_window: int = 5
    audio_help_required: int = 3
    """See `proctor_audio.monitor.AudioMonitorPolicy` — the same sustained-
    disagreement discipline as identity verification, applied to intent
    classifications instead of face matches."""

    audio_transcript_retention_days: int = 1
    """How long raw transcripts are kept, separate from the classification
    labels derived from them. Deliberately its own knob rather than reusing
    `template_retention_days`: a spoken-word transcript and a face template
    are both short-lived-by-design, but an institution's counsel may want
    different windows for each, and coupling them would remove that choice
    for no functional benefit.
    """

    livekit_url: str = ""
    """Self-hosted LiveKit server URL (e.g. `wss://livekit.example.org`).

    Empty selects `FakeRoomProvider` — no network call, no real server —
    the same "empty selects the test double" convention as
    `face_model_path` and `audio_model_path`. Unlike those, the reason is
    neither licensing nor dependency weight: there is simply no LiveKit
    server available in the environment this was built in to test
    against. See `proctor_media.provider`'s module docstring for exactly
    which parts of the integration rest on solid, long-documented ground
    (tokens, webhooks) versus a best-effort against a more version-
    sensitive one (the Egress recording RPC).
    """

    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    media_consent_notice: str = ""
    """A record that candidates were shown a notice before their camera and
    microphone were streamed to a proctor over WebRTC and, if recording is
    started, retained as video.

    One gate for both joining the room and recording, not two: a live
    video call is itself processing of biometric-adjacent data even
    before anyone presses record, so gating only the recording would be
    false comfort. Retention below is still its own, separate, shorter
    clock — the recorded artifact is materially more sensitive than the
    live stream passing through.
    """

    recording_retention_days: int = 14
    """How long a recording *reference* (not the video itself — see
    `proctor_gateway.store.SqliteStore.purge_recordings_older_than`) is
    kept. Shorter than the 30-day violation default, deliberately: this is
    the single most sensitive artifact this platform touches — video and
    audio of a candidate in their own room — and the appeals/dispute
    window it needs to survive for is an institutional policy question,
    not a technical one. Tune it to that policy; do not leave it at a
    default chosen for convenience.
    """

    console_token: str = ""
    """Bearer token required to read the proctor stream.

    The proctor endpoints carry every candidate's flags and evidence —
    biometric-derived data about identifiable people. Unauthenticated, the
    port is a live feed of who is being accused of what.

    When unset, `from_env` generates a random token and logs it rather than
    leaving the endpoint open. Failing closed with a printed dev token
    keeps local work friction-free without ever producing a deployment that
    is accidentally public.
    """

    @classmethod
    def from_env(cls) -> Settings:
        raw = os.environ.get("PROCTOR_MASTER_SECRET")
        if raw:
            master = raw.encode("utf-8")
        else:
            # Ephemeral secret so `make dev` works with no setup. Sessions do
            # not survive a restart, which is correct for development and
            # loudly wrong for production — hence the warning in main().
            master = secrets.token_bytes(32)
        return cls(
            master_secret=master,
            policy_path=os.environ.get("PROCTOR_POLICY_PATH"),
            console_token=os.environ.get("PROCTOR_CONSOLE_TOKEN") or secrets.token_urlsafe(24),
            db_path=os.environ.get("PROCTOR_DB_PATH", "proctor.db"),
            retention_days=int(os.environ.get("PROCTOR_RETENTION_DAYS", "30")),
            face_model_path=os.environ.get("PROCTOR_FACE_MODEL", ""),
            identity_threshold=(
                float(threshold_raw)
                if (threshold_raw := os.environ.get("PROCTOR_IDENTITY_THRESHOLD"))
                else None
            ),
            identity_calibrated_on=os.environ.get("PROCTOR_IDENTITY_CALIBRATED_ON", ""),
            template_retention_days=int(os.environ.get("PROCTOR_TEMPLATE_RETENTION_DAYS", "1")),
            audio_model_path=os.environ.get("PROCTOR_AUDIO_MODEL", ""),
            audio_consent_notice=os.environ.get("PROCTOR_AUDIO_CONSENT_NOTICE", ""),
            audio_transcript_retention_days=int(
                os.environ.get("PROCTOR_AUDIO_TRANSCRIPT_RETENTION_DAYS", "1")
            ),
            # llm_complete is intentionally absent here: it is code, not an
            # environment value. See its docstring.
            livekit_url=os.environ.get("PROCTOR_LIVEKIT_URL", ""),
            livekit_api_key=os.environ.get("PROCTOR_LIVEKIT_API_KEY", ""),
            livekit_api_secret=os.environ.get("PROCTOR_LIVEKIT_API_SECRET", ""),
            media_consent_notice=os.environ.get("PROCTOR_MEDIA_CONSENT_NOTICE", ""),
            recording_retention_days=int(os.environ.get("PROCTOR_RECORDING_RETENTION_DAYS", "14")),
        )

    @property
    def has_persistent_secret(self) -> bool:
        return bool(os.environ.get("PROCTOR_MASTER_SECRET"))

    @property
    def identity_enabled(self) -> bool:
        """Identity checks run only when a threshold *and* its provenance exist.

        Failing closed on a missing calibration record is the point: an
        unattributed cutoff invites the assumption that it is universal,
        and it is not.
        """
        return self.identity_threshold is not None and bool(self.identity_calibrated_on.strip())

    @property
    def audio_enabled(self) -> bool:
        """The audio pipeline runs only when a consent record exists.

        Transcription and the coarse VAD-only `sustained_speech` rule are
        independent of the LLM classifier: consent gates transcription
        itself, since transcribing speech is the action with wiretap/consent
        exposure. `llm_complete` being unset just means transcripts are
        surfaced to a human without an automated intent read on top.
        """
        return bool(self.audio_consent_notice.strip())

    @property
    def media_enabled(self) -> bool:
        """SFU/recording runs only when a consent record exists.

        Same fail-closed shape as identity and audio, gating on the risk
        specific to this feature: consent to being live-streamed and
        potentially recorded, not measurement accuracy or transcription.
        """
        return bool(self.media_consent_notice.strip())

    @property
    def has_configured_console_token(self) -> bool:
        return bool(os.environ.get("PROCTOR_CONSOLE_TOKEN"))
