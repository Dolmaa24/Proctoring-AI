"""Identity verification: enrolment, face template comparison, temporal decision.

The most consequential claim this platform can make about a person is that
someone else sat their exam. Everything in this package is arranged so that
claim is slow, evidenced, attributable to a stated threshold, and reviewed
by a human — see `matching` for why, in particular the demographic accuracy
disparities that make an unattributed threshold meaningless.
"""

from .embedder import (
    DeterministicEmbedder,
    Embedder,
    EmbedderUnavailable,
    OnnxEmbedder,
    load_embedder,
)
from .matching import (
    Embedding,
    Enrolment,
    EnrolmentError,
    MatchOutcome,
    MatchPolicy,
    MatchResult,
    ProbeQuality,
    QualityIssue,
    QualityLimits,
    Threshold,
    build_enrolment,
    centroid,
    compare,
    cosine_similarity,
    l2_normalise,
)
from .verifier import (
    IdentityFinding,
    IdentityStatus,
    IdentityVerifier,
    SessionIdentity,
    VerificationPolicy,
)

__all__ = [
    "DeterministicEmbedder",
    "Embedder",
    "EmbedderUnavailable",
    "Embedding",
    "Enrolment",
    "EnrolmentError",
    "IdentityFinding",
    "IdentityStatus",
    "IdentityVerifier",
    "MatchOutcome",
    "MatchPolicy",
    "MatchResult",
    "OnnxEmbedder",
    "ProbeQuality",
    "QualityIssue",
    "QualityLimits",
    "SessionIdentity",
    "Threshold",
    "VerificationPolicy",
    "build_enrolment",
    "centroid",
    "compare",
    "cosine_similarity",
    "l2_normalise",
    "load_embedder",
]
