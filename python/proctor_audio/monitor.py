"""Turning a stream of per-chunk intent classifications into a reviewable finding.

Same discipline as `proctor_identity.verifier` and the fusion engine's
onset/release windows: one classified chunk is never sustained evidence.
A candidate reads a formula aloud, mutters a wrong guess, or says "one
sec" to someone walking through the room — a single SEEKING_HELP
classification is weak evidence on its own, and the classifier is
fallible in both directions.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from .intent import IntentClassification, IntentLabel


class AudioStatus(StrEnum):
    QUIET = "quiet"
    SUSTAINED_HELP_SUSPECTED = "sustained_help_suspected"


@dataclass(frozen=True, slots=True)
class AudioMonitorPolicy:
    """`seeking_help_required` of the last `window` classified chunks must
    read SEEKING_HELP before anything is reported."""

    window: int = 5
    seeking_help_required: int = 3

    def __post_init__(self) -> None:
        if self.seeking_help_required > self.window:
            raise ValueError("seeking_help_required cannot exceed window")
        if self.seeking_help_required < 2:
            raise ValueError(
                "a single classified chunk must never be sufficient to allege seeking outside help"
            )


@dataclass(frozen=True, slots=True)
class AudioFinding:
    status: AudioStatus
    message: str
    recent_labels: tuple[str, ...]
    matched: int
    transcript_excerpt: str
    """For the *live* console view only — never persisted verbatim into the
    long-retained violation record. See proctor_gateway.app for the split:
    the transcript itself lives on the same short retention clock as an
    identity template, referenced from the violation rather than embedded
    in it."""

    def as_dict(self) -> dict[str, object]:
        return {
            "status": str(self.status),
            "message": self.message,
            "recent_labels": list(self.recent_labels),
            "matched": self.matched,
            "transcript_excerpt": self.transcript_excerpt,
        }


@dataclass
class SessionAudio:
    recent: deque[IntentClassification] = field(default_factory=lambda: deque(maxlen=32))
    status: AudioStatus = AudioStatus.QUIET


class AudioIntentMonitor:
    """Holds the rolling window of recent classifications per session.

    Deliberately not persisted across a restart, for the same reason the
    fusion engine's onset timers are not: a candidate whose window resets
    gets a fresh count rather than a stale one, which errs toward not
    flagging, and a candidate cannot trigger a gateway restart to exploit
    it.
    """

    def __init__(self, policy: AudioMonitorPolicy | None = None) -> None:
        self.policy = policy or AudioMonitorPolicy()
        self._sessions: dict[str, SessionAudio] = {}

    def state(self, session_id: str) -> SessionAudio:
        return self._sessions.setdefault(session_id, SessionAudio())

    def forget(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def record(
        self, session_id: str, classification: IntentClassification, transcript: str
    ) -> AudioFinding | None:
        """Record one classified chunk. Returns a finding only on escalation.

        De-escalation back to QUIET is not itself reported: it is not
        actionable, and the timeline already shows when the last finding
        fired for a reviewer to locate the surrounding chunks.
        """
        state = self.state(session_id)
        state.recent.append(classification)

        window = list(state.recent)[-self.policy.window :]
        matched = sum(1 for c in window if c.label is IntentLabel.SEEKING_HELP)

        new_status = (
            AudioStatus.SUSTAINED_HELP_SUSPECTED
            if matched >= self.policy.seeking_help_required
            else AudioStatus.QUIET
        )

        if new_status == state.status:
            return None
        state.status = new_status

        if new_status is AudioStatus.QUIET:
            return None

        return AudioFinding(
            status=new_status,
            message=(
                "Speech content repeatedly classified as seeking outside help. "
                "Read the transcript before concluding anything: intent "
                "classification is probabilistic and can misread a rhetorical "
                "question, sarcasm, or a phrase out of context."
            ),
            recent_labels=tuple(str(c.label) for c in window),
            matched=matched,
            transcript_excerpt=transcript[:280],
        )
