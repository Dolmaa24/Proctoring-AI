"""Tests for identity verification.

The claim this subsystem can make — that someone else sat the exam — is the
most serious one in the platform. Most of these tests exist to make that
claim hard to reach by accident.
"""

from __future__ import annotations

import math

import pytest

from proctor_identity import (
    DeterministicEmbedder,
    EnrolmentError,
    IdentityStatus,
    IdentityVerifier,
    MatchOutcome,
    MatchPolicy,
    ProbeQuality,
    QualityIssue,
    QualityLimits,
    Threshold,
    VerificationPolicy,
    build_enrolment,
    centroid,
    compare,
    cosine_similarity,
    l2_normalise,
)

SESSION = "sess-identity-0001"


def threshold(value: float = 0.55) -> Threshold:
    return Threshold(value=value, calibrated_on="test fixture, synthetic vectors")


def policy(value: float = 0.55, **kwargs) -> MatchPolicy:
    return MatchPolicy(threshold=threshold(value), **kwargs)


def good_quality(**overrides) -> ProbeQuality:
    base = dict(
        face_count=1,
        detector_confidence=0.95,
        yaw_deg=3.0,
        pitch_deg=-2.0,
        face_fraction=0.3,
    )
    base.update(overrides)
    return ProbeQuality(**base)


def vec(*values: float) -> tuple[float, ...]:
    return tuple(values)


# -- vector maths -----------------------------------------------------------


def test_cosine_of_identical_vectors_is_one():
    assert cosine_similarity(vec(1, 2, 3), vec(1, 2, 3)) == pytest.approx(1.0)


def test_cosine_ignores_magnitude():
    assert cosine_similarity(vec(1, 2, 3), vec(10, 20, 30)) == pytest.approx(1.0)


def test_cosine_of_opposite_vectors_is_minus_one():
    assert cosine_similarity(vec(1, 0), vec(-1, 0)) == pytest.approx(-1.0)


def test_cosine_stays_within_bounds_on_long_vectors():
    """Float error on 512 dimensions must not yield a similarity above 1."""
    a = tuple(math.sin(i) for i in range(512))
    assert -1.0 <= cosine_similarity(a, a) <= 1.0


def test_length_mismatch_is_rejected_rather_than_truncated():
    with pytest.raises(ValueError, match="length mismatch"):
        cosine_similarity(vec(1, 2, 3), vec(1, 2))


def test_zero_vector_cannot_be_normalised():
    with pytest.raises(ValueError):
        l2_normalise(vec(0, 0, 0))


def test_centroid_of_one_vector_is_itself_normalised():
    assert centroid([vec(3, 4)]) == pytest.approx((0.6, 0.8))


# -- threshold provenance ---------------------------------------------------


def test_threshold_requires_a_calibration_record():
    """A bare number invites the assumption that it is universal.

    Face-match error rates differ by one to two orders of magnitude across
    demographic groups, so a cutoff without a stated population is not a
    specification and a reviewer cannot interpret it.
    """
    with pytest.raises(ValueError, match="calibrated_on"):
        Threshold(value=0.55, calibrated_on="")
    with pytest.raises(ValueError, match="calibrated_on"):
        Threshold(value=0.55, calibrated_on="   ")


def test_threshold_must_be_a_valid_cosine():
    with pytest.raises(ValueError, match=r"\[-1, 1\]"):
        Threshold(value=1.5, calibrated_on="x")


def test_match_policy_has_no_default_threshold():
    """Forcing the caller to supply one keeps the choice deliberate."""
    with pytest.raises(TypeError):
        MatchPolicy()  # type: ignore[call-arg]


def test_every_result_carries_the_threshold_it_was_judged_against():
    enrolment = build_enrolment([vec(1, 0, 0)] * 3, policy())
    result = compare(vec(1, 0, 0), enrolment, good_quality(), policy(0.42))
    assert result.threshold == 0.42
    assert result.calibrated_on
    assert "threshold" in result.as_dict()


