"""FastAPI gateway: telemetry ingest, integrity checking, proctor fan-out.

Note what this service does *not* do: decode video, run models, or make
judgements. It authenticates frames, verifies the stream was not tampered
with, hands observations to the fusion engine, and broadcasts the results.
Video never touches this path — see ARCHITECTURE.md § Media plane.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import secrets
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from proctor_audio import (
    AudioIntentMonitor,
    AudioMonitorPolicy,
    AudioStatus,
    DeterministicTranscriber,
    IntentContext,
    LLMIntentClassifier,
    load_transcriber,
)
from proctor_fusion import FusionEngine, load_policy
from proctor_identity import (
    DeterministicEmbedder,
    EnrolmentError,
    IdentityStatus,
    IdentityVerifier,
    MatchPolicy,
    ProbeQuality,
    Threshold,
    build_enrolment,
    load_embedder,
)
from proctor_media import (
    FakeRoomProvider,
    InvalidTransition,
    LiveKitRoomProvider,
    RecordingRecord,
    RecordingStatus,
    RoomProvider,
    RoomProviderError,
    TokenError,
    verify_webhook,
)
from proctor_media.tokens import LiveKitCredentials
from proctor_protocol import (
    Attestation,
    Lifecycle,
    LifecyclePhase,
    SignatureError,
    derive_session_key,
    verify_frame,
)

from .config import Settings
from .hub import ProctorHub
from .sessions import IntegrityBreach, SessionRegistry, validate_against_policy
from .store import open_store
from .triage import TriageBoard

log = logging.getLogger("proctor.gateway")

WS_CLOSE_POLICY_VIOLATION = 1008
CONSOLE_SUBPROTOCOL = "proctor.console.v1"


def _bearer(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def _token_from_subprotocol(offered: str) -> str:
    """Extract the token from `proctor.console.v1, token.<value>`."""
    for part in offered.split(","):
        candidate = part.strip()
        if candidate.startswith("token."):
            return candidate[len("token.") :]
    return ""


def _console_token_ok(settings: Settings, presented: str) -> bool:
    if not settings.console_token:
        return False
    return secrets.compare_digest(settings.console_token, presented)


def _require_console_token(settings: Settings, presented: str) -> None:
    if not _console_token_ok(settings, presented):
        raise HTTPException(status_code=401, detail="unauthorised")


class _Broadcast:
    """Updates triage state, then fans the message out to consoles.

    Exposes the same `publish` as `ProctorHub` so it can be substituted at
    every call site. Keeping the board update here — on the single path
    every proctor-visible message already travels — means a new event kind
    cannot be added that the console silently never learns about.
    """

    def __init__(
        self,
        hub: ProctorHub,
        board: TriageBoard,
        settings: Settings,
        store: Any,
    ) -> None:
        self._hub = hub
        self._board = board
        self._settings = settings
        self._store = store

    async def publish(self, message: dict[str, Any]) -> None:
        now = self._settings.clock()
        kind = message.get("kind")
        session_id = message.get("session_id")

        if kind == "violation" and session_id:
            self._board.record_violation(message, now)
            # Written before the fan-out. A flag raised against someone and
            # then lost to a restart is worse than never flagging: a proctor
            # may already have seen it, and the evidence for it is gone.
            self._store.append_violation(message, now)
        elif kind == "stream_connected" and session_id:
            self._board.ensure(session_id, now, connected=True)
        elif kind == "stream_disconnected" and session_id:
            self._board.ensure(
                session_id,
                now,
                connected=False,
                ended_cleanly=message.get("ended_cleanly"),
                events_received=message.get("events_received"),
            )

        await self._hub.publish(message)


MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_AUDIO_BYTES = 2 * 1024 * 1024

_REPORTABLE_IDENTITY = {
    IdentityStatus.MISMATCH_SUSPECTED,
    IdentityStatus.UNOBSERVABLE,
}


def _decode_base64(data: str, max_bytes: int, what: str) -> bytes:
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception as exc:
        raise ValueError(f"{what} is not valid base64") from exc
    if not raw:
        raise ValueError(f"{what} is empty")
    if len(raw) > max_bytes:
        raise ValueError(f"{what} exceeds {max_bytes} bytes")
    return raw


def _flag_violation(
    *,
    session_id: str,
    rule_id: str,
    severity: str,
    message: str,
    now_ms: int,
    evidence_payload: dict[str, Any],
) -> dict[str, Any]:
    """The ordinary violation envelope, shared by identity and audio findings.

    Reusing this shape rather than inventing a parallel channel per feature
    means every finding inherits everything already true of a flag: it
    reaches the console, it lands in the audit trail and retention, and —
    the point — it is always `action: flag` with `requires_human_review:
    true`. Nothing that calls this ends an exam.
    """
    return {
        "violation_id": str(uuid.uuid4()),
        "session_id": session_id,
        "rule_id": rule_id,
        "severity": severity,
        "action": "flag",
        "requires_human_review": True,
        "message": message,
        "opened_at_ms": now_ms,
        "fired_at_ms": now_ms,
        "duration_ms": 0,
        "resolved": False,
        "evidence": [
            {
                "server_ts_ms": now_ms,
                "client_ts_ms": now_ms,
                "seq": -1,
                "payload": evidence_payload,
            }
        ],
    }


def _identity_violation(session_id: str, finding: Any, now_ms: int) -> dict[str, Any]:
    """Render an identity finding into the ordinary violation shape.

    The similarities and the threshold they were judged against are the
    evidence. A reviewer needs "0.41 against 0.55, calibrated on X", not
    the word "mismatch".
    """
    hard = finding.status is IdentityStatus.MISMATCH_SUSPECTED
    return _flag_violation(
        session_id=session_id,
        rule_id="identity_mismatch" if hard else "identity_unobservable",
        severity="hard" if hard else "info",
        message=finding.message,
        now_ms=now_ms,
        evidence_payload=finding.as_dict(),
    )


def _audio_violation(
    session_id: str, finding: Any, transcript_id: str, now_ms: int
) -> dict[str, Any]:
    """Render an audio finding into the ordinary violation shape.

    Note what is deliberately *absent* from the evidence: the transcript
    text itself. `finding.transcript_excerpt` exists for the live console
    view at flag-time, but the words a candidate spoke must not be baked
    into the long-retained violation record — that would defeat the point
    of giving transcripts their own short retention clock (see
    `proctor_gateway.store`). The evidence carries labels, confidences and
    a `transcript_ref`; the transcript itself is fetched separately, on
    demand, for as long as it still exists.
    """
    payload = {k: v for k, v in finding.as_dict().items() if k != "transcript_excerpt"}
    payload["transcript_ref"] = transcript_id
    return _flag_violation(
        session_id=session_id,
        rule_id="audio_seeking_help",
        severity="hard",
        message=finding.message,
        now_ms=now_ms,
        evidence_payload=payload,
    )


class EnrolRequest(BaseModel):
    captures: list[str] = Field(min_length=3, max_length=10)
    """Base64 face captures. Embedded and discarded; never written to disk."""


class ProbeRequest(BaseModel):
    image_b64: str | None = None
    face_count: int = Field(default=1, ge=0)
    detector_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    yaw_deg: float = 0.0
    pitch_deg: float = 0.0
    face_fraction: float = Field(default=0.3, ge=0.0, le=1.0)


class AudioChunkRequest(BaseModel):
    audio_b64: str
    duration_ms: int = Field(ge=0, le=30_000)
    exam_subject: str = ""


def _build_media_provider(settings: Settings) -> RoomProvider:
    """Real LiveKit provider when configured, otherwise the fake.

    Constructed regardless of `media_enabled` — same as the embedder and
    transcriber — so that the "enabled but backed by a test double" check
    below has something to compare against, and so flipping the consent
    gate on does not also require restarting with different provider
    wiring.
    """
    if settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret:
        return LiveKitRoomProvider(
            settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret
        )
    return FakeRoomProvider()


class CreateSessionRequest(BaseModel):
    exam_id: str = Field(min_length=1, max_length=128)
    candidate_ref: str = Field(min_length=1, max_length=128)
    """Opaque reference to the candidate, resolved by the institution.

    Deliberately not a name or an email. This service should not hold
    directly identifying information it has no need for.
    """


class CreateSessionResponse(BaseModel):
    session_id: str
    session_key_b64: str
    telemetry_url: str
    protocol_version: int


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    policy = load_policy(settings.policy_path)
    engine = FusionEngine(policy)
    store = open_store(settings.db_path)
    registry = SessionRegistry(
        skew_tolerance_ms=settings.clock_skew_tolerance_ms,
        store=store,
        clock=settings.clock,
    )
    hub = ProctorHub()
    board = TriageBoard()
    broadcast = _Broadcast(hub, board, settings, store)
    embedder = load_embedder(settings.face_model_path or None)
    verifier = _build_verifier(settings)
    transcriber = load_transcriber(settings.audio_model_path or None)
    intent_classifier = (
        LLMIntentClassifier(settings.llm_complete, settings.llm_model_name)
        if settings.llm_complete is not None
        else None
    )
    audio_monitor = AudioIntentMonitor(
        AudioMonitorPolicy(
            window=settings.audio_help_window,
            seeking_help_required=settings.audio_help_required,
        )
    )
    media_provider = _build_media_provider(settings)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        # Purge before restore, not after. The other order loads expired
        # rows into memory and only then deletes them from disk, so the
        # board keeps showing candidates whose data was supposed to be gone
        # and their sessions still accept a telemetry socket. Retention that
        # only applies to the database is not retention.
        _purge(store, settings)
        _purge_templates(store, settings)
        _purge_transcripts(store, settings)
        _purge_recordings(store, settings)
        restored = _restore(store, registry, board, engine, settings)
        if verifier is not None:
            _restore_templates(store, verifier, embedder)
        if restored:
            log.info("restored %d session(s) from %s", restored, settings.db_path)

        # A feature "enabled" by its gate but backed by a test double is the
        # worst of both worlds: it looks configured and does nothing real.
        if verifier is not None and isinstance(embedder, DeterministicEmbedder):
            log.warning(
                "identity verification is enabled but PROCTOR_FACE_MODEL is unset; "
                "using a test double that does NOT recognise faces. This must not "
                "run in production."
            )
        if settings.audio_enabled and isinstance(transcriber, DeterministicTranscriber):
            log.warning(
                "the audio pipeline is enabled but PROCTOR_AUDIO_MODEL is unset; "
                "using a test double that does NOT transcribe real audio. This "
                "must not run in production."
            )
        if settings.media_enabled and isinstance(media_provider, FakeRoomProvider):
            log.warning(
                "media/recording is enabled but PROCTOR_LIVEKIT_URL is unset; "
                "using a fake provider with no real SFU behind it. This must not "
                "run in production."
            )
        ticker = asyncio.create_task(_tick_loop(engine, broadcast, settings))
        log.info("gateway up: policy=%s rules=%d", policy.name, len(policy.rules))
        if not settings.has_persistent_secret:
            # Sharper than it looks. Session keys are derived from this
            # secret, so with persistence enabled a restart restores the
            # sessions but derives different keys for them: every frame
            # from a resuming client then fails its signature check and the
            # candidate is flagged for something the server did.
            severity = (
                "Restored sessions will reject their clients' frames."
                if settings.db_path != ":memory:"
                else "Sessions will not survive a restart."
            )
            log.warning(
                "PROCTOR_MASTER_SECRET is unset; using an ephemeral secret. %s "
                "Do not run this way in production.",
                severity,
            )
        if not settings.has_configured_console_token and settings.console_token:
            # Printed rather than left open. The proctor endpoints expose
            # every candidate's flags, so there is no "convenient" mode
            # where they are unauthenticated.
            log.warning(
                "PROCTOR_CONSOLE_TOKEN is unset; generated a token for this run only:\n"
                "    %s\n"
                "    console: http://localhost:8000/console",
                settings.console_token,
            )
        # Discrete rules are excluded from the shortest-onset calculation.
        # The check asks whether clock stretching could hide a violation
        # inside the tolerance window, which only makes sense for a rule
        # that requires a condition to *persist*. A discrete rule has an
        # onset of 0 by nature (see Rule.discrete), and including it would
        # peg the minimum at zero and make this warn on every start-up
        # regardless of how the tolerance is actually tuned.
        onsets = [r.onset_ms for r in policy.rules if not r.discrete]
        for warning in validate_against_policy(
            settings.clock_skew_tolerance_ms,
            min(onsets, default=settings.clock_skew_tolerance_ms),
        ):
            log.warning("%s", warning)
        try:
            yield
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker
            store.close()

    app = FastAPI(title="Proctor Gateway", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "policy": policy.name,
            "rules": len(policy.rules),
            "active_sessions": len(engine.active_sessions),
            "proctors_connected": hub.subscriber_count,
        }

    @app.post("/v1/sessions", response_model=CreateSessionResponse, status_code=201)
    async def create_session(body: CreateSessionRequest) -> CreateSessionResponse:
        session = registry.create(body.exam_id, body.candidate_ref)
        engine.open_session(session.session_id, settings.clock())
        board.ensure(
            session.session_id,
            settings.clock(),
            exam_id=body.exam_id,
            candidate_ref=body.candidate_ref,
        )
        key = derive_session_key(settings.master_secret, session.session_id)
        return CreateSessionResponse(
            session_id=session.session_id,
            session_key_b64=base64.b64encode(key).decode("ascii"),
            telemetry_url=f"/v1/sessions/{session.session_id}/telemetry",
            protocol_version=1,
        )

    @app.get("/v1/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        session = registry.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        return {
            "session_id": session.session_id,
            "exam_id": session.exam_id,
            "candidate_ref": session.candidate_ref,
            "connected": session.connected,
            "events_received": session.events_received,
            "last_seq": session.last_seq,
            "attested_build": session.attested_build,
            "integrity_breaches": session.integrity_breaches,
            "breach_counts": session.breach_counts,
            "signal_counts": session.signal_counts,
        }

    @app.websocket("/v1/sessions/{session_id}/telemetry")
    async def telemetry(ws: WebSocket, session_id: str) -> None:
        session = registry.get(session_id)
        if session is None:
            await ws.close(code=WS_CLOSE_POLICY_VIOLATION, reason="unknown session")
            return

        key = derive_session_key(settings.master_secret, session_id)
        await ws.accept()
        session.connected = True
        engine.set_connected(session_id, True)
        log.info("telemetry connected: %s", session_id)

        await broadcast.publish({"kind": "stream_connected", "session_id": session_id})
        try:
            while True:
                raw = await ws.receive_text()
                await _handle_frame(raw, session, key, engine, broadcast, settings.clock())
                # Checkpoint on every frame. `last_seq` is what makes replay
                # detection work, so the window in which a crash could let a
                # client re-send already-used sequence numbers is one frame
                # wide rather than a whole exam.
                registry.persist(session)
        except WebSocketDisconnect:
            log.info("telemetry disconnected: %s", session_id)
        finally:
            session.connected = False
            registry.persist(session)
            # Stop the silence rule from firing forever on a session whose
            # client has gone. Disconnection is its own event, below.
            engine.set_connected(session_id, False)

            if not session.ended_cleanly:
                # The socket closed without the exam formally ending. Closing
                # the socket must not be a cheaper way to stop reporting than
                # falling silent, so it raises its own hard flag.
                await _raise_breach(
                    engine,
                    broadcast,
                    session,
                    IntegrityBreach.ABANDONED,
                    f"telemetry closed after {session.events_received} events "
                    "without a session_end lifecycle event",
                    settings.clock(),
                )

            # A client vanishing mid-exam is itself proctoring-relevant: it
            # is what pulling the network cable looks like from here. The
            # console needs to see it, and it marks the end of the stream.
            await broadcast.publish(
                {
                    "kind": "stream_disconnected",
                    "session_id": session_id,
                    "events_received": session.events_received,
                    "last_seq": session.last_seq,
                    "ended_cleanly": session.ended_cleanly,
                }
            )

    @app.post("/v1/sessions/{session_id}/identity/enrol", status_code=201)
    async def enrol_identity(session_id: str, body: EnrolRequest) -> dict[str, Any]:
        """Establish the reference template from several captures.

        Images are embedded and discarded in this handler. Nothing writes a
        face image to disk anywhere in this service — only the derived
        template, on its own short retention clock.
        """
        session = registry.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        if verifier is None:
            raise HTTPException(status_code=503, detail="identity verification disabled")

        try:
            # Threaded: a real embedder's inference is CPU-bound, and a
            # slow enrolment must not stall the event loop that is also
            # serving every other candidate's telemetry websocket.
            embeddings = [
                await asyncio.to_thread(
                    embedder.embed, _decode_base64(c, MAX_IMAGE_BYTES, "capture")
                )
                for c in body.captures
            ]
            enrolment = build_enrolment(embeddings, verifier.match_policy)
        except EnrolmentError as exc:
            # 422, not 400: the request was well-formed, the captures were
            # not good enough. The message is written for the candidate.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        verifier.enrol(session_id, enrolment)
        store.save_template(
            session_id,
            embedder.name,
            enrolment.reference,
            enrolment.captures,
            enrolment.min_pairwise_similarity,
            settings.clock(),
        )
        return {
            "session_id": session_id,
            "captures": enrolment.captures,
            "min_pairwise_similarity": round(enrolment.min_pairwise_similarity, 4),
            "embedder": embedder.name,
        }

    @app.post("/v1/sessions/{session_id}/identity/probe")
    async def probe_identity(session_id: str, body: ProbeRequest) -> dict[str, Any]:
        session = registry.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        if verifier is None:
            raise HTTPException(status_code=503, detail="identity verification disabled")

        quality = ProbeQuality(
            face_count=body.face_count,
            detector_confidence=body.detector_confidence,
            yaw_deg=body.yaw_deg,
            pitch_deg=body.pitch_deg,
            face_fraction=body.face_fraction,
        )

        # Only embed when the capture is worth comparing. Embedding an
        # unusable frame produces a number that reflects the lighting
        # rather than the person, and that number would then sit in an
        # audit trail looking like evidence.
        embedding = None
        if body.image_b64 and not quality.issues(verifier.match_policy.limits):
            try:
                raw = _decode_base64(body.image_b64, MAX_IMAGE_BYTES, "capture")
                embedding = await asyncio.to_thread(embedder.embed, raw)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            result, finding = verifier.probe(session_id, embedding, quality)
        except LookupError as exc:
            raise HTTPException(status_code=409, detail="session is not enrolled") from exc

        now = settings.clock()
        store.append_identity_check(session_id, result.as_dict(), now)

        if finding is not None and finding.status in _REPORTABLE_IDENTITY:
            await broadcast.publish(
                {"kind": "violation", **_identity_violation(session_id, finding, now)}
            )

        return {"result": result.as_dict(), "finding": finding.as_dict() if finding else None}

    @app.get("/v1/proctor/sessions/{session_id}/identity")
    async def identity_history(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _require_console_token(settings, _bearer(authorization))
        return {
            "session_id": session_id,
            "enabled": verifier is not None,
            "checks": store.load_identity_checks(session_id),
        }

    @app.post("/v1/sessions/{session_id}/audio/chunk", status_code=201)
    async def audio_chunk(session_id: str, body: AudioChunkRequest) -> dict[str, Any]:
        """Transcribe one audio chunk and, if configured, classify its intent.

        The raw audio is decoded, transcribed, and discarded in this
        handler — it is never written to disk. What survives is the
        transcript (short retention) and, if an intent classifier is
        configured, the classification label (long retention). See
        `proctor_gateway.store` for why those two live on different clocks.
        """
        session = registry.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        if not settings.audio_enabled:
            raise HTTPException(
                status_code=503,
                detail="audio pipeline disabled: no consent notice configured",
            )

        try:
            raw = _decode_base64(body.audio_b64, MAX_AUDIO_BYTES, "audio chunk")
            # Threaded for the same reason as face embedding: a real
            # transcriber's inference is CPU-bound and must not stall the
            # event loop serving every other candidate's telemetry socket.
            transcript = await asyncio.to_thread(transcriber.transcribe, raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        now = settings.clock()
        transcript_id = str(uuid.uuid4())
        store.save_transcript(
            transcript_id, session_id, transcript, transcriber.name, body.duration_ms, now
        )

        response: dict[str, Any] = {
            "transcript_id": transcript_id,
            "transcript": transcript,
            "classification": None,
            "finding": None,
        }

        if intent_classifier is not None:
            context = IntentContext(exam_subject=body.exam_subject)
            classification = await asyncio.to_thread(
                intent_classifier.classify, transcript, context
            )
            store.append_audio_check(
                session_id,
                transcript_id,
                str(classification.label),
                classification.confidence,
                classification.classifier,
                now,
            )
            finding = audio_monitor.record(session_id, classification, transcript)
            response["classification"] = classification.as_dict()
            response["finding"] = finding.as_dict() if finding else None

            if finding is not None and finding.status is AudioStatus.SUSTAINED_HELP_SUSPECTED:
                await broadcast.publish(
                    {
                        "kind": "violation",
                        **_audio_violation(session_id, finding, transcript_id, now),
                    }
                )

        return response

    @app.get("/v1/proctor/sessions/{session_id}/audio")
    async def audio_history(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _require_console_token(settings, _bearer(authorization))
        return {
            "session_id": session_id,
            "enabled": settings.audio_enabled,
            "intent_classification_enabled": intent_classifier is not None,
            "checks": store.load_audio_checks(session_id),
        }

    @app.get("/v1/proctor/sessions/{session_id}/audio/transcripts/{transcript_id}")
    async def audio_transcript(
        session_id: str,
        transcript_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Fetch a transcript referenced from a flag, while it still exists.

        Deliberately absent from the violation record itself — see
        `_audio_violation`. Once the transcript's own retention window
        passes, this returns 404 like any other purged record: the flag
        and its classification survive, the words do not.
        """
        _require_console_token(settings, _bearer(authorization))
        record = store.load_transcript(transcript_id)
        if record is None or record["session_id"] != session_id:
            raise HTTPException(
                status_code=404, detail="transcript not available (purged or unknown)"
            )
        return record

    @app.get("/v1/proctor/sessions/{session_id}/violations/{violation_id}")
    async def violation_evidence(
        session_id: str,
        violation_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """The measurements behind one flag, fetched on demand.

        Deliberately not embedded in the board snapshot — see
        `TimelineEntry.evidence_count`. This is the one place a proctor
        actually needs the raw samples: after opening a specific flag to
        decide whether it warrants review, not while scanning the queue.
        """
        _require_console_token(settings, _bearer(authorization))
        record = store.load_violation(violation_id)
        if record is None or record["session_id"] != session_id:
            raise HTTPException(status_code=404, detail="violation not found")
        return record

    @app.get("/v1/sessions/{session_id}/media/config")
    async def media_config(session_id: str) -> dict[str, Any]:
        """Whether the client should bother connecting at all, and where.

        Never returns the API key or secret — only the connection URL, so
        a compromised renderer learns nothing that helps it mint its own
        tokens; it can only ask this same gateway for one, same as always.
        """
        if registry.get(session_id) is None:
            raise HTTPException(status_code=404, detail="unknown session")
        return {
            "enabled": settings.media_enabled,
            "url": settings.client_livekit_url or None,
        }

    @app.post("/v1/sessions/{session_id}/consent")
    async def record_consent(session_id: str) -> dict[str, Any]:
        """The candidate accepted the disclaimer.

        Recording is deliberately *not* started here. At the moment
        consent is given the candidate has not joined the media room yet —
        the client shows the dialog before it opens the camera, which is
        the whole point of a consent gate — so there is no room to record
        and LiveKit answers `requested room does not exist`. The recording
        is started instead when the room actually starts, from the
        `room_started` webhook (see `_start_recording_for_room`), which
        also means a candidate who drops and reconnects gets a recording
        without anything having to retry.

        The lifecycle event on the signed telemetry stream, emitted by the
        client's main process, is the authoritative record that consent
        was given; this endpoint exists so the server learns about it
        without having to parse the telemetry stream for control flow.
        """
        if registry.get(session_id) is None:
            raise HTTPException(status_code=404, detail="unknown session")
        log.info("consent recorded for %s", session_id)
        return {
            "consent": "recorded",
            "recording": "starts when the media room opens" if settings.media_enabled else None,
        }

    @app.post("/v1/sessions/{session_id}/media/token")
    async def candidate_media_token(session_id: str) -> dict[str, Any]:
        """A publish-only token for the candidate's own room.

        Deliberately not accepting a role parameter from the caller — see
        `VideoGrant.publisher`. The grant this issues can never subscribe,
        record, or admin the room; a candidate-scoped identity that could
        do any of those would be a much more useful thing for a
        compromised renderer to have extracted than a publish-only one.

        Authorised the same way the identity and audio endpoints are:
        knowledge of `session_id`, a 96-bit random value never guessable
        and never logged in a way this service controls — see
        ARCHITECTURE.md § 5.6 for why a stronger, signature-based scheme
        is a documented hardening step rather than done here.
        """
        session = registry.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        if not settings.media_enabled:
            raise HTTPException(
                status_code=503, detail="media disabled: no consent notice configured"
            )
        token = media_provider.candidate_token(session_id, identity="candidate")
        return {"url": settings.client_livekit_url, "token": token}

    @app.post("/v1/proctor/sessions/{session_id}/media/token")
    async def proctor_media_token(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """A subscribe-and-record token for a proctor watching this session.

        Console-token gated. This is the elevation-of-privilege boundary
        that matters most in this module: nothing a candidate holds can
        ever reach this grant shape (`VideoGrant.subscriber`, which can
        watch and can start a recording) — see
        test_a_candidate_cannot_obtain_a_proctor_grant.
        """
        _require_console_token(settings, _bearer(authorization))
        if registry.get(session_id) is None:
            raise HTTPException(status_code=404, detail="unknown session")
        if not settings.media_enabled:
            raise HTTPException(
                status_code=503, detail="media disabled: no consent notice configured"
            )
        # "proctor" rather than a per-reviewer identity: there is no
        # per-proctor identity system yet — the same known gap already
        # noted for the console generally (nothing here newly introduces
        # it; it is worth closing before this is relied on for real review).
        token = media_provider.proctor_token(session_id, identity="proctor")
        return {"url": settings.client_livekit_url, "token": token}

    @app.post("/v1/proctor/sessions/{session_id}/media/recording/start", status_code=201)
    async def start_recording(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _require_console_token(settings, _bearer(authorization))
        if registry.get(session_id) is None:
            raise HTTPException(status_code=404, detail="unknown session")
        if not settings.media_enabled:
            raise HTTPException(
                status_code=503, detail="media disabled: no consent notice configured"
            )

        now = settings.clock()
        try:
            # Threaded: the real provider makes a blocking network call to
            # start Egress, the same reason embed/transcribe/classify run
            # through asyncio.to_thread rather than directly in the handler.
            started = await asyncio.to_thread(media_provider.start_recording, session_id)
        except RoomProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        record = RecordingRecord(
            recording_id=str(uuid.uuid4()),
            session_id=session_id,
            status=RecordingStatus.REQUESTED,
            requested_ms=now,
            egress_id=started.egress_id,
        )
        store.save_recording(record.as_dict(), now)
        return record.as_dict()

    @app.post("/v1/proctor/sessions/{session_id}/media/recording/{recording_id}/stop")
    async def stop_recording(
        session_id: str,
        recording_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_console_token(settings, _bearer(authorization))
        stored = store.load_recording(recording_id)
        if stored is None or stored["session_id"] != session_id:
            raise HTTPException(status_code=404, detail="recording not found")

        record = _recording_from_dict(stored)
        try:
            record.transition(RecordingStatus.STOPPING, settings.clock())
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if record.egress_id is not None:
            try:
                await asyncio.to_thread(media_provider.stop_recording, session_id, record.egress_id)
            except RoomProviderError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        store.save_recording(record.as_dict(), settings.clock())
        return record.as_dict()

    @app.get("/v1/proctor/sessions/{session_id}/media/recordings")
    async def list_recordings(
        session_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _require_console_token(settings, _bearer(authorization))
        return {"recordings": store.load_recordings_for_session(session_id)}

    @app.post("/v1/media/webhook")
    async def media_webhook(request: Request) -> dict[str, Any]:
        """LiveKit's room/recording lifecycle notifications.

        Authenticated by LiveKit's own webhook signature scheme, not the
        console bearer token — this endpoint is called by the SFU, not a
        proctor's browser. See `proctor_media.tokens.verify_webhook` for
        exactly what is checked and its confidence caveats.
        """
        body = await request.body()
        try:
            event = verify_webhook(
                body,
                # LiveKit 1.x sends the raw JWT in `Authorization`, with no
                # `Bearer ` prefix. `Authorize` is checked as a fallback
                # because older releases used that name, and a webhook
                # rejected over a header spelling is a silent
                # no-recordings failure that looks like nothing at all.
                request.headers.get("Authorization") or request.headers.get("Authorize"),
                LiveKitCredentials(settings.livekit_api_key, settings.livekit_api_secret),
            )
        except TokenError as exc:
            # Logged, not only returned. The 401 goes back to LiveKit,
            # which does not read it; an operator whose recordings are
            # silently never starting needs to see the reason on this
            # side, and "key mismatch" vs "body hash mismatch" are very
            # different problems to go looking for.
            log.warning("rejected media webhook: %s", exc)
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        if event.event == "room_started":
            await _start_recording_for_room(
                store, media_provider, settings, event.room_name, settings.clock()
            )
        else:
            _apply_webhook_event(store, event, settings.clock())
        return {"status": "ok"}

    @app.get("/v1/proctor/sessions")
    async def proctor_sessions(authorization: str | None = Header(default=None)):
        _require_console_token(settings, _bearer(authorization))
        return {"sessions": board.snapshot(settings.clock())}

    @app.websocket("/v1/proctor/stream")
    async def proctor_stream(ws: WebSocket) -> None:
        # Browsers cannot set headers on a WebSocket handshake, so the token
        # travels as a subprotocol rather than a query parameter — query
        # strings end up in access logs and proxy history, and this one
        # grants access to every candidate's flags.
        offered = ws.headers.get("sec-websocket-protocol", "")
        token = _token_from_subprotocol(offered)
        if not _console_token_ok(settings, token):
            log.warning("rejected unauthenticated proctor stream connection")
            await ws.close(code=WS_CLOSE_POLICY_VIOLATION, reason="unauthorised")
            return

        await ws.accept(subprotocol=CONSOLE_SUBPROTOCOL)
        async with hub.subscribe() as queue:
            await ws.send_json(
                {
                    "kind": "hello",
                    "policy": policy.name,
                    "sessions": board.snapshot(settings.clock()),
                }
            )
            try:
                while True:
                    message = await queue.get()
                    await ws.send_json(message)
            except WebSocketDisconnect:
                return

    # The console page itself carries no data — every byte it renders comes
    # from the token-gated endpoints above — so serving it openly is fine.
    console_dir = Path(__file__).resolve().parents[2] / "apps" / "console"
    if console_dir.is_dir():
        app.mount("/console", StaticFiles(directory=console_dir, html=True), name="console")

    app.state.engine = engine
    app.state.registry = registry
    app.state.hub = hub
    app.state.board = board
    app.state.store = store
    app.state.verifier = verifier
    app.state.embedder = embedder
    app.state.transcriber = transcriber
    app.state.intent_classifier = intent_classifier
    app.state.audio_monitor = audio_monitor
    app.state.media_provider = media_provider
    app.state.settings = settings
    return app


async def _handle_frame(
    raw: str,
    session,
    key: bytes,
    engine: FusionEngine,
    hub: _Broadcast,
    server_ms: int,
) -> None:
    try:
        envelope = verify_frame(raw, key)
    except SignatureError as exc:
        await _raise_breach(
            engine, hub, session, IntegrityBreach.BAD_SIGNATURE, str(exc), server_ms
        )
        return

    if envelope.session_id != session.session_id:
        # A frame validly signed for this session key but naming another
        # session. Should be impossible; treat as hostile.
        await _raise_breach(
            engine,
            hub,
            session,
            IntegrityBreach.BAD_SIGNATURE,
            "envelope session_id does not match connection",
            server_ms,
        )
        return

    result = session.check(envelope.seq, envelope.ts_monotonic_ms, server_ms)
    if not result.ok and result.breach is not None:
        await _raise_breach(engine, hub, session, result.breach, result.detail, server_ms)
        if result.breach is IntegrityBreach.REPLAY:
            # A replayed "face present" must not clear a real absence, so
            # replays are flagged and discarded. A gap, by contrast, is
            # flagged but the frame is still evaluated: the events that
            # went missing are gone either way, and refusing to look at
            # what did arrive would let one dropped frame blind the rest
            # of the exam.
            return

    payload = envelope.payload
    session.count_signal(payload.type)
    if isinstance(payload, Attestation):
        session.attested_build = payload.client_build
    elif isinstance(payload, Lifecycle) and payload.phase is LifecyclePhase.SESSION_END:
        session.ended_cleanly = True
        engine.close_session(session.session_id)

    for violation in engine.on_event(envelope, server_ms):
        await hub.publish({"kind": "violation", **violation.as_dict()})


async def _raise_breach(
    engine: FusionEngine,
    hub: _Broadcast,
    session,
    breach: IntegrityBreach,
    detail: str,
    server_ms: int,
) -> None:
    session.record_breach(str(breach), detail)
    if not session.should_report_breach(str(breach), server_ms):
        # Still counted, just not re-broadcast. See Session.should_report_breach.
        return

    occurrences = session.breach_counts.get(str(breach), 1)
    log.warning(
        "integrity breach %s on %s (x%d): %s",
        breach,
        session.session_id,
        occurrences,
        detail,
    )
    annotated = detail if occurrences == 1 else f"{detail} [x{occurrences} so far]"
    violation = engine.report_integrity_breach(
        session.session_id, str(breach), annotated, server_ms
    )
    await hub.publish({"kind": "violation", **violation.as_dict()})


RESTORED_VIOLATIONS_PER_SESSION = 200


def _build_verifier(settings: Settings) -> IdentityVerifier | None:
    """Construct the verifier, or None when identity checks are not configured.

    Returns None rather than defaulting a threshold. There is no cosine
    cutoff that is correct across populations — false-match rates vary by
    one to two orders of magnitude between demographic groups — so a
    default here would be a number masquerading as a decision someone made.
    Both the threshold and the record of what it was calibrated against
    must be supplied before any candidate is measured against it.
    """
    if not settings.identity_enabled:
        return None
    return IdentityVerifier(
        MatchPolicy(
            threshold=Threshold(
                value=float(settings.identity_threshold),
                calibrated_on=settings.identity_calibrated_on,
            )
        )
    )


def _restore_templates(store: Any, verifier: IdentityVerifier, embedder: Any) -> None:
    """Reload enrolment templates so a restart does not force re-enrolment.

    Templates embedded by a *different* model are dropped rather than
    reused: comparing a vector from one network against a probe from
    another produces a meaningless similarity, and a meaningless similarity
    that crosses a threshold is an accusation.
    """
    from proctor_identity.matching import Enrolment  # noqa: PLC0415 - avoid cycle

    dropped = 0
    for session_id, record in store.load_templates().items():
        if record["embedder"] != embedder.name:
            dropped += 1
            continue
        verifier.enrol(
            session_id,
            Enrolment(
                reference=record["reference"],
                captures=record["captures"],
                min_pairwise_similarity=record["min_pairwise"],
            ),
        )
    if dropped:
        log.warning(
            "dropped %d enrolment template(s) embedded by a different model; "
            "those sessions must re-enrol",
            dropped,
        )


def _purge_templates(store: Any, settings: Settings) -> None:
    if settings.template_retention_days <= 0:
        return
    cutoff = settings.clock() - settings.template_retention_days * 86_400_000
    removed = store.purge_templates_older_than(cutoff)
    if removed:
        log.info(
            "retention: removed %d face template(s) older than %d day(s)",
            removed,
            settings.template_retention_days,
        )


def _purge_transcripts(store: Any, settings: Settings) -> None:
    if settings.audio_transcript_retention_days <= 0:
        return
    cutoff = settings.clock() - settings.audio_transcript_retention_days * 86_400_000
    removed = store.purge_transcripts_older_than(cutoff)
    if removed:
        log.info(
            "retention: removed %d audio transcript(s) older than %d day(s)",
            removed,
            settings.audio_transcript_retention_days,
        )


def _purge_recordings(store: Any, settings: Settings) -> None:
    if settings.recording_retention_days <= 0:
        return
    cutoff = settings.clock() - settings.recording_retention_days * 86_400_000
    removed = store.purge_recordings_older_than(cutoff)
    if removed:
        log.info(
            "retention: removed %d recording reference(s) older than %d day(s) "
            "(the referenced objects in your storage backend are not deleted "
            "by this — see purge_recordings_older_than)",
            removed,
            settings.recording_retention_days,
        )


def _recording_from_dict(stored: dict[str, Any]) -> RecordingRecord:
    return RecordingRecord(
        recording_id=stored["recording_id"],
        session_id=stored["session_id"],
        status=RecordingStatus(stored["status"]),
        requested_ms=stored["requested_ms"],
        started_ms=stored.get("started_ms"),
        stopped_ms=stored.get("stopped_ms"),
        egress_id=stored.get("egress_id"),
        storage_ref=stored.get("storage_ref"),
        failure_reason=stored.get("failure_reason"),
    )


def _session_id_from_room(room_name: str | None) -> str | None:
    """Rooms are named `proctor-{session_id}` — see `provider._room_name`."""
    if not room_name or not room_name.startswith("proctor-"):
        return None
    return room_name[len("proctor-") :]


async def _start_recording_for_room(
    store: Any, provider: Any, settings: Settings, room_name: str | None, now_ms: int
) -> None:
    """Start a recording when the candidate's room actually opens.

    This is where recording begins, rather than at consent: the client
    shows its disclaimer before opening the camera, so at consent time
    there is no room and LiveKit rejects the egress request outright.

    Idempotent by session, because `room_started` can be delivered more
    than once — LiveKit retries, and a candidate who reconnects opens the
    room again. Starting a second egress for a session that already has a
    live one would produce two files and two rows for one exam.
    """
    session_id = _session_id_from_room(room_name)
    if session_id is None or not settings.media_enabled:
        return

    live = {RecordingStatus.REQUESTED, RecordingStatus.ACTIVE, RecordingStatus.STOPPING}
    for existing in store.load_recordings_for_session(session_id):
        if RecordingStatus(existing["status"]) in live:
            return

    try:
        started = await asyncio.to_thread(provider.start_recording, session_id)
    except RoomProviderError as exc:
        # Logged, not raised. A 500 here makes LiveKit retry the same
        # delivery, and a recording that could not start is a gap for a
        # reviewer to see rather than a reason to break the webhook.
        log.warning("could not start recording for %s: %s", session_id, exc)
        return

    # The `egress_started` webhook routinely arrives before this function
    # gets back from the RPC and writes its row — LiveKit is fast and the
    # two are racing. If the webhook won, it has already created the row
    # (see `_apply_webhook_event`) at a *later* status, and overwriting it
    # with REQUESTED here would walk the lifecycle backwards.
    if store.find_recording_by_egress_id(started.egress_id) is not None:
        return

    store.save_recording(
        RecordingRecord(
            recording_id=started.egress_id,
            session_id=session_id,
            status=RecordingStatus.REQUESTED,
            requested_ms=now_ms,
            egress_id=started.egress_id,
        ).as_dict(),
        now_ms,
    )
    log.info("recording %s started for %s", started.egress_id, session_id)


def _apply_webhook_event(store: Any, event: Any, now_ms: int) -> None:
    """Update a recording's stored status from a LiveKit webhook.

    Looked up by `egress_id`, not by scanning for a room name match: an
    event with no egress info (a plain room_started/room_finished with no
    recording involved) has nothing here to update, and is silently
    ignored rather than treated as an error — most webhook deliveries this
    endpoint receives will be exactly that.
    """
    if not event.egress_id:
        return

    with_session = store.find_recording_by_egress_id(event.egress_id)
    if with_session is None:
        session_id = _session_id_from_room(event.room_name)
        if session_id is None:
            log.warning(
                "webhook for unknown egress_id %s (event=%s, room=%s); ignoring",
                event.egress_id,
                event.event,
                event.room_name,
            )
            return
        # The other half of the race in `_start_recording_for_room`: this
        # webhook beat our own RPC call's write. The row is created here
        # instead, keyed on the egress id so that whichever side lands
        # first wins and the other finds it rather than duplicating it.
        # This also covers a gateway restart mid-recording, where the
        # in-flight egress is otherwise never accounted for.
        with_session = RecordingRecord(
            recording_id=event.egress_id,
            session_id=session_id,
            status=RecordingStatus.REQUESTED,
            requested_ms=now_ms,
            egress_id=event.egress_id,
        ).as_dict()
        store.save_recording(with_session, now_ms)

    record = _recording_from_dict(with_session)
    target = _STATUS_FOR_EVENT.get(event.event)
    if target is None:
        return

    if event.event == "egress_ended":
        succeeded = (event.egress_status or "").upper() in ("EGRESS_COMPLETE", "COMPLETE")
        target = RecordingStatus.AVAILABLE if succeeded else RecordingStatus.FAILED
        if event.storage_ref:
            record.storage_ref = event.storage_ref
        if not succeeded:
            record.failure_reason = event.egress_status

    try:
        record.transition(target, now_ms)
    except InvalidTransition:
        # An out-of-order or duplicate delivery — LiveKit does not
        # guarantee ordering across webhook retries. Logged, not raised:
        # a webhook endpoint that 500s on a delivery order it does not
        # control invites the sender to retry the same problematic
        # delivery forever.
        log.warning(
            "ignoring out-of-order webhook: egress %s already %s, got %s",
            event.egress_id,
            record.status,
            target,
        )
        return

    store.save_recording(record.as_dict(), now_ms)


_STATUS_FOR_EVENT = {
    "egress_started": RecordingStatus.ACTIVE,
    "egress_ended": RecordingStatus.AVAILABLE,  # refined in _apply_webhook_event
}


def _restore(
    store: Any,
    registry: SessionRegistry,
    board: TriageBoard,
    engine: FusionEngine,
    settings: Settings,
) -> int:
    """Rebuild in-memory state from the durable store.

    Order matters: sessions first so the board has metadata to attach, then
    violations replayed oldest-first because both the timeline and the
    decaying score depend on arrival order.

    Note what is deliberately *not* restored: the fusion engine's per-rule
    onset timers. Those are transient — a candidate mid-look-away when the
    gateway restarts gets a fresh onset window. That errs toward not
    flagging, which is the right direction to err, and a candidate cannot
    trigger a restart to exploit it.
    """
    records = store.load_sessions()
    if not records:
        return 0

    restored = registry.restore(records)
    now = settings.clock()

    for record in records:
        engine.open_session(record.session_id, now)
        # Sessions come back disconnected, so the silence rule must not
        # fire on them until a client actually attaches.
        engine.set_connected(record.session_id, False)
        if record.ended_cleanly:
            engine.close_session(record.session_id)
        board.ensure(
            record.session_id,
            record.created_ms,
            exam_id=record.exam_id,
            candidate_ref=record.candidate_ref,
            connected=False,
            ended_cleanly=record.ended_cleanly,
            events_received=record.events_received,
        )

    for session_id, violations in store.load_violations(RESTORED_VIOLATIONS_PER_SESSION).items():
        for violation in violations:
            board.record_violation(violation, violation.get("recorded_ms", now))
        log.debug("restored %d violation(s) for %s", len(violations), session_id)

    return restored


def _purge(store: Any, settings: Settings) -> None:
    if settings.retention_days <= 0:
        return
    cutoff = settings.clock() - settings.retention_days * 86_400_000
    violations, sessions = store.purge_older_than(cutoff)
    if violations or sessions:
        log.info(
            "retention: removed %d violation(s) and %d session(s) older than %d days",
            violations,
            sessions,
            settings.retention_days,
        )


async def _tick_loop(engine: FusionEngine, hub: _Broadcast, settings: Settings) -> None:
    """Drives absence-of-telemetry rules on a wall clock."""
    interval = settings.tick_interval_ms / 1000
    while True:
        await asyncio.sleep(interval)
        try:
            for violation in engine.on_tick(settings.clock()):
                await hub.publish({"kind": "violation", **violation.as_dict()})
        except Exception:  # noqa: BLE001 - a tick failure must not kill the loop
            log.exception("tick evaluation failed")


app = create_app()
