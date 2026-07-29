"""The fusion engine: turns a stream of edge observations into reviewable events.

Pure logic, no I/O, no clock of its own — every entry point takes the
current time as an argument. That makes the whole of exam policy testable
in microseconds instead of requiring a candidate, a webcam and patience.

Which clock times a rule
------------------------
Two clocks are in play and they do different jobs.

Rule durations are measured with the client's *monotonic* counter
(`ts_monotonic_ms`, milliseconds since session start). This is the only
clock that accurately reflects how long something actually lasted in front
of the camera. Server receipt time cannot do this job: a candidate on poor
wifi whose client buffers three seconds of telemetry and then bursts it
would have every sustained violation compressed into a sub-threshold blip,
and "have bad internet" is not an acceptable way to defeat proctoring.

Absence rules are measured with *server* time, because a client cannot be
asked to self-report that it stopped talking.

The obvious objection — that the monotonic counter is reported by a machine
the candidate controls — is handled one layer down, in the gateway: every
frame's monotonic counter is checked against server elapsed time, and
divergence beyond tolerance raises `stream_clock_skew` and flags the
session. Stretching the counter to shrink a violation therefore trades a
soft flag for a hard one, which is a bad trade for the candidate. Neither
clock is trustworthy alone; together they are.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from proctor_protocol import Envelope

from .rules import AutomatedAction, LivenessRule, Policy, Rule, Severity

EVIDENCE_WINDOW = 64
"""Samples retained per session for attaching to a fired violation."""


@dataclass(frozen=True, slots=True)
class EvidenceSample:
    server_ts_ms: int
    client_ts_ms: int
    seq: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Violation:
    """A reviewable event. Note what this type does *not* have: a verdict.

    Nothing here says the candidate cheated. It says a policy condition held
    for a measured duration, and carries the observations that led there so
    a human can decide.
    """

    violation_id: str
    session_id: str
    rule_id: str
    severity: Severity
    action: AutomatedAction
    requires_human_review: bool
    message: str
    opened_at_ms: int
    fired_at_ms: int
    duration_ms: int
    evidence: tuple[EvidenceSample, ...]
    resolved: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "violation_id": self.violation_id,
            "session_id": self.session_id,
            "rule_id": self.rule_id,
            "severity": str(self.severity),
            "action": str(self.action),
            "requires_human_review": self.requires_human_review,
            "message": self.message,
            "opened_at_ms": self.opened_at_ms,
            "fired_at_ms": self.fired_at_ms,
            "duration_ms": self.duration_ms,
            "resolved": self.resolved,
            "evidence": [
                {
                    "server_ts_ms": s.server_ts_ms,
                    "client_ts_ms": s.client_ts_ms,
                    "seq": s.seq,
                    "payload": s.payload,
                }
                for s in self.evidence
            ],
        }


@dataclass
class _RuleState:
    """State machine for one (session, rule) pair."""

    candidate_since_ms: int | None = None
    releasing_since_ms: int | None = None
    firing: bool = False
    last_fired_ms: int | None = None
    active_violation_id: str | None = None

    def reset(self) -> None:
        self.candidate_since_ms = None
        self.releasing_since_ms = None
        self.firing = False
        self.active_violation_id = None


@dataclass
class _SessionState:
    session_id: str
    opened_at_ms: int
    last_event_ms: int
    rules: dict[str, _RuleState] = field(default_factory=dict)
    evidence: deque[EvidenceSample] = field(default_factory=lambda: deque(maxlen=EVIDENCE_WINDOW))
    liveness_last_fired_ms: int | None = None
    closed: bool = False
    connected: bool = True


class FusionEngine:
    """Evaluates a `Policy` against per-session telemetry streams."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self._rules_by_signal: dict[str, list[Rule]] = {}
        for rule in policy.rules:
            self._rules_by_signal.setdefault(rule.signal, []).append(rule)
        self._sessions: dict[str, _SessionState] = {}

    # -- session lifecycle -------------------------------------------------

    def open_session(self, session_id: str, now_ms: int) -> None:
        self._sessions[session_id] = _SessionState(
            session_id=session_id, opened_at_ms=now_ms, last_event_ms=now_ms
        )

    def close_session(self, session_id: str) -> None:
        state = self._sessions.get(session_id)
        if state is not None:
            state.closed = True

    def set_connected(self, session_id: str, connected: bool) -> None:
        """Track whether the telemetry socket is currently attached.

        The silence rule targets a client that is still connected but has
        stopped reporting — the actual suppression attack. A client that
        has *disconnected* is a different event with its own signal
        (`stream_disconnected`, plus `stream_abandoned` if the exam never
        formally ended), reported once.

        Without this distinction every finished exam emits silence
        violations forever and its state is never released, which is both
        console noise and an unbounded leak.
        """
        state = self._sessions.get(session_id)
        if state is not None:
            state.connected = connected
            if connected:
                state.liveness_last_fired_ms = None

    def forget_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    @property
    def active_sessions(self) -> tuple[str, ...]:
        return tuple(s for s, st in self._sessions.items() if not st.closed)

    # -- evaluation --------------------------------------------------------

    def on_event(self, envelope: Envelope, now_ms: int) -> list[Violation]:
        """Feed one authenticated telemetry event.

        `now_ms` is server receipt time and is used for liveness tracking
        and evidence. Rule durations are timed on the envelope's monotonic
        counter — see the module docstring for why.
        """
        state = self._sessions.get(envelope.session_id)
        if state is None:
            raise KeyError(f"session {envelope.session_id!r} is not open")
        if state.closed:
            return []

        state.last_event_ms = now_ms
        payload = envelope.payload
        sample = EvidenceSample(
            server_ts_ms=now_ms,
            client_ts_ms=envelope.ts_client_ms,
            seq=envelope.seq,
            payload=payload.model_dump(mode="json"),
        )
        state.evidence.append(sample)

        policy_ms = envelope.ts_monotonic_ms
        raised: list[Violation] = []
        for rule in self._rules_by_signal.get(payload.type, ()):
            violation = self._step_rule(state, rule, payload, policy_ms)
            if violation is not None:
                raised.append(violation)
        return raised

    def on_tick(self, now_ms: int) -> list[Violation]:
        """Wall-clock evaluation for conditions that absence of data implies."""
        raised: list[Violation] = []
        liveness = self.policy.liveness
        for state in self._sessions.values():
            if state.closed or not state.connected:
                continue
            gap = now_ms - state.last_event_ms
            if gap < liveness.max_gap_ms:
                continue
            if (
                state.liveness_last_fired_ms is not None
                and now_ms - state.liveness_last_fired_ms < liveness.cooldown_ms
            ):
                continue
            state.liveness_last_fired_ms = now_ms
            raised.append(self._liveness_violation(state, liveness, gap, now_ms))
        return raised

    def report_integrity_breach(
        self, session_id: str, rule_id: str, message: str, now_ms: int
    ) -> Violation:
        """Raise a violation for a transport-level problem the gateway found.

        Sequence gaps, replayed frames and attestation mismatches are
        detected during authentication, before the payload is meaningful,
        but they belong in the same reviewable stream as everything else.
        """
        state = self._sessions.get(session_id)
        evidence = tuple(state.evidence)[-8:] if state else ()
        return Violation(
            violation_id=str(uuid.uuid4()),
            session_id=session_id,
            rule_id=rule_id,
            severity=Severity.HARD,
            action=AutomatedAction.FLAG,
            requires_human_review=True,
            message=message,
            opened_at_ms=now_ms,
            fired_at_ms=now_ms,
            duration_ms=0,
            evidence=evidence,
        )

    # -- internals ---------------------------------------------------------

    def _step_rule(
        self, state: _SessionState, rule: Rule, payload: Any, now_ms: int
    ) -> Violation | None:
        rule_state = state.rules.setdefault(rule.id, _RuleState())

        confidence = getattr(payload, "confidence", 1.0)
        if confidence < rule.min_confidence:
            # No information. Deliberately neither advances the onset timer
            # nor starts the release timer: a detector that briefly loses
            # confidence (glasses glare, a hand across the face) must not
            # be able to either manufacture or cancel a violation.
            return None

        if rule.when.evaluate(payload):
            return self._on_match(state, rule, rule_state, now_ms)
        return self._on_no_match(state, rule, rule_state, now_ms)

    def _on_match(
        self, state: _SessionState, rule: Rule, rs: _RuleState, now_ms: int
    ) -> Violation | None:
        rs.releasing_since_ms = None
        if rs.candidate_since_ms is None:
            rs.candidate_since_ms = now_ms

        if rs.firing:
            return None
        if now_ms - rs.candidate_since_ms < rule.onset_ms:
            return None
        if rs.last_fired_ms is not None and now_ms - rs.last_fired_ms < rule.cooldown_ms:
            return None

        rs.firing = True
        rs.last_fired_ms = now_ms
        rs.active_violation_id = str(uuid.uuid4())
        return Violation(
            violation_id=rs.active_violation_id,
            session_id=state.session_id,
            rule_id=rule.id,
            severity=rule.severity,
            action=rule.action,
            requires_human_review=rule.requires_human_review,
            message=rule.description,
            opened_at_ms=rs.candidate_since_ms,
            fired_at_ms=now_ms,
            duration_ms=now_ms - rs.candidate_since_ms,
            evidence=tuple(state.evidence),
        )

    def _on_no_match(
        self, state: _SessionState, rule: Rule, rs: _RuleState, now_ms: int
    ) -> Violation | None:
        if rs.candidate_since_ms is None:
            return None

        if rs.releasing_since_ms is None:
            rs.releasing_since_ms = now_ms
            return None

        if now_ms - rs.releasing_since_ms < rule.release_ms:
            return None

        was_firing = rs.firing
        violation_id = rs.active_violation_id
        opened = rs.candidate_since_ms
        released_at = rs.releasing_since_ms
        rs.reset()

        if not was_firing or violation_id is None:
            return None

        return Violation(
            violation_id=violation_id,
            session_id=state.session_id,
            rule_id=rule.id,
            severity=rule.severity,
            action=AutomatedAction.FLAG,
            requires_human_review=rule.requires_human_review,
            message=f"{rule.description} (resolved)",
            opened_at_ms=opened,
            fired_at_ms=released_at,
            duration_ms=released_at - opened,
            evidence=(),
            resolved=True,
        )

    def _liveness_violation(
        self, state: _SessionState, liveness: LivenessRule, gap_ms: int, now_ms: int
    ) -> Violation:
        return Violation(
            violation_id=str(uuid.uuid4()),
            session_id=state.session_id,
            rule_id=liveness.id,
            severity=liveness.severity,
            action=liveness.action,
            requires_human_review=liveness.requires_human_review,
            message=f"{liveness.description} No telemetry for {gap_ms}ms.",
            opened_at_ms=state.last_event_ms,
            fired_at_ms=now_ms,
            duration_ms=gap_ms,
            evidence=tuple(state.evidence)[-8:],
        )
