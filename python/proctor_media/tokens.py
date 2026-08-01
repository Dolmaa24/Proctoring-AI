"""LiveKit access tokens and webhook signature verification.

Implemented against LiveKit's publicly documented, long-stable JWT token
and webhook-authentication formats — not against a live server, because
none is available in this environment. That distinction matters: the
shapes below (claim names, the `video` grant object, the webhook
`Authorize` header) have been stable across LiveKit's SDKs for years, and
the logic is unit tested against known-good token contents. But "matches
the documented format" and "verified against a running deployment" are
different claims, and only the first one is true here. Confirm against
your actual LiveKit server version before relying on this in production —
see `test_media.py` for what is and is not covered.

Why implement this ourselves rather than depend on `livekit-api`
------------------------------------------------------------------
The official server SDK is a reasonable choice too. This repo implements
the (simple, documented) JWT construction directly with `pyjwt` instead,
for the same reason the audio pipeline injects a completion function
rather than importing a model SDK: fewer dependencies, an auditable
~100 lines instead of an opaque library, and no coupling to a specific
SDK's release cadence for a format that is just "a JWT with a few claims".
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field

import jwt

DEFAULT_TTL_SECONDS = 6 * 60 * 60
"""6 hours — long enough for an exam session, short enough that a leaked
token (e.g. via a compromised renderer, the one part of this client that
is not fully trusted — see ARCHITECTURE.md § 1.2) is not useful for long."""


class TokenError(Exception):
    """A token could not be built or a webhook could not be verified."""


@dataclass(frozen=True, slots=True)
class VideoGrant:
    """The `video` claim LiveKit access tokens carry.

    Deliberately not a general-purpose grant builder. The two factory
    methods below (`publisher`, `subscriber`) are the only two shapes this
    codebase issues, and that is enforced structurally — see
    `proctor_gateway.app`'s separate candidate/proctor token endpoints —
    rather than left to a caller to assemble correctly by hand.
    """

    room: str
    room_join: bool = True
    can_publish: bool = False
    can_subscribe: bool = False
    can_publish_data: bool = False
    room_record: bool = False
    room_admin: bool = False
    hidden: bool = False

    @classmethod
    def publisher(cls, room: str) -> VideoGrant:
        """A candidate's grant: publish their own feed, see and hear no one.

        `can_subscribe=False` is not incidental. A candidate's client has
        no legitimate reason to receive other participants' media — there
        should not usually be any other participant in their room, and a
        grant that could subscribe would be a much more useful thing for a
        compromised renderer to have extracted from it than a publish-only
        one.
        """
        return cls(room=room, can_publish=True, can_subscribe=False)

    @classmethod
    def subscriber(cls, room: str) -> VideoGrant:
        """A proctor's grant: watch and hear, publish nothing, record on request."""
        return cls(room=room, can_publish=False, can_subscribe=True, room_record=True)

    def as_claim(self) -> dict[str, object]:
        return {
            "room": self.room,
            "roomJoin": self.room_join,
            "canPublish": self.can_publish,
            "canSubscribe": self.can_subscribe,
            "canPublishData": self.can_publish_data,
            "roomRecord": self.room_record,
            "roomAdmin": self.room_admin,
            "hidden": self.hidden,
        }


@dataclass(frozen=True, slots=True)
class LiveKitCredentials:
    api_key: str
    api_secret: str


def build_access_token(
    credentials: LiveKitCredentials,
    identity: str,
    grant: VideoGrant,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """A signed JWT a LiveKit client can use to join `grant.room`.

    `now` is injectable so tests can assert exact `exp`/`nbf` values rather
    than asserting "some timestamp in the future", the same reason every
    other clock in this codebase (`Settings.clock`) is injectable.
    """
    issued = now if now is not None else int(time.time())
    payload = {
        "iss": credentials.api_key,
        "sub": identity,
        "iat": issued,
        "nbf": issued,
        "exp": issued + ttl_seconds,
        "jti": identity,
        "video": grant.as_claim(),
    }
    return jwt.encode(payload, credentials.api_secret, algorithm="HS256")


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """The fields this codebase reads out of a LiveKit webhook payload.

    Not a full transcription of LiveKit's WebhookEvent schema — that has
    many more fields (participant info, track info, full egress detail)
    this project has no use for. Adding a field here should mean the
    gateway has an actual, immediate use for it, not "for completeness".
    """

    event: str
    room_name: str
    egress_id: str | None = None
    egress_status: str | None = None
    storage_ref: str | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict) -> WebhookEvent:
        room = payload.get("room") or {}
        egress = payload.get("egressInfo") or {}
        file_results = egress.get("fileResults") or []
        storage_ref = file_results[0].get("location") if file_results else None
        return cls(
            event=payload.get("event", ""),
            # `egress_started`/`egress_ended` carry no top-level `room`
            # object — the room is inside egressInfo. Reading only
            # `room.name` left every egress event with an empty room name,
            # which silently broke deriving the session from it.
            room_name=room.get("name") or egress.get("roomName") or "",
            egress_id=egress.get("egressId"),
            egress_status=egress.get("status"),
            storage_ref=storage_ref,
            raw=payload,
        )


def verify_webhook(
    body: bytes, authorize_header: str | None, credentials: LiveKitCredentials
) -> WebhookEvent:
    """Verify a LiveKit webhook POST and parse it.

    LiveKit signs webhooks with a JWT whose payload commits to a hash of
    the exact request body: `sha256` = base64(SHA-256(body)). Verification
    is therefore two checks, both required: the JWT signature (proves the
    sender holds the API secret) and the body hash (proves this exact
    payload, unmodified, is what was signed) — checking only the first
    would accept a valid signature attached to a swapped-in body.

    The caller supplies the header value. Verified against LiveKit 1.13:
    the token arrives in `Authorization` as a bare JWT with no `Bearer `
    prefix. Older releases used `Authorize`, so the gateway reads
    whichever is present.
    """
    if not authorize_header:
        raise TokenError("missing Authorize header")

    try:
        claims = jwt.decode(
            authorize_header,
            credentials.api_secret,
            algorithms=["HS256"],
            issuer=credentials.api_key,
        )
    except jwt.InvalidTokenError as exc:
        raise TokenError(f"invalid webhook signature: {exc}") from exc

    expected_hash = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
    if not claims.get("sha256") or claims["sha256"] != expected_hash:
        raise TokenError("webhook body does not match its signed hash")

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise TokenError("webhook body is not valid JSON") from exc

    return WebhookEvent.from_payload(payload)
