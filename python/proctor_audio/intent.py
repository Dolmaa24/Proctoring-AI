"""Classifying what a transcript means, not just what words it contains.

Why this module exists
-----------------------
The original Proctoring-AI project's audio approach was to strip stopwords
from both the transcript and the exam question paper, then report the
overlap as a cheating signal. That fails in the direction that matters:
"what is the answer to number four" shares almost no vocabulary with the
question paper and sails straight through, while a candidate reading a
question aloud to concentrate — not cheating — matches the paper's own
wording closely and gets flagged for nothing. Keyword overlap measures
lexical similarity, not intent, and those are different things.

`KeywordIntentClassifier` below is kept for exactly one reason: to make
that failure a runnable difference rather than a claim in a docstring. It
is never wired into the gateway — see `proctor_gateway.app` — and its own
docstring says so.

The three-way split
--------------------
`THINKING_ALOUD`, `READING_ALOUD` and `UNCLEAR` are all treated as
non-findings: none of them contribute to escalation (`monitor.py`). Only
sustained `SEEKING_HELP` does. A classifier that cannot decide must default
toward silence, not toward an accusation — the asymmetry matters because
the cost of a false `SEEKING_HELP` is a real person investigated for
cheating, and the cost of a missed one is, at worst, parity with never
having built this feature.

No vendor lives in this file
------------------------------
`LLMIntentClassifier` takes an injected `complete` callable rather than
calling any specific provider's SDK. That keeps this package free of API
keys, network code, and a hardcoded vendor choice, and it is what makes
the classifier testable with a canned function instead of a live network
call. Wiring a real model means the operator writes a small adapter and
passes it to `Settings(llm_complete=...)`; see ARCHITECTURE.md.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class IntentLabel(StrEnum):
    THINKING_ALOUD = "thinking_aloud"
    READING_ALOUD = "reading_aloud"
    SEEKING_HELP = "seeking_help"
    UNCLEAR = "unclear"
    """Too short, garbled, or ambiguous to place confidently.

    Scored identically to THINKING_ALOUD for escalation purposes: an
    inconclusive read is not evidence of anything."""


@dataclass(frozen=True, slots=True)
class IntentContext:
    """What the classifier is told about the situation, to judge in context."""

    exam_subject: str = ""
    """Free text, e.g. "high school algebra". Helps a classifier recognise
    that spoken numbers and formulae are normal, not suspicious, for this
    exam — without it, reciting an equation looks identical to reciting a
    phone number someone gave you."""


@dataclass(frozen=True, slots=True)
class IntentClassification:
    label: IntentLabel
    confidence: float
    rationale: str
    classifier: str

    def as_dict(self) -> dict[str, object]:
        return {
            "label": str(self.label),
            "confidence": self.confidence,
            "rationale": self.rationale,
            "classifier": self.classifier,
        }


class IntentClassifier(Protocol):
    @property
    def name(self) -> str: ...

    def classify(self, transcript: str, context: IntentContext) -> IntentClassification: ...


SYSTEM_PROMPT = """You are assisting exam proctoring by classifying a short transcript of \
a candidate's speech during a test. You do not decide whether the candidate cheated — a \
human reviewer does that; your job is only to categorise the transcript.

Classify into exactly one of:
- thinking_aloud: reasoning to themselves, muttering, verbalising their own thought \
process, including reading their own working or saying numbers aloud.
- reading_aloud: reading the exam question or their own answer aloud, to themselves.
- seeking_help: addressing another person, asking a question directed outward for an \
answer, or a response from someone else is audible in the room.
- unclear: too short, garbled, or ambiguous to classify with confidence.

Err toward thinking_aloud or unclear when genuinely uncertain: this classification can \
trigger review of a real person's exam, and a wrong seeking_help is a false accusation.

Respond with exactly one JSON object and nothing else: \
{"label": "...", "confidence": 0.0-1.0, "rationale": "one sentence"}"""


class KeywordIntentClassifier:
    """The naive approach, kept only to demonstrate why it is not used.

    This is close to a restatement of the original project's stopword-
    overlap logic, translated to trigger phrases, and it carries the same
    blind spot on purpose: "what's the answer to number 4" is exactly the
    kind of phrase a fixed trigger list will miss or catch inconsistently,
    while a candidate reading the question's own wording aloud risks a
    false match. See `test_audio.py` for the two side by side.

    Never imported by `proctor_gateway`. If you find yourself wiring this
    into anything that makes a decision about a real person, stop —
    that is the mistake this module exists to document.
    """

    name = "keyword-test-double-not-for-production"

    _TRIGGERS = (
        "what is the answer",
        "what's the answer",
        "tell me the answer",
        "can you help me",
    )

    def classify(self, transcript: str, context: IntentContext) -> IntentClassification:
        lowered = transcript.lower()
        if any(trigger in lowered for trigger in self._TRIGGERS):
            return IntentClassification(
                IntentLabel.SEEKING_HELP, 0.6, "matched a fixed trigger phrase", self.name
            )
        return IntentClassification(
            IntentLabel.THINKING_ALOUD, 0.5, "no trigger phrase matched", self.name
        )


class LLMIntentClassifier:
    """Delegates to an injected completion function.

    `complete(system_prompt, user_prompt) -> raw_text` is supplied by the
    operator — typically a thin wrapper around their own model client —
    which keeps this module free of network code and a hardcoded vendor.

    The response is parsed defensively. Anything that fails to parse, or
    that names a label this module does not recognise, becomes UNCLEAR
    rather than propagating an exception or defaulting toward
    SEEKING_HELP: failing open toward silence is the safe direction here,
    because the cost of a false SEEKING_HELP is a person wrongly
    investigated, and the cost of a missed one is not worse than never
    having built this feature.
    """

    def __init__(self, complete: Callable[[str, str], str], model_name: str = "") -> None:
        self._complete = complete
        self._model_name = model_name or "operator-configured"

    @property
    def name(self) -> str:
        return f"llm:{self._model_name}"

    def classify(self, transcript: str, context: IntentContext) -> IntentClassification:
        prompt = self._build_prompt(transcript, context)
        try:
            raw = self._complete(SYSTEM_PROMPT, prompt)
            parsed = json.loads(raw)
            label = IntentLabel(parsed["label"])
            confidence = float(parsed["confidence"])
            rationale = str(parsed.get("rationale", ""))
        except Exception as exc:
            return IntentClassification(
                IntentLabel.UNCLEAR,
                0.0,
                f"classifier response could not be parsed ({type(exc).__name__}); "
                "treated as unclear rather than risk a false escalation",
                self.name,
            )

        if not 0.0 <= confidence <= 1.0:
            confidence = 0.0
        return IntentClassification(label, confidence, rationale, self.name)

    @staticmethod
    def _build_prompt(transcript: str, context: IntentContext) -> str:
        subject = f"Exam subject: {context.exam_subject}\n" if context.exam_subject else ""
        return f"{subject}Transcript: {transcript!r}"
