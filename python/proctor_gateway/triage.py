"""Per-session triage state for the proctor console.

A proctor watching fifty candidates cannot watch fifty video feeds. This
turns the raw violation stream into something a human can act on: one row
per session, ordered so the sessions most worth a look sort first.

Why the score lives here and not in the browser
-----------------------------------------------
Two proctors looking at the same exam must see the same ordering, the
ordering must survive a page refresh, and — most importantly — the logic
that decides whose name floats to the top of an invigilator's screen is
consequential enough to belong somewhere it can be unit tested and
reviewed. A decay function buried in dashboard JavaScript is none of those
things.

What the score is not
---------------------
It is **not** a probability of cheating, and the console must never render
it as a percentage. It is a recency-weighted count of flags, and its only
job is ordering a work queue. A number between 0 and 100 shown next to a
person's name will be read as a confidence value by a tired human under
time pressure, no matter what the label says — so `band()` exists, and the
console shows the band rather than the number.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from proctor_fusion import Severity

SCORE_HALF_LIFE_MS = 180_000
"""How long until a flag counts half as much toward ordering.

Three minutes. Long enough that a burst of related flags keeps a candidate
near the top while a proctor gets to them; short enough that someone who
had one bad moment twenty minutes ago is not still pinned there.
"""

SEVERITY_WEIGHT: dict[Severity, float] = {
    Severity.HARD: 10.0,
    Severity.SOFT: 3.0,
    Severity.INFO: 0.0,
}
"""INFO contributes nothing to ordering.

Capture-quality notes are context for review, not evidence of anything,
and a candidate with a bad webcam must not drift up the queue for it.
"""

SCORE_CEILING = 100.0
TIMELINE_LIMIT = 200


class Band(StrEnum):
    """Coarse triage bucket. This, not the raw score, is what the UI shows."""

    QUIET = "quiet"
    NOTICE = "notice"
    REVIEW = "review"


BAND_THRESHOLDS = ((Band.REVIEW, 12.0), (Band.NOTICE, 3.0))


@dataclass(slots=True)
class TimelineEntry:
    at_ms: int
    rule_id: str
    severity: str
    message: str
    duration_ms: int
    violation_id: str
    resolved: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "at_ms": self.at_ms,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "message": self.message,
            "duration_ms": self.duration_ms,
            "violation_id": self.violation_id,
            "resolved": self.resolved,
        }


@dataclass(slots=True)
class SessionTriage:
    session_id: str
    exam_id: str = ""
    candidate_ref: str = ""
    started_ms: int = 0
    connected: bool = False
    ended_cleanly: bool = False
    events_received: int = 0
    _score: float = 0.0
    _score_at_ms: int = 0
    open_violations: dict[str, TimelineEntry] = field(default_factory=dict)
    timeline: deque[TimelineEntry] = field(default_factory=lambda: deque(maxlen=TIMELINE_LIMIT))

    def score(self, now_ms: int) -> float:
        """Current score with exponential decay applied.

        Decay is computed on read rather than on a timer, so a session with
        no traffic still cools down correctly and there is no background
        job to keep sessions warm.
        """
        if self._score <= 0:
            return 0.0
        elapsed = max(0, now_ms - self._score_at_ms)
        decayed = self._score * math.pow(0.5, elapsed / SCORE_HALF_LIFE_MS)
        return decayed if decayed > 0.01 else 0.0

    def band(self, now_ms: int) -> Band:
        current = self.score(now_ms)
        for band, threshold in BAND_THRESHOLDS:
            if current >= threshold:
                return band
        return Band.QUIET

    def add_violation(self, violation: dict[str, Any], now_ms: int) -> None:
        entry = TimelineEntry(
            at_ms=now_ms,
            rule_id=violation["rule_id"],
            severity=violation["severity"],
            message=violation["message"],
            duration_ms=violation.get("duration_ms", 0),
            violation_id=violation["violation_id"],
            resolved=bool(violation.get("resolved")),
        )

        if entry.resolved:
            # A resolution closes the open flag but must not erase it from
            # the timeline: "looked away for 30s, then stopped" is exactly
            # what a reviewer needs to see.
            self.open_violations.pop(entry.violation_id, None)
            self.timeline.append(entry)
            return

        self.open_violations[entry.violation_id] = entry
        self.timeline.append(entry)

        severity = _severity_of(entry.severity)
        weight = SEVERITY_WEIGHT.get(severity, 0.0)
        if weight:
            self._score = min(SCORE_CEILING, self.score(now_ms) + weight)
            self._score_at_ms = now_ms

    def as_dict(self, now_ms: int) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "exam_id": self.exam_id,
            "candidate_ref": self.candidate_ref,
            "started_ms": self.started_ms,
            "connected": self.connected,
            "ended_cleanly": self.ended_cleanly,
            "events_received": self.events_received,
            "band": str(self.band(now_ms)),
            # Sent for ordering and debugging. The console sorts on it and
            # renders the band; see the module docstring.
            "score": round(self.score(now_ms), 2),
            "open_violations": [v.as_dict() for v in self.open_violations.values()],
            "timeline": [entry.as_dict() for entry in reversed(self.timeline)],
        }


def _severity_of(raw: str) -> Severity | None:
    try:
        return Severity(raw)
    except ValueError:
        return None


class TriageBoard:
    """All sessions a proctor is responsible for."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionTriage] = {}

    def ensure(self, session_id: str, now_ms: int, **fields: Any) -> SessionTriage:
        session = self._sessions.get(session_id)
        if session is None:
            session = SessionTriage(session_id=session_id, started_ms=now_ms)
            self._sessions[session_id] = session
        for key, value in fields.items():
            if value is not None and hasattr(session, key):
                setattr(session, key, value)
        return session

    def get(self, session_id: str) -> SessionTriage | None:
        return self._sessions.get(session_id)

    def record_violation(self, violation: dict[str, Any], now_ms: int) -> SessionTriage:
        session = self.ensure(violation["session_id"], now_ms)
        session.add_violation(violation, now_ms)
        return session

    def ordered(self, now_ms: int) -> list[SessionTriage]:
        """Sessions in the order a proctor should work through them.

        Disconnected sessions sort above quiet connected ones regardless of
        score: a client that vanished is unattended, and unattended is the
        state most likely to need a human.
        """
        return sorted(
            self._sessions.values(),
            key=lambda s: (
                -s.score(now_ms),
                s.connected,  # False (0) first
                s.session_id,
            ),
        )

    def snapshot(self, now_ms: int) -> list[dict[str, Any]]:
        return [session.as_dict(now_ms) for session in self.ordered(now_ms)]

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
