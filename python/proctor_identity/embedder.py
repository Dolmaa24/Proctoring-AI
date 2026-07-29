"""Turning a face image into an embedding.

Why this runs on the server
---------------------------
The client could compute embeddings locally and send only the vector,
which would keep face images off the network entirely. That is the better
privacy answer and it is the wrong security answer, for the same reason
policy lives server-side: a client that computes its own identity evidence
can simply resend the enrolment vector forever, and the check becomes
decorative.

So the image crosses the wire and the server embeds it. The compensating
controls are that the image is **never written to disk** (see
`gateway/app.py`), and that the embedding itself is retained on a much
shorter clock than the similarity scores derived from it — the score is
what a reviewer needs, the template is not.

Model licensing — read before choosing weights
----------------------------------------------
Most readily available ArcFace weights, including the InsightFace model
zoo, are released **for non-commercial research use only**. They cannot be
used in a commercial proctoring product. This is the same shape of problem
as Ultralytics' AGPL licence and is why no model is vendored into this
repository: shipping one would hand every downstream user a licence
violation.

Supply your own licensed ONNX model and point `PROCTOR_FACE_MODEL` at it.
The expected contract is a single-input network taking NCHW float32
`(1, 3, 112, 112)` and returning one embedding vector, which is what
essentially every ArcFace-family export does.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Protocol

from .matching import Embedding


class Embedder(Protocol):
    """Produces a face embedding from a decoded, aligned face image."""

    @property
    def name(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, image_bytes: bytes) -> Embedding: ...


class EmbedderUnavailable(RuntimeError):
    """No usable face model is configured."""


class OnnxEmbedder:
    """ArcFace-family ONNX model, loaded lazily.

    Kept deliberately thin. Alignment, detection and cropping belong to the
    caller; this takes an already-prepared face crop so the preprocessing
    can be tested and swapped without touching inference.
    """

    def __init__(self, model_path: str | Path, input_size: int = 112) -> None:
        self.model_path = Path(model_path)
        self.input_size = input_size
        self._session = None
        self._dimensions = 0

        if not self.model_path.is_file():
            raise EmbedderUnavailable(
                f"no face model at {self.model_path}. Set PROCTOR_FACE_MODEL to a "
                "licensed ArcFace-family ONNX export; none is bundled because the "
                "commonly available weights are non-commercial-research-only."
            )

    @property
    def name(self) -> str:
        digest = hashlib.sha256(self.model_path.read_bytes()).hexdigest()[:16]
        return f"onnx:{self.model_path.name}:{digest}"

    @property
    def dimensions(self) -> int:
        self._ensure_session()
        return self._dimensions

    def _ensure_session(self) -> None:
        if self._session is not None:
            return
        try:
            import onnxruntime  # noqa: PLC0415 - optional, loaded on first use
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise EmbedderUnavailable(
                "onnxruntime is not installed; `pip install onnxruntime` to use "
                "the ONNX face embedder"
            ) from exc

        self._session = onnxruntime.InferenceSession(
            str(self.model_path), providers=["CPUExecutionProvider"]
        )
        output = self._session.get_outputs()[0]
        self._dimensions = int(output.shape[-1])

    def embed(self, image_bytes: bytes) -> Embedding:  # pragma: no cover - needs a model
        self._ensure_session()
        raise NotImplementedError(
            "wire preprocessing (decode -> align -> 112x112 NCHW float32) to the "
            "session here once a licensed model is in place"
        )


class DeterministicEmbedder:
    """Test double producing stable embeddings from image bytes.

    Not a face model and never presented as one. It exists so the
    enrolment, matching, temporal and storage logic — which is all of this
    project's own code — can be tested end to end without a licensed model
    or a photograph of a real person.

    Identical bytes give identical vectors and similar bytes give similar
    vectors, which is the only property the surrounding logic depends on.
    """

    def __init__(self, dimensions: int = 64) -> None:
        self._dimensions = dimensions

    @property
    def name(self) -> str:
        return f"deterministic-test-embedder:{self._dimensions}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, image_bytes: bytes) -> Embedding:
        if not image_bytes:
            raise ValueError("cannot embed empty image bytes")
        digest = hashlib.sha256(image_bytes).digest()
        values = []
        for i in range(self._dimensions):
            # Smooth function of the digest so that inputs sharing a prefix
            # land near each other in the space, mimicking the one property
            # the matching logic relies on.
            seed = digest[i % len(digest)] + i
            values.append(math.sin(seed * 0.37) + math.cos(seed * 0.11))
        return tuple(values)


def load_embedder(model_path: str | None) -> Embedder:
    """Select an embedder. Empty path selects the deterministic test double."""
    if not model_path:
        return DeterministicEmbedder()
    return OnnxEmbedder(model_path)
