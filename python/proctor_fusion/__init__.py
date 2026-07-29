"""Fusion engine: temporal filtering and policy evaluation over edge telemetry."""

from __future__ import annotations

from pathlib import Path

import yaml

from .engine import EvidenceSample, FusionEngine, Violation
from .rules import (
    AutomatedAction,
    Condition,
    LivenessRule,
    Policy,
    Rule,
    Severity,
)

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "policies" / "default.yaml"


def load_policy(path: str | Path | None = None) -> Policy:
    """Load and validate a policy file.

    Validation is strict (`extra="forbid"` throughout): a typo in a rule
    field name fails at load time rather than silently disabling a rule
    that everyone assumes is protecting the exam.
    """
    target = Path(path) if path is not None else DEFAULT_POLICY_PATH
    with open(target, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return Policy.model_validate(raw)


__all__ = [
    "DEFAULT_POLICY_PATH",
    "AutomatedAction",
    "Condition",
    "EvidenceSample",
    "FusionEngine",
    "LivenessRule",
    "Policy",
    "Rule",
    "Severity",
    "Violation",
    "load_policy",
]
