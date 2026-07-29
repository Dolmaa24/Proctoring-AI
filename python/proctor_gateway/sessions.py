"""Session registry and stream-integrity checking.

The integrity checks here are the part of the system that a hostile client
actually has to get past. Signature verification proves a frame came from a
provisioned client; these checks prove the client did not *withhold* frames,
which is the far more likely attack.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .store import SessionRecord

BREACH_COOLDOWN_MS = 10_000
"""Minimum gap between broadcasts of the same breach kind on one session."""

MAX_RECORDED_BREACHES = 50
"""Cap on the durable per-session breach log. Totals live in `breach_counts`."""


def now_ms() -> int:
    return int(time.time() * 1000)


def new_session_id() -> str:
    return f"sess-{secrets.token_hex(12)}"


class IntegrityBreach(StrEnum):
    REPLAY = "stream_replay"
    SEQUENCE_GAP = "stream_sequence_gap"
    CLOCK_SKEW = "stream_clock_skew"
    BAD_SIGNATURE = "stream_bad_signature"
    ATTESTATION_MISMATCH = "stream_attestation_mismatch"
    ABANDONED = "stream_abandoned"
    """Telemetry socket closed without the exam ever formally ending.

    Distinct from `stream_silent`: this is a client that went away rather
    than one that went quiet. Closing the socket must not be a cheaper way
    to stop reporting than simply falling silent."""


@dataclass(slots=True)
class IntegrityResult:
    ok: bool
    breach: IntegrityBreach | None = None
    detail: str = ""


@dataclass(slots=True)
class Session:
    session_id: str
    exam_id: str
    candidate_ref: str
    created_ms: int
    last_seq: int = -1
    first_client_ms: int | None = None
    first_server_ms: int | None = None
    events_received: int = 0
    connected: bool = False
    ended_cleanly: bool = False
    attested_build: str | None = None
    integrity_breaches: list[str] = field(default_factory=list)
    skew_tolerance_ms: int = 2_000
    last_monotonic_ms: int = -1
    breach_counts: dict[str, int] = field(default_factory=dict)
    signal_counts: dict[str, int] = field(default_factory=dict)
    """Per-payload-type tally of what this client actually sent.

    Operationally useful on its own — "this candidate's client never sent a
    single gaze signal" is the shape of a broken camera or a stripped-down
    client, and neither is visible from the violation stream alone.
    """
    _breach_last_ms: dict[str, int] = field(default_factory=dict)

    def count_signal(self, payload_type: str) -> None:
        self.signal_counts[payload_type] = self.signal_counts.get(payload_type, 0) + 1

    def to_record(self) -> SessionRecord:
        return SessionRecord(
            session_id=self.session_id,
            exam_id=self.exam_id,
            candidate_ref=self.candidate_ref,
            created_ms=self.created_ms,
            last_seq=self.last_seq,
            last_monotonic_ms=self.last_monotonic_ms,
            first_client_ms=self.first_client_ms,
            first_server_ms=self.first_server_ms,
            events_received=self.events_received,
            ended_cleanly=self.ended_cleanly,
            attested_build=self.attested_build,
            integrity_breaches=list(self.integrity_breaches),
            breach_counts=dict(self.breach_counts),
            signal_counts=dict(self.signal_counts),
        )

    def should_report_breach(
        self, kind: str, now_ms: int, cooldown_ms: int = BREACH_COOLDOWN_MS
    ) -> bool:
        """Rate-limit repeat breaches of the same kind, but never lose the count.

        A clock that drifts, or a client that keeps replaying, produces a
        breach on *every frame* — hundreds per minute. Broadcasting each one
        buries the proctor console in identical rows and makes the genuinely
        interesting flag impossible to find, which is a safety problem, not
        just a noise one. The count is still tracked in full, so review sees
        "1,847 occurrences" rather than 1,847 rows.
        """
        self.breach_counts[kind] = self.breach_counts.get(kind, 0) + 1
        last = self._breach_last_ms.get(kind)
        if last is not None and now_ms - last < cooldown_ms:
            return False
        self._breach_last_ms[kind] = now_ms
        return True

    def record_breach(self, kind: str, detail: str) -> None:
        """Append to the durable breach log, bounded.

        Unbounded growth here is a slow memory leak per session and, worse,
        an attacker-controlled one: a hostile client can emit breaches as
        fast as it can send frames.
        """
        if len(self.integrity_breaches) < MAX_RECORDED_BREACHES:
            self.integrity_breaches.append(f"{kind}: {detail}")
        elif len(self.integrity_breaches) == MAX_RECORDED_BREACHES:
            self.integrity_breaches.append(
                "... further breaches suppressed; see breach_counts for totals"
            )

    def check(self, seq: int, ts_monotonic_ms: int, server_ms: int) -> IntegrityResult:
        """Validate stream continuity for one freshly authenticated event.

        The clock checked here is the monotonic counter, not wall clock,
        because that is the one the fusion engine times rules against.

        There are two distinct attacks on that counter and they need two
        distinct checks:

        *Rewinding* it — jumping backwards so a violation window appears
        not to have elapsed. Caught absolutely, by the monotonicity check
        below: a counter that goes backwards is definitionally broken, so
        no tolerance applies and a rewind of any size is visible.

        *Stretching* it — advancing it slower than real time. Caught by
        cumulative drift against server elapsed time. Note the bound this
        implies: an undetected stretch can shave at most
        `skew_tolerance_ms` of total violation duration across a whole
        session. That is why tolerance must stay below the shortest onset
        in the policy, and why `validate_against_policy` exists.
        """
        # Replay is checked before the clock, and the order is not arbitrary.
        # A replayed frame trips both rules — its sequence number is stale
        # *and* its monotonic counter has gone backwards — so whichever runs
        # first decides the label. `stream_replay` is the precise diagnosis
        # and `stream_clock_skew` is a symptom of it; a reviewer reading the
        # breach log deserves the former.
        if seq <= self.last_seq:
            return IntegrityResult(
                ok=False,
                breach=IntegrityBreach.REPLAY,
                detail=f"received seq {seq} after {self.last_seq}",
            )

        if ts_monotonic_ms < self.last_monotonic_ms:
            return IntegrityResult(
                ok=False,
                breach=IntegrityBreach.CLOCK_SKEW,
                detail=(
                    f"monotonic counter went backwards: {ts_monotonic_ms} "
                    f"after {self.last_monotonic_ms}"
                ),
            )

        expected = self.last_seq + 1
        if seq != expected:
            missing = seq - expected
            # Resynchronise to the new position rather than holding the old
            # one. Holding it would make every subsequent frame look like a
            # gap too, so a single dropped event early on would silently
            # disable evaluation for the rest of the exam — one dropped
            # frame and the candidate is unproctored. The gap is recorded
            # and flagged; the stream keeps being watched.
            self.last_seq = seq
            self.last_monotonic_ms = ts_monotonic_ms
            self.events_received += 1
            return IntegrityResult(
                ok=False,
                breach=IntegrityBreach.SEQUENCE_GAP,
                detail=f"{missing} event(s) missing between seq {expected - 1} and {seq}",
            )

        if self.first_client_ms is None:
            self.first_client_ms = ts_monotonic_ms
            self.first_server_ms = server_ms
        else:
            client_elapsed = ts_monotonic_ms - self.first_client_ms
            server_elapsed = server_ms - (self.first_server_ms or server_ms)
            skew = abs(client_elapsed - server_elapsed)
            if skew > self.skew_tolerance_ms:
                # Advance anyway: we want the event recorded, flagged, and
                # the stream to keep running. Refusing it would just hand a
                # hostile client a way to sever its own telemetry cleanly.
                self.last_seq = seq
                self.last_monotonic_ms = ts_monotonic_ms
                self.events_received += 1
                return IntegrityResult(
                    ok=False,
                    breach=IntegrityBreach.CLOCK_SKEW,
                    detail=f"client clock drifted {skew}ms from server elapsed time",
                )

        self.last_seq = seq
        self.last_monotonic_ms = ts_monotonic_ms
        self.events_received += 1
        return IntegrityResult(ok=True)


def validate_against_policy(skew_tolerance_ms: int, min_onset_ms: int) -> list[str]:
    """Check that clock tolerance cannot swallow the shortest rule onset.

    Returns human-readable warnings rather than raising: an operator may
    knowingly accept the trade on a network bad enough to need the slack,
    and an exam platform refusing to boot over a tuning choice is worse
    than one that says loudly what it is exposed to.
    """
    warnings: list[str] = []
    if skew_tolerance_ms >= min_onset_ms:
        warnings.append(
            f"clock_skew_tolerance_ms={skew_tolerance_ms} is not below the shortest "
            f"rule onset ({min_onset_ms}ms). A client stretching its monotonic clock "
            f"could hide up to {skew_tolerance_ms}ms of violation without tripping "
            "stream_clock_skew. Lower the tolerance or raise the shortest onset."
        )
    return warnings


class SessionRegistry:
    """Session store, backed by an optional durable `Store`.

    Lookups stay in memory; writes are mirrored to the store so that
    sequence and clock state survive a restart. Redis or Postgres can
    replace the backing store without touching the request handlers.
    """

    def __init__(
        self,
        skew_tolerance_ms: int = 2_000,
        store: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self._skew_tolerance_ms = skew_tolerance_ms
        self._store = store
        self._clock = clock or now_ms

    def restore(self, records: Iterable[Any]) -> int:
        """Rebuild sessions from durable records. Returns how many loaded.

        Nothing is restored as connected: no client is attached immediately
        after a restart, and a session that claimed otherwise would show a
        proctor a candidate who is not there while suppressing the silence
        rule for a stream that does not exist.
        """
        count = 0
        for record in records:
            self._sessions[record.session_id] = Session(
                session_id=record.session_id,
                exam_id=record.exam_id,
                candidate_ref=record.candidate_ref,
                created_ms=record.created_ms,
                last_seq=record.last_seq,
                first_client_ms=record.first_client_ms,
                first_server_ms=record.first_server_ms,
                events_received=record.events_received,
                connected=False,
                ended_cleanly=record.ended_cleanly,
                attested_build=record.attested_build,
                integrity_breaches=list(record.integrity_breaches),
                skew_tolerance_ms=self._skew_tolerance_ms,
                last_monotonic_ms=record.last_monotonic_ms,
                breach_counts=dict(record.breach_counts),
                signal_counts=dict(record.signal_counts),
            )
            count += 1
        return count

    def persist(self, session: Session) -> None:
        """Mirror a session's current state to the durable store."""
        if self._store is None:
            return
        self._store.save_session(session.to_record(), self._clock())

    def create(self, exam_id: str, candidate_ref: str) -> Session:
        session = Session(
            session_id=new_session_id(),
            exam_id=exam_id,
            candidate_ref=candidate_ref,
            created_ms=self._clock(),
            skew_tolerance_ms=self._skew_tolerance_ms,
        )
        self._sessions[session.session_id] = session
        self.persist(session)
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def all(self) -> tuple[Session, ...]:
        return tuple(self._sessions.values())

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
