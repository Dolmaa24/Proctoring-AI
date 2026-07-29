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

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from proctor_fusion import FusionEngine, load_policy
from proctor_identity import (
    EnrolmentError,
    IdentityStatus,
    IdentityVerifier,
    MatchPolicy,
    ProbeQuality,
    Threshold,
    build_enrolment,
    load_embedder,
)
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

_REPORTABLE_IDENTITY = {
    IdentityStatus.MISMATCH_SUSPECTED,
    IdentityStatus.UNOBSERVABLE,
}


def _decode_image(image_b64: str) -> bytes:
    try:
        raw = base64.b64decode(image_b64, validate=True)
    except Exception as exc:
        raise ValueError("capture is not valid base64") from exc
    if not raw:
        raise ValueError("capture is empty")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"capture exceeds {MAX_IMAGE_BYTES} bytes")
    return raw


def _identity_violation(session_id: str, finding: Any, now_ms: int) -> dict[str, Any]:
    """Render an identity finding into the ordinary violation shape.

    Reusing the violation record rather than inventing a parallel channel
    means identity inherits everything already true of a flag: it reaches
    the console, it lands in the audit trail and retention, and — the point
    — it is `action: flag` with `requires_human_review: true` like every
    other rule. Nothing here ends an exam.
    """
    hard = finding.status is IdentityStatus.MISMATCH_SUSPECTED
    return {
        "violation_id": str(uuid.uuid4()),
        "session_id": session_id,
        "rule_id": ("identity_mismatch" if hard else "identity_unobservable"),
        "severity": "hard" if hard else "info",
        "action": "flag",
        "requires_human_review": True,
        "message": finding.message,
        "opened_at_ms": now_ms,
        "fired_at_ms": now_ms,
        "duration_ms": 0,
        "resolved": False,
        # The similarities and the threshold they were judged against are
        # the evidence. A reviewer needs "0.41 against 0.55, calibrated on
        # X", not the word "mismatch".
        "evidence": [
            {
                "server_ts_ms": now_ms,
                "client_ts_ms": now_ms,
                "seq": -1,
                "payload": finding.as_dict(),
            }
        ],
    }


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

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        # Purge before restore, not after. The other order loads expired
        # rows into memory and only then deletes them from disk, so the
        # board keeps showing candidates whose data was supposed to be gone
        # and their sessions still accept a telemetry socket. Retention that
        # only applies to the database is not retention.
        _purge(store, settings)
        _purge_templates(store, settings)
        restored = _restore(store, registry, board, engine, settings)
        if verifier is not None:
            _restore_templates(store, verifier, embedder)
        if restored:
            log.info("restored %d session(s) from %s", restored, settings.db_path)
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
        for warning in validate_against_policy(
            settings.clock_skew_tolerance_ms,
            min((r.onset_ms for r in policy.rules), default=settings.clock_skew_tolerance_ms),
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
            embeddings = [embedder.embed(_decode_image(c)) for c in body.captures]
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
                embedding = embedder.embed(_decode_image(body.image_b64))
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
