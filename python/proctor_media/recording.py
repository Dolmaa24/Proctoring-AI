"""Recording lifecycle: a plain state machine, deliberately not a classifier.

Every other subsystem added to this project — the fusion engine's onset
windows, identity's sustained-mismatch requirement, audio's sustained
seeking-help requirement — exists to stop a probabilistic inference about
a candidate's behaviour from firing on one noisy sample. Recording start
and stop have no such mechanism here, and that is not an oversight: a
recording is not an inference about the candidate at all. It starts
because a proctor clicked "record" or a policy said to, and it stops the
same way. There is nothing to be falsely confident about in the way a
face match or an intent classification can be, so there is nothing here
for a temporal-discipline module to protect against.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RecordingStatus(StrEnum):
    REQUESTED = "requested"
    ACTIVE = "active"
    STOPPING = "stopping"
    AVAILABLE = "available"
    FAILED = "failed"


_TRANSITIONS: dict[RecordingStatus, set[RecordingStatus]] = {
    # REQUESTED reaches every later state directly, not only ACTIVE, for
    # the same reason ACTIVE reaches AVAILABLE without passing through
    # STOPPING below: LiveKit gives no ordering guarantee across webhook
    # deliveries, and a proctor's local "stop" click is not a webhook at
    # all. A proctor can ask to stop a recording before its `egress_started`
    # confirmation has arrived; a short recording can finish and deliver
    # `egress_ended` before `egress_started` is processed. Treating ACTIVE
    # as a mandatory waypoint would make both of those ordinary cases an
    # InvalidTransition instead of the routine race they are.
    RecordingStatus.REQUESTED: {
        RecordingStatus.ACTIVE,
        RecordingStatus.STOPPING,
        RecordingStatus.AVAILABLE,
        RecordingStatus.FAILED,
    },
    # ACTIVE reaches AVAILABLE/FAILED directly, not only via STOPPING.
    # STOPPING represents *us* having asked the SFU to stop (set locally
    # when a proctor clicks "stop"), but a recording can also end on its
    # own — the room closes because the candidate disconnected, say — and
    # the completion webhook arrives with no local STOPPING step ever
    # having happened. Requiring STOPPING as a mandatory waypoint would
    # make that ordinary case an InvalidTransition.
    RecordingStatus.ACTIVE: {
        RecordingStatus.STOPPING,
        RecordingStatus.AVAILABLE,
        RecordingStatus.FAILED,
    },
    RecordingStatus.STOPPING: {RecordingStatus.AVAILABLE, RecordingStatus.FAILED},
    RecordingStatus.AVAILABLE: set(),
    RecordingStatus.FAILED: set(),
}


class InvalidTransition(Exception):
    """A recording tried to move to a status its current one cannot reach.

    Raised rather than silently clamped or overwritten: an out-of-order
    webhook delivery (LiveKit does not guarantee ordering across retries)
    corrupting a recording's recorded status is exactly the kind of bug
    that is invisible until someone needs the recording and it is not
    there — better to fail loudly at the point of the bad transition.
    """


@dataclass(slots=True)
class RecordingRecord:
    recording_id: str
    session_id: str
    status: RecordingStatus
    requested_ms: int
    started_ms: int | None = None
    stopped_ms: int | None = None
    egress_id: str | None = None
    storage_ref: str | None = None
    """Where the recording ended up (e.g. an S3 URI), as reported by the
    egress service. This is a reference, never bytes — the same principle
    as an identity template or an audio transcript, applied to the single
    most sensitive artifact this platform touches. Retrieving the actual
    recording is the operator's storage layer's job (a presigned URL,
    typically); this codebase tracks where it is, not what is in it.
    """
    failure_reason: str | None = None

    def transition(self, new_status: RecordingStatus, now_ms: int) -> None:
        if new_status not in _TRANSITIONS[self.status]:
            raise InvalidTransition(
                f"recording {self.recording_id}: cannot move from {self.status} to {new_status}"
            )
        self.status = new_status
        if new_status is RecordingStatus.ACTIVE:
            self.started_ms = now_ms
        elif new_status is RecordingStatus.AVAILABLE:
            self.stopped_ms = now_ms

    def as_dict(self) -> dict[str, object]:
        return {
            "recording_id": self.recording_id,
            "session_id": self.session_id,
            "status": str(self.status),
            "requested_ms": self.requested_ms,
            "started_ms": self.started_ms,
            "stopped_ms": self.stopped_ms,
            "egress_id": self.egress_id,
            "storage_ref": self.storage_ref,
            "failure_reason": self.failure_reason,
        }
