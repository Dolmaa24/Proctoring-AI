"""Declarative rule definitions for the fusion engine.

Rules are data, not code, so that exam policy can be reviewed, diffed and
tuned by people who are not going to open a Python file — and so that
changing a threshold does not require a backend deploy.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Severity(StrEnum):
    """How much weight a fired rule carries.

    INFO never reaches a proctor's queue; it exists so that we can record
    context (a brief look away, a single low-confidence blip) that makes a
    later HARD violation interpretable during review.
    """

    INFO = "info"
    SOFT = "soft"
    HARD = "hard"


class AutomatedAction(StrEnum):
    """What the platform is permitted to do on its own when a rule fires.

    Default is FLAG for every rule shipped in this repo. Nothing in an
    automated proctoring system should end someone's exam without a human
    in the loop: the models have measurable accuracy disparities across
    skin tones and lighting, and the behavioural heuristics penalise
    candidates with tics, ADHD, or a habit of reading aloud. LOCK_EXAM
    exists because customers ask for it, not because we recommend it.
    """

    FLAG = "flag"
    WARN_CANDIDATE = "warn_candidate"
    LOCK_EXAM = "lock_exam"


_OPS: Mapping[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "in": lambda a, b: a in b,
    "abs>": lambda a, b: abs(a) > b,
    "abs>=": lambda a, b: abs(a) >= b,
}


class Condition(BaseModel):
    """A predicate over a signal payload.

    Either a leaf comparison (`field`/`op`/`value`) or a boolean
    combination (`all_of`/`any_of`). Nesting is allowed but deliberately
    unexciting — if a rule needs more expressiveness than this, it wants a
    real detector rather than a cleverer config file.
    """

    model_config = ConfigDict(extra="forbid")

    field: str | None = None
    op: str | None = None
    value: Any = None
    all_of: list[Condition] | None = None
    any_of: list[Condition] | None = None
    negate: bool = False

    @model_validator(mode="after")
    def _exactly_one_form(self) -> Condition:
        leaf = self.field is not None
        branches = sum(x is not None for x in (self.all_of, self.any_of))
        if leaf and branches:
            raise ValueError("condition cannot be both a leaf and a combination")
        if not leaf and branches != 1:
            raise ValueError("condition must be a leaf or exactly one of all_of/any_of")
        if leaf:
            if self.op not in _OPS:
                raise ValueError(f"unknown operator {self.op!r}; expected one of {sorted(_OPS)}")
        return self

    def evaluate(self, payload: Any) -> bool:
        result = self._evaluate_inner(payload)
        return not result if self.negate else result

    def _evaluate_inner(self, payload: Any) -> bool:
        if self.all_of is not None:
            return all(c.evaluate(payload) for c in self.all_of)
        if self.any_of is not None:
            return any(c.evaluate(payload) for c in self.any_of)

        actual = getattr(payload, self.field, _MISSING)  # type: ignore[arg-type]
        if actual is _MISSING:
            # A rule pointed at a field this signal does not have. Treat as
            # "does not match" rather than raising: a misconfigured rule
            # must not be able to take the whole engine down mid-exam.
            return False

        expected = self.value
        if self.op in ("==", "!=") and _both_sequences(actual, expected):
            # YAML has no tuple literal, so `value: []` arrives as a list
            # while the signal field is a tuple — and in Python `() != []`
            # is True. Left alone, that made `blacklisted_processes != []`
            # match on every clean machine, flagging every honest candidate
            # for screen sharing. Compare sequences by content, not by
            # container type.
            actual, expected = list(actual), list(expected)

        try:
            return bool(_OPS[self.op](actual, expected))  # type: ignore[index]
        except TypeError:
            return False


def _both_sequences(a: Any, b: Any) -> bool:
    """True when both operands are non-string sequences.

    Strings are excluded deliberately: `in` over a string is a substring
    test and normalising it would change what existing rules mean.
    """
    return isinstance(a, list | tuple) and isinstance(b, list | tuple)


class _Missing:
    pass


_MISSING = _Missing()

Condition.model_rebuild()


class Rule(BaseModel):
    """One policy statement.

    The three durations are what make this humane rather than trigger-happy:

    `onset_ms`
        How long the condition must hold *continuously* before it counts.
        Humans blink, stretch, glance at a noise, and rub their eyes. Even
        HARD rules get a nonzero onset so that a single false-positive
        frame from the object detector cannot flag anyone.

    `release_ms`
        Hysteresis. Once a condition is building or firing, it must be
        false for this long before the timer resets. Without it, one
        dropped detection in the middle of a genuine 10-second phone use
        restarts the clock and the violation never fires.

    `cooldown_ms`
        Minimum gap between repeat firings, so a candidate who is looking
        away for two minutes generates one reviewable event rather than
        forty-eight of them.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    signal: str
    when: Condition
    severity: Severity
    onset_ms: int = Field(ge=0)
    release_ms: int = Field(default=750, ge=0)
    cooldown_ms: int = Field(default=15_000, ge=0)
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    action: AutomatedAction = AutomatedAction.FLAG
    requires_human_review: bool = True

    @model_validator(mode="after")
    def _guard_instant_hard_rules(self) -> Rule:
        if self.severity is Severity.HARD and self.onset_ms == 0:
            raise ValueError(
                f"rule {self.id!r}: a HARD rule with onset_ms=0 fires on a single frame, "
                "which makes it a detector false-positive amplifier. Use at least one "
                "confirmation window (~500ms)."
            )
        if self.action is AutomatedAction.LOCK_EXAM and not self.requires_human_review:
            raise ValueError(
                f"rule {self.id!r}: locking an exam without flagging it for human review "
                "is not a supported configuration."
            )
        return self


class LivenessRule(BaseModel):
    """Detects the *absence* of telemetry rather than its content.

    A hostile client's cheapest attack is to stop sending events during the
    interesting part of the exam. This rule is evaluated on a wall-clock
    tick rather than on receipt of an event, so silence trips it.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = "stream_silent"
    description: str = "Telemetry stream went quiet; client may have been suppressed."
    max_gap_ms: int = Field(default=5_000, gt=0)
    severity: Severity = Severity.HARD
    cooldown_ms: int = Field(default=30_000, ge=0)
    action: AutomatedAction = AutomatedAction.FLAG
    requires_human_review: bool = True


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    rules: list[Rule]
    liveness: LivenessRule = Field(default_factory=LivenessRule)

    @model_validator(mode="after")
    def _unique_ids(self) -> Policy:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"duplicate rule id {rule.id!r}")
            seen.add(rule.id)
        if self.liveness.id in seen:
            raise ValueError(f"liveness rule id {self.liveness.id!r} collides with a rule")
        return self
