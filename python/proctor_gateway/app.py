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
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from proctor_fusion import FusionEngine, load_policy
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

log = logging.getLogger("proctor.gateway")

WS_CLOSE_POLICY_VIOLATION = 1008


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
    registry = SessionRegistry(skew_tolerance_ms=settings.clock_skew_tolerance_ms)
    hub = ProctorHub()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        ticker = asyncio.create_task(_tick_loop(engine, hub, settings))
        log.info("gateway up: policy=%s rules=%d", policy.name, len(policy.rules))
        if not settings.has_persistent_secret:
            log.warning(
                "PROCTOR_MASTER_SECRET is unset; using an ephemeral secret. "
                "Sessions will not survive a restart. Do not run this way in production."
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

        await hub.publish({"kind": "stream_connected", "session_id": session_id})
        try:
            while True:
                raw = await ws.receive_text()
                await _handle_frame(raw, session, key, engine, hub, settings.clock())
        except WebSocketDisconnect:
            log.info("telemetry disconnected: %s", session_id)
        finally:
            session.connected = False
            # Stop the silence rule from firing forever on a session whose
            # client has gone. Disconnection is its own event, below.
            engine.set_connected(session_id, False)

            if not session.ended_cleanly:
                # The socket closed without the exam formally ending. Closing
                # the socket must not be a cheaper way to stop reporting than
                # falling silent, so it raises its own hard flag.
                await _raise_breach(
                    engine,
                    hub,
                    session,
                    IntegrityBreach.ABANDONED,
                    f"telemetry closed after {session.events_received} events "
                    "without a session_end lifecycle event",
                    settings.clock(),
                )

            # A client vanishing mid-exam is itself proctoring-relevant: it
            # is what pulling the network cable looks like from here. The
            # console needs to see it, and it marks the end of the stream.
            await hub.publish(
                {
                    "kind": "stream_disconnected",
                    "session_id": session_id,
                    "events_received": session.events_received,
                    "last_seq": session.last_seq,
                    "ended_cleanly": session.ended_cleanly,
                }
            )

    @app.websocket("/v1/proctor/stream")
    async def proctor_stream(ws: WebSocket) -> None:
        await ws.accept()
        async with hub.subscribe() as queue:
            await ws.send_json(
                {
                    "kind": "hello",
                    "policy": policy.name,
                    "active_sessions": list(engine.active_sessions),
                }
            )
            try:
                while True:
                    message = await queue.get()
                    await ws.send_json(message)
            except WebSocketDisconnect:
                return

    app.state.engine = engine
    app.state.registry = registry
    app.state.hub = hub
    app.state.settings = settings
    return app


async def _handle_frame(
    raw: str,
    session,
    key: bytes,
    engine: FusionEngine,
    hub: ProctorHub,
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
    if isinstance(payload, Attestation):
        session.attested_build = payload.client_build
    elif isinstance(payload, Lifecycle) and payload.phase is LifecyclePhase.SESSION_END:
        session.ended_cleanly = True
        engine.close_session(session.session_id)

    for violation in engine.on_event(envelope, server_ms):
        await hub.publish({"kind": "violation", **violation.as_dict()})


async def _raise_breach(
    engine: FusionEngine,
    hub: ProctorHub,
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


async def _tick_loop(engine: FusionEngine, hub: ProctorHub, settings: Settings) -> None:
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
