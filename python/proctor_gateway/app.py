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
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
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

    def __init__(self, hub: ProctorHub, board: TriageBoard, settings: Settings) -> None:
        self._hub = hub
        self._board = board
        self._settings = settings

    async def publish(self, message: dict[str, Any]) -> None:
        now = self._settings.clock()
        kind = message.get("kind")
        session_id = message.get("session_id")

        if kind == "violation" and session_id:
            self._board.record_violation(message, now)
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
    board = TriageBoard()
    broadcast = _Broadcast(hub, board, settings)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        ticker = asyncio.create_task(_tick_loop(engine, broadcast, settings))
        log.info("gateway up: policy=%s rules=%d", policy.name, len(policy.rules))
        if not settings.has_persistent_secret:
            log.warning(
                "PROCTOR_MASTER_SECRET is unset; using an ephemeral secret. "
                "Sessions will not survive a restart. Do not run this way in production."
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