# -- enrolment --------------------------------------------------------------


def test_enrolment_requires_several_captures():
    with pytest.raises(EnrolmentError, match="at least 3"):
        build_enrolment([vec(1, 0)], policy())


def test_enrolment_rejects_inconsistent_captures():
    """A bad reference poisons every later comparison for that candidate.

    Worse, the failure surfaces later as "this person keeps failing
    identity checks" and gets attributed to them rather than to enrolment.
    """
    with pytest.raises(EnrolmentError, match="disagree"):
        build_enrolment([vec(1, 0, 0), vec(0, 1, 0), vec(0, 0, 1)], policy())


def test_enrolment_accepts_consistent_captures_and_reports_its_worst_pair():
    captures = [vec(1, 0, 0.02), vec(1, 0.01, 0), vec(0.99, 0, 0.01)]
    enrolment = build_enrolment(captures, policy())
    assert enrolment.captures == 3
    assert enrolment.min_pairwise_similarity > 0.99


# -- quality gating ---------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "issue"),
    [
        ({"face_count": 0}, QualityIssue.NO_FACE),
        ({"detector_confidence": 0.2}, QualityIssue.LOW_CONFIDENCE),
        ({"yaw_deg": 60.0}, QualityIssue.EXTREME_POSE),
        ({"pitch_deg": -55.0}, QualityIssue.EXTREME_POSE),
        ({"face_fraction": 0.02}, QualityIssue.TOO_SMALL),
    ],
)
def test_unusable_probes_are_not_assessable_never_mismatch(overrides, issue):
    """The single most important test in this file.

    A dark room, a turned head or a distant face must never read as "a
    different person". Conflating "cannot see" with "someone else" is
    exactly how a system ends up disproportionately failing the people its
    models were already worst at.
    """
    enrolment = build_enrolment([vec(1, 0, 0)] * 3, policy())
    # A probe that would be a clear mismatch if it were assessed at all.
    result = compare(vec(-1, 0, 0), enrolment, good_quality(**overrides), policy())

    assert result.outcome is MatchOutcome.NOT_ASSESSABLE
    assert result.similarity is None, "an unusable probe must not report a number"
    assert issue in result.issues


def test_a_usable_probe_of_a_different_face_is_a_mismatch():
    enrolment = build_enrolment([vec(1, 0, 0)] * 3, policy())
    result = compare(vec(-1, 0.1, 0), enrolment, good_quality(), policy())
    assert result.outcome is MatchOutcome.MISMATCH
    assert result.similarity is not None and result.similarity < 0


def test_a_usable_probe_of_the_same_face_matches():
    enrolment = build_enrolment([vec(1, 0, 0)] * 3, policy())
    result = compare(vec(0.99, 0.05, 0), enrolment, good_quality(), policy())
    assert result.outcome is MatchOutcome.MATCH


def test_quality_limits_are_generous_by_default():
    """Rejecting a marginal probe costs a check; judging one costs a person."""
    limits = QualityLimits()
    assert limits.max_yaw_deg >= 30
    assert limits.min_detector_confidence <= 0.8


# -- temporal decision ------------------------------------------------------


def enrolled_verifier(**policy_kwargs) -> IdentityVerifier:
    verifier = IdentityVerifier(policy(), VerificationPolicy(**policy_kwargs))
    verifier.enrol(SESSION, build_enrolment([vec(1, 0, 0)] * 3, policy()))
    return verifier


def test_a_single_mismatch_never_alleges_impersonation():
    verifier = enrolled_verifier()
    _, finding = verifier.probe(SESSION, vec(-1, 0, 0), good_quality())
    assert finding is None
    assert verifier.state(SESSION).status is not IdentityStatus.MISMATCH_SUSPECTED


def test_a_verification_policy_cannot_be_configured_to_fire_on_one_probe():
    with pytest.raises(ValueError, match="single probe"):
        VerificationPolicy(window=5, mismatches_required=1)


