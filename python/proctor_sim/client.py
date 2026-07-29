"""Simulated edge client, including the ways a hostile one misbehaves."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

from proctor_protocol import Envelope, Lifecycle, LifecyclePhase, sign_envelope

from .scenarios import ScriptedEvent


class Tamper(StrEnum):
    """Ways a client under the candidate's control can misbehave.

    Each of these is cheap to do in practice. A candidate who can run a
    debugger against their own machine can do any of them in an afternoon,
    which is why the gateway checks for all of them.
    """

    NONE = "none"
    FORGE_SIGNATURE = "forge_signature"
    """Sign with the wrong key — what an attacker gets before extracting
    the real session key."""

    REPLAY = "replay"
    """Re-send an earlier 'everything is fine' frame to paper over a gap."""

    DROP_EVENTS = "drop_events"
    """Silently discard the incriminating window, keeping seq contiguous
    on the wire is impossible — which is the whole point of seq."""

    SEQUENCE_STALL = "sequence_stall"
    """Keep transmitting but stop advancing seq, hoping the server keys
    off arrival rather than order."""

    CLOCK_SKEW = "clock_skew"
    """Jump the client clock so a long violation looks brief."""

    GO_SILENT = "go_silent"
    """Stop transmitting entirely partway through."""


@dataclass(slots=True)
class SimulatedClient:
    """Turns a behaviour script into signed wire frames.

    Kept transport-free on purpose: `frames()` yields strings, and the
    caller decides whether they go over a real WebSocket or straight into
    a test harness.
    """

    session_id: str
    session_key: bytes
    tamper: Tamper = Tamper.NONE
    tamper_window_ms: tuple[int, int] = (4_000, 8_000)
    end_cleanly: bool = True
    """Whether to send a `session_end` lifecycle event before disconnecting.

    A well-behaved client always does. Setting this False models a candidate
    who simply kills the app, which raises `stream_abandoned`."""

    @classmethod
    def from_enrolment(
        cls, session_id: str, session_key_b64: str, tamper: Tamper = Tamper.NONE
    ) -> SimulatedClient:
        return cls(
            session_id=session_id,
            session_key=base64.b64decode(session_key_b64),
            tamper=tamper,
        )

    def frames(self, script: list[ScriptedEvent]) -> Iterator[tuple[int, str]]:
        """Yield `(t_ms, frame)` pairs ready to transmit."""
        seq = 0
        lo, hi = self.tamper_window_ms
        last_frame: str | None = None
        stalled_seq: int | None = None

        for event in script:
            in_window = lo <= event.t_ms < hi

            if self.tamper is Tamper.GO_SILENT and event.t_ms >= lo:
                return

            if self.tamper is Tamper.DROP_EVENTS and in_window:
                # Drop the payload but still burn the sequence number, which
                # is what a naive attacker does: the gap is visible to the
                # server precisely because seq is authenticated.
                seq += 1
                continue

            if self.tamper is Tamper.REPLAY and in_window and last_frame is not None:
                yield event.t_ms, last_frame
                continue

            effective_seq = seq
            if self.tamper is Tamper.SEQUENCE_STALL and in_window:
                if stalled_seq is None:
                    stalled_seq = seq
                effective_seq = stalled_seq

            client_ms = event.t_ms
            monotonic_ms = event.t_ms
            if self.tamper is Tamper.CLOCK_SKEW and event.t_ms >= lo:
                # Rewind both clocks, so the violation window appears not to
                # have elapsed. The monotonic counter is the one that matters:
                # it is what the fusion engine times rules against.
                client_ms = max(0, event.t_ms - (hi - lo))
                monotonic_ms = max(0, event.t_ms - (hi - lo))

            envelope = Envelope(
                session_id=self.session_id,
                seq=effective_seq,
                ts_client_ms=client_ms,
                ts_monotonic_ms=monotonic_ms,
                payload=event.payload,
            )

            key = self.session_key
            if self.tamper is Tamper.FORGE_SIGNATURE and in_window:
                key = b"\x00" * 32

            frame = sign_envelope(envelope, key)
            last_frame = frame
            seq += 1
            yield event.t_ms, frame

        if self.end_cleanly and script:
            end_ms = script[-1].t_ms + 1
            closing = Envelope(
                session_id=self.session_id,
                seq=seq,
                ts_client_ms=end_ms,
                ts_monotonic_ms=end_ms,
                payload=Lifecycle(phase=LifecyclePhase.SESSION_END),
            )
            yield end_ms, sign_envelope(closing, self.session_key)
