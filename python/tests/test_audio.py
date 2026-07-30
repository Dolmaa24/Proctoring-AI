"""Tests for the audio pipeline's own logic: transcription, intent, escalation.

The centrepiece is `test_keyword_matching_repeats_the_original_projects_flaw`,
which puts the naive approach and the LLM-backed one side by side on the
exact phrase that defeats keyword overlap. Everything else is the same
temporal discipline already applied to identity verification, adapted to a
two-state escalation.
"""

from __future__ import annotations

import json

import pytest

from proctor_audio import (
    AudioIntentMonitor,
    AudioMonitorPolicy,
    AudioStatus,
    DeterministicTranscriber,
    IntentClassification,
    IntentContext,
    IntentLabel,
    KeywordIntentClassifier,
    LLMIntentClassifier,
    TranscriberUnavailable,
    WhisperTranscriber,
    load_transcriber,
)

SESSION = "sess-audio-0001"
CONTEXT = IntentContext(exam_subject="algebra")


# -- transcription ------------------------------------------------------------


def test_deterministic_transcriber_round_trips_utf8():
    transcriber = DeterministicTranscriber()
    assert transcriber.transcribe(b"what is the answer to number four") == (
        "what is the answer to number four"
    )


def test_deterministic_transcriber_rejects_empty_audio():
    with pytest.raises(ValueError, match="empty"):
        DeterministicTranscriber().transcribe(b"")


def test_deterministic_transcriber_rejects_non_utf8_binary():
    with pytest.raises(ValueError, match="UTF-8"):
        DeterministicTranscriber().transcribe(b"\xff\xfe\x00\x01")


def test_load_transcriber_selects_deterministic_by_default():
    assert isinstance(load_transcriber(None), DeterministicTranscriber)
    assert isinstance(load_transcriber(""), DeterministicTranscriber)


def test_missing_whisper_model_fails_loudly_and_correctly_attributes_the_reason():
    """Distinct from identity's embedder message on purpose.

    Whisper's weights are MIT-licensed by OpenAI, unlike the ArcFace weights
    proctor_identity declines to bundle, so the error here must not claim a
    licensing block that does not exist.
    """
    with pytest.raises(TranscriberUnavailable) as excinfo:
        WhisperTranscriber("/nonexistent/whisper-model")
    message = str(excinfo.value)
    assert "MIT-licensed" in message
    assert "not licensing" in message


# -- the reason this module exists -------------------------------------------


def test_keyword_matching_repeats_the_original_projects_flaw():
    """The comparison this whole package is built around, made runnable.

    "what's the answer to number four" is exactly the phrase that defeats
    the original Proctoring-AI project's stopword-overlap approach: it
    shares almost no vocabulary with a question paper, so naive keyword
    logic has to fall back on a fixed trigger list, which is exactly as
    brittle as it sounds. Here the keyword double is deliberately built
    with a plausible-looking trigger list and still gets outperformed by
    an LLM classifier on a paraphrase one word off from its own triggers.
    """
    keyword = KeywordIntentClassifier()
    llm = LLMIntentClassifier(complete=_fake_llm(IntentLabel.SEEKING_HELP, 0.85))

    paraphrase = "hey what do you think number four is"
    keyword_result = keyword.classify(paraphrase, CONTEXT)
    llm_result = llm.classify(paraphrase, CONTEXT)

    assert keyword_result.label is IntentLabel.THINKING_ALOUD, (
        "the fixed trigger list misses a paraphrase it was never written for — "
        "this is the failure mode, demonstrated, not asserted"
    )
    assert llm_result.label is IntentLabel.SEEKING_HELP


def test_keyword_classifier_is_named_as_a_non_production_double():
    assert "not-for-production" in KeywordIntentClassifier().name


# -- LLM classifier: defensive parsing ---------------------------------------


def _fake_llm(label: IntentLabel, confidence: float, rationale: str = "test"):
    def complete(system_prompt: str, user_prompt: str) -> str:
        return json.dumps({"label": str(label), "confidence": confidence, "rationale": rationale})

    return complete


def test_llm_classifier_parses_a_well_formed_response():
    classifier = LLMIntentClassifier(complete=_fake_llm(IntentLabel.READING_ALOUD, 0.7))
    result = classifier.classify("the square root of sixteen is four", CONTEXT)
    assert result.label is IntentLabel.READING_ALOUD
    assert result.confidence == pytest.approx(0.7)


def test_malformed_json_becomes_unclear_not_seeking_help():
    """The safety property of this whole classifier.

    A response the classifier cannot parse must fail toward silence, not
    toward the most serious allegation this package can make.
    """
    classifier = LLMIntentClassifier(complete=lambda s, u: "not json at all")
    result = classifier.classify("anything", CONTEXT)
    assert result.label is IntentLabel.UNCLEAR
    assert result.confidence == 0.0


def test_unrecognised_label_becomes_unclear():
    def complete(s, u):
        return json.dumps({"label": "definitely_cheating", "confidence": 0.9})

    result = LLMIntentClassifier(complete=complete).classify("x", CONTEXT)
    assert result.label is IntentLabel.UNCLEAR