def test_mismatches_required_cannot_exceed_the_window():
    with pytest.raises(ValueError, match="cannot exceed"):
        VerificationPolicy(window=3, mismatches_required=4)


def test_sustained_mismatch_is_reported():
    verifier = enrolled_verifier()
    findings = []
    for _ in range(4):
        _, finding = verifier.probe(SESSION, vec(-1, 0, 0), good_quality())
        if finding:
            findings.append(finding)

    assert len(findings) == 1, "must report once, not on every probe"
    assert findings[0].status is IdentityStatus.MISMATCH_SUSPECTED
    assert findings[0].mismatched >= 3
    assert "demographic" in findings[0].message, (
        "the finding must carry its own caveat to whoever reads it"
    )


def test_occasional_bad_frames_do_not_accumulate_into_an_allegation():
    """Two bad frames in five is a shadow, not a stand-in."""
    verifier = enrolled_verifier()
    sequence = [vec(1, 0, 0), vec(-1, 0, 0), vec(1, 0, 0), vec(-1, 0, 0), vec(1, 0, 0)]
    statuses = []
    for embedding in sequence * 3:
        _, finding = verifier.probe(SESSION, embedding, good_quality())
        if finding:
            statuses.append(finding.status)

    assert IdentityStatus.MISMATCH_SUSPECTED not in statuses


def test_unassessable_probes_do_not_count_toward_mismatch():
    verifier = enrolled_verifier()
    for _ in range(20):
        _, finding = verifier.probe(SESSION, None, good_quality(face_count=0))
        if finding:
            assert finding.status is not IdentityStatus.MISMATCH_SUSPECTED


def test_a_long_run_of_unassessable_probes_is_reported_as_unobservable():
    """A distinct finding: 'we cannot see', not 'it is someone else'."""
    verifier = enrolled_verifier(unobservable_run=4)
    findings = []
    for _ in range(6):
        _, finding = verifier.probe(SESSION, None, good_quality(face_count=0))
        if finding:
            findings.append(finding)

    assert findings and findings[-1].status is IdentityStatus.UNOBSERVABLE
    assert "not a mismatch" in findings[-1].message


def test_a_clean_run_confirms_identity():
    verifier = enrolled_verifier()
    findings = []
    for _ in range(6):
        _, finding = verifier.probe(SESSION, vec(1, 0, 0), good_quality())
        if finding:
            findings.append(finding)

    assert findings[-1].status is IdentityStatus.CONFIRMED


def test_probing_without_enrolment_is_an_error_not_a_mismatch():
    verifier = IdentityVerifier(policy())
    with pytest.raises(LookupError):
        verifier.probe("sess-unenrolled", vec(1, 0, 0), good_quality())


def test_findings_carry_the_similarities_they_were_based_on():
    """A reviewer needs the numbers, not just the verdict."""
    verifier = enrolled_verifier()
    finding = None
    for _ in range(4):
        _, produced = verifier.probe(SESSION, vec(-1, 0, 0), good_quality())
        finding = produced or finding

    assert finding is not None
    assert finding.similarities
    assert all(s is not None for s in finding.similarities)


# -- embedder ---------------------------------------------------------------


def test_deterministic_embedder_is_stable_and_never_claims_to_be_a_face_model():
    embedder = DeterministicEmbedder()
    assert embedder.embed(b"abc") == embedder.embed(b"abc")
    assert embedder.embed(b"abc") != embedder.embed(b"xyz")
    assert "test" in embedder.name


def test_deterministic_embedder_rejects_empty_input():
    with pytest.raises(ValueError):
        DeterministicEmbedder().embed(b"")


def test_missing_onnx_model_fails_loudly_with_the_licensing_reason():
    from proctor_identity import EmbedderUnavailable, OnnxEmbedder

    with pytest.raises(EmbedderUnavailable, match="non-commercial"):
        OnnxEmbedder("/nonexistent/arcface.onnx")
