"""Authentication of telemetry frames.

Wire format
-----------
A signed frame is a JSON object with exactly two fields::

    {"b": "<base64url of the envelope JSON bytes>", "s": "<hex HMAC-SHA256>"}

The signature covers the base64 payload *exactly as transmitted*, and the
receiver verifies against the bytes it received rather than against a
re-serialisation of the parsed object.

That indirection is not ceremony. Canonical-JSON signing across a Python
server and a JavaScript client is a trap: ``0.1 + 0.2`` serialises as
``0.30000000000000004`` in both, but plenty of other floats do not agree
between Python's ``repr`` and JS's ``Number.prototype.toString``, and every
signal in this protocol is float-valued. Signing opaque bytes, JWS-style,
sidesteps the entire class of bug.

What this buys us
-----------------
An HMAC proves a frame came from a client instance that was provisioned
with the session key, and that it has not been altered in flight. It does
NOT prove the computer vision was honest — the key lives on a machine the
candidate controls, and a determined attacker can extract it and sign
whatever they like.

The defences that actually address a hostile client are elsewhere:
sequence continuity (§ gateway), server-side frame sampling, and the
uploaded session recording. This module is the cheap layer that stops the
casual attacker and makes the expensive layers meaningful.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Final

from .events import Envelope

_HKDF_INFO: Final = b"proctor-telemetry-v1"
_KEY_LEN: Final = 32


class SignatureError(Exception):
    """Raised when a frame fails authentication."""


def derive_session_key(master_secret: bytes, session_id: str) -> bytes:
    """HKDF-SHA256 (RFC 5869) restricted to a single 32-byte output block.

    Each session gets its own key derived from the server master secret, so
    a key extracted from one candidate's machine is worthless against any
    other session. The server recomputes rather than stores.
    """
    if not master_secret:
        raise ValueError("master_secret must not be empty")

    salt = session_id.encode("utf-8")
    prk = hmac.new(salt, master_secret, hashlib.sha256).digest()
    okm = hmac.new(prk, _HKDF_INFO + b"\x01", hashlib.sha256).digest()
    return okm[:_KEY_LEN]


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError) as exc:
        raise SignatureError("payload is not valid base64url") from exc


def sign_envelope(envelope: Envelope, session_key: bytes) -> str:
    """Serialise and sign an envelope, returning the JSON frame to transmit."""
    body = envelope.model_dump_json(exclude_none=True).encode("utf-8")
    b64 = _b64encode(body)
    sig = hmac.new(session_key, b64.encode("ascii"), hashlib.sha256).hexdigest()
    return json.dumps({"b": b64, "s": sig}, separators=(",", ":"))


def verify_frame(frame: str | bytes, session_key: bytes) -> Envelope:
    """Authenticate a received frame and return the parsed envelope.

    Raises `SignatureError` for anything malformed or unauthenticated. The
    caller should treat every failure identically — a client that cannot
    produce valid frames is not one whose error messages we want to help
    debug over the wire.
    """
    if isinstance(frame, bytes):
        try:
            frame = frame.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SignatureError("frame is not valid UTF-8") from exc

    try:
        outer = json.loads(frame)
    except json.JSONDecodeError as exc:
        raise SignatureError("frame is not valid JSON") from exc

    if not isinstance(outer, dict) or outer.keys() != {"b", "s"}:
        raise SignatureError("frame must have exactly the keys 'b' and 's'")

    b64, sig = outer["b"], outer["s"]
    if not isinstance(b64, str) or not isinstance(sig, str):
        raise SignatureError("frame fields must be strings")

    expected = hmac.new(session_key, b64.encode("ascii"), hashlib.sha256).hexdigest()
    # compare_digest, not ==, so we do not leak the signature via timing.
    if not hmac.compare_digest(expected, sig):
        raise SignatureError("signature mismatch")

    try:
        return Envelope.model_validate_json(_b64decode(b64))
    except SignatureError:
        raise
    except Exception as exc:
        raise SignatureError("payload is not a valid envelope") from exc
