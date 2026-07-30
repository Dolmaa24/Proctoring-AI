"""Speech-to-text: turning an audio chunk into a transcript.

Pure interface plus a test double, no model, so the storage, retention and
temporal-escalation logic around transcripts can be tested without a real
speech model — same shape as `proctor_identity.embedder`.

A licensing note, because it differs from the identity module's
------------------------------------------------------------------
No Whisper weights are bundled here either, but not for the reason ArcFace
weights are not bundled. Whisper's code and model checkpoints are released
by OpenAI under the MIT licence, so vendoring a small model would not be a
licence violation the way shipping InsightFace's ArcFace weights would be.

It is excluded for a practical reason instead: the runtime dependency
(`faster-whisper`/ctranslate2, or `openai-whisper`/torch) is hundreds of
megabytes and the smallest usable model is still tens of megabytes, which
is disproportionate to add to every install of this repository for a
feature that is off by default. Operators who want real transcription
install the dependency themselves and point `PROCTOR_AUDIO_MODEL` at a
downloaded model directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Transcriber(Protocol):
    @property
    def name(self) -> str: ...

    def transcribe(self, audio_bytes: bytes) -> str: ...


class TranscriberUnavailable(RuntimeError):
    """No usable speech-to-text backend is configured."""


class DeterministicTranscriber:
    """Test double: the "audio" bytes *are* the UTF-8 transcript.

    Not a speech model and never presented as one. It exists so that
    enrolment-adjacent logic — storage, retention, the temporal escalation
    in `monitor.py` — can be exercised with fully controlled transcript
    content, with no audio codec and no real model in the loop. A test
    that wants the transcript "what is the answer to number four" simply
    encodes that string as the chunk.
    """

    name = "deterministic-test-transcriber"

    def transcribe(self, audio_bytes: bytes) -> str:
        if not audio_bytes:
            raise ValueError("cannot transcribe an empty audio chunk")
        try:
            return audio_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "DeterministicTranscriber expects UTF-8 text standing in for "
                "audio bytes; got binary data. Configure PROCTOR_AUDIO_MODEL "
                "for real audio."
            ) from exc


class WhisperTranscriber:
    """`faster-whisper`-backed transcription. Not bundled — see module docstring."""

    def __init__(self, model_path: str | Path) -> None:
        self.model_path = Path(model_path)
        self._model = None

        if not self.model_path.exists():
            raise TranscriberUnavailable(
                f"no speech model at {self.model_path}. Install faster-whisper "
                "(`pip install faster-whisper`), download a CTranslate2 Whisper "
                "export, and set PROCTOR_AUDIO_MODEL to its directory. Not bundled "
                "because of dependency and model size, not licensing — Whisper's "
                "weights are MIT-licensed by OpenAI."
            )

    @property
    def name(self) -> str:
        return f"whisper:{self.model_path.name}"

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        try:
            from faster_whisper import WhisperModel  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise TranscriberUnavailable(
                "faster-whisper is not installed; `pip install faster-whisper`"
            ) from exc
        self._model = WhisperModel(str(self.model_path))

    def transcribe(self, audio_bytes: bytes) -> str:  # pragma: no cover - needs a model
        self._ensure_model()
        raise NotImplementedError(
            "wire container decoding (webm/opus -> PCM16) to the model here once "
            "a real model is in place"
        )


def load_transcriber(model_path: str | None) -> Transcriber:
    """Empty path selects the deterministic test double."""
    if not model_path:
        return DeterministicTranscriber()
    return WhisperTranscriber(model_path)
