"""SFU integration: room tokens, recording lifecycle, webhook verification.

This is the integration *layer*, not a media server. Building a WebRTC
media engine (ICE/DTLS/SRTP, congestion control) from scratch would be
irresponsible in scope this large a piece of security-critical
infrastructure deserves its own project, and it is not what real
deployments do either — they run an existing SFU. This package assumes a
self-hosted LiveKit deployment (Apache-2.0) and implements the parts this
platform needs on top of it: scoped, short-lived access tokens; verified
webhook ingestion; and a recording lifecycle that tracks a *reference* to
where a recording ended up, never the bytes themselves.

See `provider.py`'s module docstring for which parts of this are
implemented against a stable, long-documented LiveKit surface (tokens,
webhooks) versus a best-effort against a more version-sensitive one
(the Egress recording RPC).
"""

from .provider import (
    FakeRoomProvider,
    LiveKitRoomProvider,
    RoomProvider,
    RoomProviderError,
    StartedRecording,
)
from .recording import InvalidTransition, RecordingRecord, RecordingStatus
from .tokens import (
    DEFAULT_TTL_SECONDS,
    LiveKitCredentials,
    TokenError,
    VideoGrant,
    WebhookEvent,
    build_access_token,
    verify_webhook,
)

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "FakeRoomProvider",
    "InvalidTransition",
    "LiveKitCredentials",
    "LiveKitRoomProvider",
    "RecordingRecord",
    "RecordingStatus",
    "RoomProvider",
    "RoomProviderError",
    "StartedRecording",
    "TokenError",
    "VideoGrant",
    "WebhookEvent",
    "build_access_token",
    "verify_webhook",
]
