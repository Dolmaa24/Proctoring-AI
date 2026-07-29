"""Face template comparison: normalisation, similarity, quality gating.

Pure functions and small dataclasses, no model and no I/O, so the part
that decides whether a candidate "is who they said they were" can be
tested exhaustively rather than eyeballed against a webcam.

Read this before tuning anything
--------------------------------
Face recognition has the largest documented demographic accuracy gap of
any component in this system. NIST's FRVT 1:1 evaluations found false
match rates varying by one to two orders of magnitude across demographic
groups — with the highest false-positive rates for West and East African
and East Asian faces, for women, and at the extremes of age. A cosine
threshold calibrated on one population is simply a different operating
point on another.

Three consequences are built into this module rather than written in a
policy document nobody reads:

1. `MatchPolicy` has **no default threshold**. It must be supplied, and
   `Threshold.calibrated_on` must record what population it was measured
   against. A threshold with no provenance is a guess wearing a number.
2. Every outcome carries the raw similarity and the threshold applied, so
   a reviewer sees `0.41 against 0.55 (calibrated: vendor-default)` rather
   than the word "mismatch".
3. A probe that cannot be assessed — bad light, extreme pose, no face — is
   `NOT_ASSESSABLE`, never `MISMATCH`. Being hard to photograph is not
   evidence of impersonation, and conflating the two is how a system ends
   up disproportionately failing the people its models were worst at.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

Embedding = tuple[float, ...]


class MatchOutcome(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    NOT_ASSESSABLE = "not_assessable"
    """The probe could not be compared at all.

    Distinct from MISMATCH on purpose. A dark room, a candidate turned
    away, or a failed detection produces this — and it must never be
    escalated as though a different person had been seen.
    """


class QualityIssue(StrEnum):
    NO_FACE = "no_face"
    LOW_CONFIDENCE = "low_detector_confidence"
    EXTREME_POSE = "extreme_pose"
    TOO_SMALL = "face_too_small_in_frame"


@dataclass(frozen=True, slots=True)
class ProbeQuality:
    """What the capture looked like, independent of who is in it."""

    face_count: int
    detector_confidence: float
    yaw_deg: float
    pitch_deg: float
    face_fraction: float
    """Fraction of frame height occupied by the face. Small faces embed badly."""

    def issues(self, limits: QualityLimits) -> tuple[QualityIssue, ...]:
        found: list[QualityIssue] = []
        if self.face_count < 1:
            found.append(QualityIssue.NO_FACE)
        if self.detector_confidence < limits.min_detector_confidence:
            found.append(QualityIssue.LOW_CONFIDENCE)
        if abs(self.yaw_deg) > limits.max_yaw_deg or abs(self.pitch_deg) > limits.max_pitch_deg:
            found.append(QualityIssue.EXTREME_POSE)
        if self.face_fraction < limits.min_face_fraction:
            found.append(QualityIssue.TOO_SMALL)
        return tuple(found)


@dataclass(frozen=True, slots=True)
class QualityLimits:
    """When a probe is too poor to compare.

    Generous rather than strict. Rejecting a marginal probe costs one
    missed check; comparing it produces a similarity score that reflects
    the lighting rather than the person, and that number then sits in
    someone's audit trail looking like evidence.
    """

    min_detector_confidence: float = 0.7
    max_yaw_deg: float = 35.0
    max_pitch_deg: float = 30.0
    min_face_fraction: float = 0.12


@dataclass(frozen=True, slots=True)
class Threshold:
    """A similarity cutoff and, mandatorily, where it came from."""

    value: float
    calibrated_on: str
    """Free text describing the population and conditions it was measured
    against — "vendor default, LFW", "internal, 2026-06, 4k candidates".

    Required because a bare number invites the assumption that it is
    universal. It is not: the same cutoff is a different false-match rate
    for different groups, and a reviewer or auditor needs to know which
    measurement is being relied on."""

    def __post_init__(self) -> None:
        if not -1.0 <= self.value <= 1.0:
            raise ValueError("cosine threshold must lie in [-1, 1]")
        if not self.calibrated_on.strip():
            raise ValueError(
                "Threshold.calibrated_on must record the population this cutoff "
                "was measured against. Face-match error rates vary by one to two "
                "orders of magnitude across demographic groups, so an unattributed "
                "threshold is not a specification."
            )


@dataclass(frozen=True, slots=True)
class MatchPolicy:
    threshold: Threshold
    limits: QualityLimits = QualityLimits()
    enrolment_consistency: float = 0.75
    """Minimum pairwise similarity required among enrolment captures.

    If the enrolment frames do not resemble each other, the enrolment is
    unusable — the candidate moved, the light changed, or more than one
    person was in shot. Accepting it would bake a bad reference into every
    later comparison.
    """


@dataclass(frozen=True, slots=True)
class MatchResult:
    outcome: MatchOutcome
    similarity: float | None
    threshold: float
    calibrated_on: str
    issues: tuple[QualityIssue, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome": str(self.outcome),
            "similarity": self.similarity,
            "threshold": self.threshold,
            "calibrated_on": self.calibrated_on,
            "issues": [str(i) for i in self.issues],
        }


# -- vector maths -----------------------------------------------------------


def l2_normalise(vector: Embedding) -> Embedding:
    """Scale to unit length so cosine similarity is a plain dot product."""
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:
        raise ValueError("cannot normalise a zero vector")
    return tuple(component / norm for component in vector)


def cosine_similarity(a: Embedding, b: Embedding) -> float:
    """Cosine similarity of two embeddings, clamped to [-1, 1].

    Clamped because accumulated float error on 512-dimension vectors can
    produce 1.0000000000000002, and a similarity above 1 downstream looks
    like a bug in something more alarming than arithmetic.
    """
    if len(a) != len(b):
        raise ValueError(f"embedding length mismatch: {len(a)} vs {len(b)}")
    a_n, b_n = l2_normalise(a), l2_normalise(b)
    dot = sum(x * y for x, y in zip(a_n, b_n, strict=True))
    return max(-1.0, min(1.0, dot))


def centroid(embeddings: list[Embedding]) -> Embedding:
    """Mean of normalised embeddings, renormalised.

    Averaging several enrolment captures is more robust than trusting one
    frame's lighting and expression.
    """
    if not embeddings:
        raise ValueError("cannot take the centroid of no embeddings")
    normalised = [l2_normalise(e) for e in embeddings]
    dimensions = len(normalised[0])
    if any(len(e) != dimensions for e in normalised):
        raise ValueError("all enrolment embeddings must have the same length")
    summed = [sum(e[i] for e in normalised) for i in range(dimensions)]
    return l2_normalise(tuple(summed))


# -- enrolment --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Enrolment:
    reference: Embedding
    captures: int
    min_pairwise_similarity: float


class EnrolmentError(Exception):
    """Enrolment could not be accepted. The message is shown to the candidate."""


def build_enrolment(
    embeddings: list[Embedding],
    policy: MatchPolicy,
    minimum_captures: int = 3,
) -> Enrolment:
    """Combine enrolment captures into a reference template.

    Fails loudly rather than producing a weak reference. An enrolment that
    silently accepts inconsistent captures poisons every subsequent
    comparison for that candidate, and the failure surfaces later as
    "this person keeps failing identity checks" — attributed to them
    rather than to the enrolment.
    """
    if len(embeddings) < minimum_captures:
        raise EnrolmentError(
            f"need at least {minimum_captures} enrolment captures, got {len(embeddings)}"
        )

    worst = 1.0
    for i in range(len(embeddings)):
        for j in range(i + 1, len(embeddings)):
            worst = min(worst, cosine_similarity(embeddings[i], embeddings[j]))

    if worst < policy.enrolment_consistency:
        raise EnrolmentError(
            f"enrolment captures disagree with each other (lowest pairwise "
            f"similarity {worst:.3f} < {policy.enrolment_consistency}). Re-capture "
            "in even lighting, facing the camera."
        )

    return Enrolment(
        reference=centroid(embeddings),
        captures=len(embeddings),
        min_pairwise_similarity=worst,
    )


# -- verification -----------------------------------------------------------


def compare(
    probe: Embedding | None,
    enrolment: Enrolment,
    quality: ProbeQuality,
    policy: MatchPolicy,
) -> MatchResult:
    """Compare one probe against the enrolment reference.

    Quality is judged *before* similarity and short-circuits it. Computing
    a similarity from an unusable capture and then reporting it would put a
    meaningless number into an audit trail where it reads as evidence.
    """
    issues = quality.issues(policy.limits)
    if issues or probe is None:
        return MatchResult(
            outcome=MatchOutcome.NOT_ASSESSABLE,
            similarity=None,
            threshold=policy.threshold.value,
            calibrated_on=policy.threshold.calibrated_on,
            issues=issues or (QualityIssue.NO_FACE,),
        )

    similarity = cosine_similarity(probe, enrolment.reference)
    return MatchResult(
        outcome=(
            MatchOutcome.MATCH if similarity >= policy.threshold.value else MatchOutcome.MISMATCH
        ),
        similarity=similarity,
        threshold=policy.threshold.value,
        calibrated_on=policy.threshold.calibrated_on,
    )
