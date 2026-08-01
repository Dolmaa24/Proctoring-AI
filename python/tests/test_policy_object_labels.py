"""The shipped policy's object rules, checked against what a detector can emit.

`wearable_detected` matches on `smartwatch` and `headphones`. The bundled
COCO-80 detector has neither class, so that rule is inert: structurally
valid, evaluated against every `signal.object`, and incapable of ever
matching. It is kept in the policy deliberately — deleting it would hide
the gap — but "kept deliberately" and "forgotten" look identical in a
YAML file six months later.

These tests are what makes the difference visible. They pin which labels
are reachable, name every inert rule explicitly, and fail if someone adds
a rule for a class nothing produces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from proctor_fusion import load_policy
from proctor_fusion.rules import Condition, Rule
from proctor_protocol import DETECTABLE_LABELS, UNDETECTABLE_LABELS, ObjectLabel

POLICY_PATH = Path(__file__).resolve().parents[2] / "policies" / "default.yaml"


def labels_in(condition: Condition) -> set[str]:
    """Every `label` value a condition tree compares against.

    Walks the tree rather than reading the top level, since a rule is free
    to bury its label check inside an `any_of` — and a check that only
    looked one level down would quietly pass a rule it never inspected.
    """
    found: set[str] = set()
    if condition.field == "label" and condition.value is not None:
        value = condition.value
        found.update(value if isinstance(value, list) else [value])
    for branch in (condition.all_of, condition.any_of):
        for child in branch or ():
            found |= labels_in(child)
    return found


def object_rules() -> list[Rule]:
    policy = load_policy(POLICY_PATH)
    return [rule for rule in policy.rules if rule.signal == "signal.object"]


def test_every_label_set_is_accounted_for():
    """Adding an ObjectLabel forces a decision about whether it is reachable.

    Without this, a new enum member silently belongs to neither set and
    the reachability checks below stop covering it.
    """
    assert DETECTABLE_LABELS | UNDETECTABLE_LABELS == set(ObjectLabel)
    assert not (DETECTABLE_LABELS & UNDETECTABLE_LABELS)


def test_policy_object_rules_only_reference_real_labels():
    """A typo'd label is indistinguishable from an inert rule at runtime.

    Both simply never match, so neither shows up as an error anywhere —
    which is why this is asserted rather than left to review.
    """
    valid = {label.value for label in ObjectLabel}
    for rule in object_rules():
        unknown = labels_in(rule.when) - valid
        assert not unknown, f"rule {rule.id} matches on unknown label(s): {sorted(unknown)}"


def test_wearable_detected_is_the_only_inert_rule():
    """Names the gap, and fails if it grows.

    If a detector for these classes is ever supplied, move the labels into
    DETECTABLE_LABELS and this test will fail — correctly, because the
    documented gap will have closed and should stop being advertised.
    """
    undetectable = {label.value for label in UNDETECTABLE_LABELS}
    inert = {
        rule.id
        for rule in object_rules()
        if (found := labels_in(rule.when)) and found <= undetectable
    }
    assert inert == {"wearable_detected"}


def test_rules_are_not_partly_inert():
    """A rule mixing reachable and unreachable labels is the dangerous shape.

    It fires — so it looks healthy — while silently never matching half of
    what it claims to cover. A reviewer would reasonably read
    `label in [phone, smartwatch]` as covering both.
    """
    detectable = {label.value for label in DETECTABLE_LABELS}
    undetectable = {label.value for label in UNDETECTABLE_LABELS}
    for rule in object_rules():
        found = labels_in(rule.when)
        assert not (found & detectable and found & undetectable), (
            f"rule {rule.id} mixes reachable and unreachable labels "
            f"({sorted(found)}); it would fire while silently covering less "
            "than it appears to"
        )


@pytest.mark.parametrize("label", sorted(x.value for x in DETECTABLE_LABELS))
def test_detectable_labels_have_a_coco_source(label):
    """Guards the other direction: DETECTABLE_LABELS claiming too much.

    The mapping lives in the client (apps/client/src/renderer/objects.ts)
    and is asserted there too. This reads it as text rather than importing
    it — the alternative is a Node round trip inside a pytest run — so it
    is deliberately a loose containment check, not a parse.
    """
    mapping = (
        Path(__file__).resolve().parents[2] / "apps" / "client" / "src" / "renderer" / "objects.ts"
    ).read_text(encoding="utf-8")
    table = mapping.split("COCO_TO_LABEL", 1)[1].split("};", 1)[0]
    assert f'"{label}"' in table or f": {label}," in table, (
        f"{label} is listed as detectable but the client's COCO mapping never produces it"
    )