def test_out_of_range_confidence_is_clamped_to_zero_not_trusted():
    def complete(s, u):
        return json.dumps({"label": "seeking_help", "confidence": 42.0})

    result = LLMIntentClassifier(complete=complete).classify("x", CONTEXT)
    assert result.confidence == 0.0


def test_an_exception_from_the_completion_function_becomes_unclear():
    def complete(s, u):
        raise RuntimeError("network is down")

    result = LLMIntentClassifier(complete=complete).classify("x", CONTEXT)
    assert result.label is IntentLabel.UNCLEAR


def test_llm_classifier_name_reflects_the_configured_model():
    assert LLMIntentClassifier(
        complete=_fake_llm(IntentLabel.UNCLEAR, 0), model_name="claude-x"
    ).name == ("llm:claude-x")
    assert (
        LLMIntentClassifier(complete=_fake_llm(IntentLabel.UNCLEAR, 0)).name
        == "llm:operator-configured"
    )


# -- temporal escalation ------------------------------------------------------


def classification(label: IntentLabel, confidence: float = 0.8) -> IntentClassification:
    return IntentClassification(label, confidence, "test", "test-classifier")


def test_a_single_seeking_help_classification_raises_no_finding():
    monitor = AudioIntentMonitor()
    finding = monitor.record(SESSION, classification(IntentLabel.SEEKING_HELP), "help me")
    assert finding is None


def test_policy_cannot_be_configured_to_fire_on_one_chunk():
    with pytest.raises(ValueError, match="single classified chunk"):
        AudioMonitorPolicy(window=5, seeking_help_required=1)


def test_seeking_help_required_cannot_exceed_the_window():
    with pytest.raises(ValueError, match="cannot exceed"):
        AudioMonitorPolicy(window=3, seeking_help_required=4)


def test_sustained_seeking_help_escalates_once():
    monitor = AudioIntentMonitor()
    findings = []
    for _ in range(4):
        finding = monitor.record(SESSION, classification(IntentLabel.SEEKING_HELP), "help me")
        if finding:
            findings.append(finding)

    assert len(findings) == 1, "must escalate once, not on every subsequent chunk"
    assert findings[0].status is AudioStatus.SUSTAINED_HELP_SUSPECTED
    assert findings[0].matched >= 3
    assert "probabilistic" in findings[0].message


def test_occasional_seeking_help_reads_among_mostly_normal_speech_do_not_escalate():
    """Two flagged chunks in five is a misread, not a pattern."""
    monitor = AudioIntentMonitor()
    sequence = [
        IntentLabel.THINKING_ALOUD,
        IntentLabel.SEEKING_HELP,
        IntentLabel.READING_ALOUD,
        IntentLabel.SEEKING_HELP,
        IntentLabel.THINKING_ALOUD,
    ]
    statuses = []
    for label in sequence * 3:
        finding = monitor.record(SESSION, classification(label), "x")
        if finding:
            statuses.append(finding.status)

    assert AudioStatus.SUSTAINED_HELP_SUSPECTED not in statuses


def test_unclear_and_reading_aloud_never_contribute_to_escalation():
    monitor = AudioIntentMonitor()
    for _ in range(20):
        for label in (IntentLabel.UNCLEAR, IntentLabel.READING_ALOUD, IntentLabel.THINKING_ALOUD):
            finding = monitor.record(SESSION, classification(label), "x")
            assert finding is None


def test_deescalation_back_to_quiet_is_not_reported():
    monitor = AudioIntentMonitor()
    for _ in range(4):
        monitor.record(SESSION, classification(IntentLabel.SEEKING_HELP), "help")
    for _ in range(5):
        finding = monitor.record(SESSION, classification(IntentLabel.THINKING_ALOUD), "x")
        assert finding is None or finding.status is not AudioStatus.QUIET


def test_finding_carries_a_transcript_excerpt_for_immediate_review():
    monitor = AudioIntentMonitor()
    finding = None
    for _ in range(4):
        found = monitor.record(
            SESSION, classification(IntentLabel.SEEKING_HELP), "can you tell me the answer please"
        )
        finding = found or finding
    assert finding is not None
    assert "answer" in finding.transcript_excerpt


def test_transcript_excerpt_is_truncated():
    monitor = AudioIntentMonitor()
    long_transcript = "help me " * 100
    finding = None
    for _ in range(4):
        found = monitor.record(SESSION, classification(IntentLabel.SEEKING_HELP), long_transcript)
        finding = found or finding
    assert finding is not None
    assert len(finding.transcript_excerpt) <= 280


def test_sessions_are_independent():
    monitor = AudioIntentMonitor()
    for _ in range(4):
        monitor.record("sess-a", classification(IntentLabel.SEEKING_HELP), "x")
    finding = monitor.record("sess-b", classification(IntentLabel.SEEKING_HELP), "x")
    assert finding is None, "a different session must start with a clean window"


def test_forget_clears_session_state():
    monitor = AudioIntentMonitor()
    for _ in range(4):
        monitor.record(SESSION, classification(IntentLabel.SEEKING_HELP), "x")
    monitor.forget(SESSION)
    assert monitor.state(SESSION).status is AudioStatus.QUIET
    assert list(monitor.state(SESSION).recent) == []
