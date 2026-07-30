"""Audio pipeline: transcription, intent classification, temporal escalation.

The edge already reports coarse voice-activity telemetry (`signal.audio` in
`proctor_protocol`, evaluated by the `sustained_speech` fusion rule) — that
existed before this package and keeps working independently. What this
package adds is the layer on top: when speech has been sustained, transcribe
it and classify *why* someone was talking, rather than just that they were.

See `intent.py` for why this does not repeat the original Proctoring-AI
project's stopword-overlap approach, and `proctor_gateway.app` for why the
feature is off by default and how it is gated.
"""

from .intent import (
    IntentClassification,
    IntentClassifier,
    IntentContext,
    IntentLabel,
    KeywordIntentClassifier,
    LLMIntentClassifier,
)
from .monitor import (
    AudioFinding,
    AudioIntentMonitor,
    AudioMonitorPolicy,
    AudioStatus,
    SessionAudio,
)
from .transcription import (
    DeterministicTranscriber,
    Transcriber,
    TranscriberUnavailable,
    WhisperTranscriber,
    load_transcriber,
)

__all__ = [
    "AudioFinding",
    "AudioIntentMonitor",
    "AudioMonitorPolicy",
    "AudioStatus",
    "DeterministicTranscriber",
    "IntentClassification",
    "IntentClassifier",
    "IntentContext",
    "IntentLabel",
    "KeywordIntentClassifier",
    "LLMIntentClassifier",
    "SessionAudio",
    "Transcriber",
    "TranscriberUnavailable",
    "WhisperTranscriber",
    "load_transcriber",
]
