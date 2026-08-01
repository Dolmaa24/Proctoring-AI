"""Generate the TypeScript event types from the Pydantic schema.

The Python models are the single source of truth for the wire format. This
emits their TypeScript equivalent so the edge client and the backend cannot
drift apart silently — a mismatch here is a class of bug that shows up as
"the gateway ignores a signal" months later, with no error anywhere.

    python tools/generate_ts_protocol.py            # write the file
    python tools/generate_ts_protocol.py --check    # fail if stale (CI)
"""

from __future__ import annotations

import argparse
import enum
import sys
import types
import typing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from pydantic import BaseModel  # noqa: E402

from proctor_protocol import events  # noqa: E402

OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "apps"
    / "client"
    / "src"
    / "protocol"
    / "events.generated.ts"
)

def _payload_models() -> list[type[BaseModel]]:
    """The members of `Payload`, read from the annotation itself.

    Derived rather than listed by hand. A hand-maintained list drifts the
    moment someone adds a payload type: the `Envelope` interface below is
    generated from the real annotation, so a missing entry here emitted a
    union referencing an interface that was never written — TypeScript
    that does not compile, from a generator whose whole job is stopping
    the two halves of the protocol from disagreeing.
    """
    annotation = events.Payload
    # Annotated[X | Y, Field(...)] -> unwrap to the union, then to members.
    if typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    return list(typing.get_args(annotation))


def _enums_used_by(models: list[type[BaseModel]]) -> list[type[enum.Enum]]:
    """Every enum reachable from a field, in first-seen order.

    Same reasoning as `_payload_models`: `ts_type` emits an enum by name,
    so an enum that is used but never declared is a dangling reference.
    """
    found: list[type[enum.Enum]] = []
    for model in models:
        for field in model.model_fields.values():
            for candidate in (field.annotation, *typing.get_args(field.annotation)):
                if (
                    isinstance(candidate, type)
                    and issubclass(candidate, enum.Enum)
                    and candidate not in found
                ):
                    found.append(candidate)
    return found


PAYLOAD_MODELS = _payload_models()

SUPPORT_MODELS = [events.BoundingBox]
ENUMS = _enums_used_by([*SUPPORT_MODELS, *PAYLOAD_MODELS, events.Envelope])


def ts_type(annotation: object) -> str:
    """Map a Python annotation to its TypeScript equivalent.

    Deliberately narrow: it handles exactly the constructs the protocol
    uses and raises on anything else, so adding an unsupported type to the
    schema fails here rather than emitting silently wrong TypeScript.
    """
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if annotation is str:
        return "string"
    if annotation is bool:
        return "boolean"
    if annotation in (int, float):
        return "number"

    if origin is typing.Literal:
        return " | ".join(f'"{a}"' for a in args)

    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        # Reference the exported alias rather than inlining the members, so
        # the generated file reads as a schema instead of a wall of literals.
        return annotation.__name__

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation.__name__

    if origin in (types.UnionType, typing.Union):
        return " | ".join(ts_type(a) for a in args if a is not type(None)) + (
            " | null" if type(None) in args else ""
        )

    if origin in (tuple, list):
        if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
            return f"{ts_type(args[0])}[]"
        return f"{ts_type(args[0])}[]"

    if origin is dict:
        return f"Record<{ts_type(args[0])}, {ts_type(args[1])}>"

    raise TypeError(f"no TypeScript mapping for {annotation!r}")


def emit_interface(model: type[BaseModel]) -> str:
    lines = [f"export interface {model.__name__} {{"]
    for name, field in model.model_fields.items():
        rendered = ts_type(field.annotation)
        # `type` carries a default in Python purely for ergonomics, but on
        # the wire it is the discriminator: mark it required so TypeScript
        # can narrow the union, and so a client cannot omit it.
        optional = "" if (field.is_required() or name == "type") else "?"
        lines.append(f"  {name}{optional}: {rendered};")
    lines.append("}")
    return "\n".join(lines)


def generate() -> str:
    parts = [
        "// GENERATED FILE — DO NOT EDIT BY HAND.",
        "//",
        "// Source of truth: python/proctor_protocol/events.py",
        "// Regenerate:     python tools/generate_ts_protocol.py",
        "//",
        "// The Python models define the wire format. This file exists so the",
        "// edge client cannot drift from them without CI noticing.",
        "",
        f"export const PROTOCOL_VERSION = {events.PROTOCOL_VERSION};",
        "",
    ]

    for enum_type in ENUMS:
        members = " | ".join(f'"{m.value}"' for m in enum_type)
        parts.append(f"export type {enum_type.__name__} = {members};")
    parts.append("")

    for model in SUPPORT_MODELS:
        parts.append(emit_interface(model))
        parts.append("")

    for model in PAYLOAD_MODELS:
        parts.append(emit_interface(model))
        parts.append("")

    union = "\n  | ".join(m.__name__ for m in PAYLOAD_MODELS)
    parts.append(f"export type Payload =\n  | {union};")
    parts.append("")
    parts.append(emit_interface(events.Envelope))
    parts.append("")

    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    generated = generate()

    if args.check:
        if not OUTPUT.exists():
            print(f"{OUTPUT} does not exist; run: python {sys.argv[0]}")
            return 1
        if OUTPUT.read_text(encoding="utf-8") != generated:
            print(
                f"{OUTPUT} is stale.\n"
                f"The Python schema changed without regenerating the TypeScript.\n"
                f"Run: python {sys.argv[0]}"
            )
            return 1
        print("TypeScript protocol types are up to date.")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(generated, encoding="utf-8")
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
