"""Turning a stream of single-probe comparisons into a reviewable finding.

The same discipline the fusion engine applies to gaze applies here, and
more so: a single low similarity is not a person swap. It is far more
often a shadow, a head turn caught mid-motion, glasses going on, or the
candidate leaning back. Accusing someone of having a stand-in take their
exam is the most serious claim this system can make, so it takes sustained
evidence rather than one frame.

Note what *does* escalate quickly: a run of probes that cannot be assessed
at all. That is not a mismatch and is never reported as one, but a
candidate whose camera has stopped producing usable images for ten minutes
is a session a human should look at, and the reason to look is "we cannot
see" rather than "it is someone else".
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from .matching import (
    Embedding,
    Enrolment,
    MatchOutcome,
    MatchPolicy,
    MatchResult,
    ProbeQuality,
    compare,
)


class IdentityStatus(StrEnum):
    UNVERIFIED = "unverified"
    """No enrolment yet, or no assessable probe since."""

    CONFIRMED = "confirmed"
    MISMATCH_SUSPECTED = "mismatch_suspected"
    UNOBSERVABLE = "unobservable"
    """Probes keep arriving but none can be assessed."""


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    """How much disagreement counts.

    `mismatches_required` of the last `window` assessable probes must fall
    below threshold before anything is reported. Both defaults are
    deliberately unhurried — at one probe every 30 seconds, three of five
    is roughly two and a half minutes of sustained disagreement.
    """

    window: int = 5
    mismatches_required: int = 3
    unobservable_run: int = 8
    """Consecutive unassessable probes before the session is flagged as
    unobservable. Not a misconduct finding; a "we cannot see this candidate"
    finding."""

    def __post_init__(self) -> None:
        if self.mismatches_required > self.window:
            raise ValueError("mismatches_required cannot exceed window")
        if self.mismatches_required < 2:
            raise ValueError("a single probe must never be sufficient to allege impersonation")


@dataclass(frozen=True, slots=True)
class IdentityFinding:
    status: IdentityStatus
    message: str
    similarities: tuple[float | None, ...]
    threshold: float
    calibrated_on: str
    assessed: int
    mismatched: int

    def as_dict(self) -> dict[str, object]:
        return {
            "status": str(self.status),
            "message": self.message,
            "similarities": list(self.similarities),
            "threshold": self.threshold,
            "calibrated_on": self.calibrated_on,
            "assessed": self.assessed,
            "mismatched": self.mismatched,
        }


@dataclass
class SessionIdentity:
    """Per-session identity state."""

    enrolment: Enrolment | None = None
    recent: deque[MatchResult] = field(default_factory=lambda: deque(maxlen=32))
    unassessable_run: int = 0
    status: IdentityStatus = IdentityStatus.UNVERIFIED
    probes: int = 0

    def enrolled(self) -> bool:
        return self.enrolment is not None


class IdentityVerifier:
    """Holds enrolments and decides when disagreement is worth reporting."""

    def __init__(
        self,
        match_policy: MatchPolicy,
        verification_policy: VerificationPolicy | None = None,
    ) -> None:
        self.match_policy = match_policy
        self.verification_policy = verification_policy or VerificationPolicy()
        self._sessions: dict[str, SessionIdentity] = {}

    def state(self, session_id: str) -> SessionIdentity:
        return self._sessions.setdefault(session_id, SessionIdentity())

    def enrol(self, session_id: str, enrolment: Enrolment) -> None:
        state = self.state(session_id)
        state.enrolment = enrolment
        state.status = IdentityStatus.UNVERIFIED

    def forget(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def probe(
        self,
        session_id: str,
        embedding: Embedding | None,
        quality: ProbeQuality,
    ) -> tuple[MatchResult, IdentityFinding | None]:
        """Record one probe. Returns the comparison and any new finding.

        A finding is returned only when the *status changes*, so a session
        that is confidently mismatched does not re-report on every probe
        and bury the console — the same reasoning as the cooldowns in the
        fusion engine.
        """
        state = self.state(session_id)
        if state.enrolment is None:
            raise LookupError(f"session {session_id!r} has no enrolment")

        result = compare(embedding, state.enrolment, quality, self.match_policy)
        state.recent.append(result)
        state.probes += 1

        if result.outcome is MatchOutcome.NOT_ASSESSABLE:
            state.unassessable_run += 1
        else:
            state.unassessable_run = 0

        new_status = self._evaluate(state)
        if new_status == state.status:
            return result, None

        state.status = new_status
        return result, self._finding(state, new_status)

    def _evaluate(self, state: SessionIdentity) -> IdentityStatus:
        policy = self.verification_policy

        if state.unassessable_run >= policy.unobservable_run:
            return IdentityStatus.UNOBSERVABLE

        assessable = [r for r in state.recent if r.outcome is not MatchOutcome.NOT_ASSESSABLE][
            -policy.window :
        ]
        if not assessable:
            return IdentityStatus.UNVERIFIED

        mismatches = sum(1 for r in assessable if r.outcome is MatchOutcome.MISMATCH)
        if mismatches >= policy.mismatches_required:
            return IdentityStatus.MISMATCH_SUSPECTED

        # Requiring a full clean window before confirming avoids flipping to
        # CONFIRMED on the strength of one good frame after a bad run.
        if len(assessable) >= policy.window and mismatches == 0:
            return IdentityStatus.CONFIRMED

        # Not enough evidence either way: hold whatever we last concluded
        # rather than oscillating on partial windows.
        return state.status

    def _finding(self, state: SessionIdentity, status: IdentityStatus) -> IdentityFinding:
        assessable = [r for r in state.recent if r.outcome is not MatchOutcome.NOT_ASSESSABLE][
            -self.verification_policy.window :
        ]
        mismatched = sum(1 for r in assessable if r.outcome is MatchOutcome.MISMATCH)

        messages = {
            IdentityStatus.CONFIRMED: "Identity consistent with enrolment.",
            IdentityStatus.MISMATCH_SUSPECTED: (
                "Face does not match the enrolment reference across multiple "
                "checks. Compare the enrolment and session recordings before "
                "drawing any conclusion — this measurement has known accuracy "
                "differences across demographic groups."
            ),
            IdentityStatus.UNOBSERVABLE: (
                "Identity could not be checked: no usable capture for a "
                "sustained period. This is a visibility problem, not a "
                "mismatch."
            ),
            IdentityStatus.UNVERIFIED: "Identity not yet established.",
        }

        return IdentityFinding(
            status=status,
            message=messages[status],
            similarities=tuple(r.similarity for r in assessable),
            threshold=self.match_policy.threshold.value,
            calibrated_on=self.match_policy.threshold.calibrated_on,
            assessed=len(assessable),
            mismatched=mismatched,
        )
