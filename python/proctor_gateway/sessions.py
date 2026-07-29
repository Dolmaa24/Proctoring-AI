"""Session registry and stream-integrity checking.

The integrity checks here are the part of the system that a hostile client
actually has to get past. Signature verification proves a frame came from a
provisioned client; these checks prove the client did not *withhold* frames,
which is the far more likely attack.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum

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
        if ts_monotonic_ms < self.last_monotonic_ms:
            return IntegrityResult(
                ok=False,
                breach=IntegrityBreach.CLOCK_SKEW,
                detail=(
                    f"monotonic counter went backwards: {ts_monotonic_ms} "
                    f"after {self.last_monotonic_ms}"
                ),
            )

        if seq <= self.last_seq:
            return IntegrityResult(
                ok=False,
                breach=IntegrityBreach.REPLAY,
                detail=f"received seq {seq} after {self.last_seq}",
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
    """In-memory session store.

    Deliberately behind a narrow interface. Everything here is a dict lookup
    today; swapping in Redis for horizontal scale should not require touching
    the gateway's request handlers.
    """

    def __init__(self, skew_tolerance_ms: int = 2_000) -> None:
        self._sessions: dict[str, Session] = {}
        self._skew_tolerance_ms = skew_tolerance_ms

    def create(self, exam_id: str, candidate_ref: str) -> Session:
        session = Session(
            session_id=new_session_id(),
            exam_id=exam_id,
            candidate_ref=candidate_ref,
            created_ms=now_ms(),
            skew_tolerance_ms=self._skew_tolerance_ms,
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def all(self) -> tuple[Session, ...]:
        return tuple(self._sessions.values())

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
