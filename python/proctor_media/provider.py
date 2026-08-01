"""Room and recording provider: the seam between this codebase and an SFU.

`RoomProvider` is the only thing the gateway depends on. `LiveKitRoomProvider`
is a real implementation; `FakeRoomProvider` is a deterministic test double.
Swapping providers should never require touching `proctor_gateway.app`.

A confidence note, stated once here rather than repeated per method
-------------------------------------------------------------------
Token issuance and webhook verification (`tokens.py`) rest on parts of
LiveKit's API that have been stable, documented, and used by essentially
every LiveKit SDK for years — reasonably solid ground to implement without
a live server to test against. The Egress recording RPC below is on
shakier ground: it is a Twirp-JSON RPC surface that has grown and shifted
field names across LiveKit versions in ways the access-token format has
not. `start_recording`/`stop_recording` are implemented as a best-effort
against the documented shape, isolated in their own methods specifically
so they are easy to find and correct against your deployed server version
without touching anything else. Test this against a real deployment
before trusting it; the fake provider is what this repository's own test
suite relies on instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from .tokens import LiveKitCredentials, VideoGrant, build_access_token


class RoomProviderError(Exception):
    """The SFU (or the request to it) could not do what was asked."""


@dataclass(frozen=True, slots=True)
class StartedRecording:
    egress_id: str


class RoomProvider(Protocol):
    def candidate_token(self, session_id: str, identity: str) -> str: ...
    def proctor_token(self, session_id: str, identity: str) -> str: ...
    def start_recording(self, session_id: str) -> StartedRecording: ...
    def stop_recording(self, session_id: str, egress_id: str) -> None: ...


def _room_name(session_id: str) -> str:
    """One room per session. Not configurable: a shared or guessable room
    name would let one candidate's client join another's room, and the
    room name is the only thing standing between the two once a token is
    scoped to `roomJoin` for it."""
    return f"proctor-{session_id}"


def _http_url(url: str) -> str:
    """The HTTP origin for LiveKit's Twirp API, from its WebSocket URL.

    `livekit_url` is configured as the URL *clients* connect to, which is
    a WebSocket one (`ws://` / `wss://`). The Egress RPC is ordinary
    HTTP POST on the same host and port, and pointing httpx at a `ws://`
    URL does not work — it was a 404 against a real server, which reads
    like a wrong path rather than a wrong scheme and is worth naming here
    so the next person does not go hunting through Twirp route names.
    """
    if url.startswith("wss://"):
        return "https://" + url[len("wss://") :]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://") :]
    return url


def _raise_for_twirp(response: httpx.Response, what: str) -> None:
    """Fail with the server's actual complaint, not just the status code.

    Twirp puts a `{"code": ..., "msg": ...}` body on every error, and
    `raise_for_status()` throws all of it away. A bare "400 Bad Request"
    from an RPC with a dozen required-in-combination fields is close to
    undebuggable; the message names the field.
    """
    if response.is_success:
        return
    detail = response.text.strip()
    try:
        payload = response.json()
        detail = payload.get("msg") or payload.get("message") or detail
    except ValueError:
        pass
    raise RoomProviderError(f"could not {what}: {response.status_code} {detail}")


DEFAULT_FILEPATH_TEMPLATE = "/out/{room_name}-{time}"
"""Where egress writes the file, in the *egress service's* filesystem.

`{room_name}` and `{time}` are LiveKit's own placeholders, expanded by
egress rather than here. The default matches the volume the compose stack
mounts at `./data/recordings`, so a recording lands somewhere a human can
open it — which is the point of recording it at all.

Egress also decides the container from the extension; leaving it off, as
here, gets the default MP4.
"""


class LiveKitRoomProvider:
    def __init__(
        self,
        url: str,
        api_key: str,
        api_secret: str,
        filepath_template: str = DEFAULT_FILEPATH_TEMPLATE,
    ) -> None:
        self.url = url.rstrip("/")
        self.api_url = _http_url(self.url)
        self.filepath_template = filepath_template
        self.credentials = LiveKitCredentials(api_key=api_key, api_secret=api_secret)

    def candidate_token(self, session_id: str, identity: str) -> str:
        return build_access_token(
            self.credentials, identity, VideoGrant.publisher(_room_name(session_id))
        )

    def proctor_token(self, session_id: str, identity: str) -> str:
        return build_access_token(
            self.credentials, identity, VideoGrant.subscriber(_room_name(session_id))
        )

    def start_recording(self, session_id: str) -> StartedRecording:
        """Start a room-composite recording.

        `file_outputs` is required, not optional: an egress request with no
        output destination is rejected with a bare 400, which reads like a
        malformed request rather than a missing field.

        Room *composite* rather than per-track: a reviewer wants one file
        showing the session, not a track file per participant to
        reassemble afterwards.
        """
        admin_grant = VideoGrant(room=_room_name(session_id), room_join=False, room_record=True)
        token = build_access_token(self.credentials, "proctor-gateway", admin_grant)
        try:
            response = httpx.post(
                f"{self.api_url}/twirp/livekit.Egress/StartRoomCompositeEgress",
                json={
                    "room_name": _room_name(session_id),
                    "file_outputs": [{"filepath": self.filepath_template}],
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
            _raise_for_twirp(response, "start recording")
            body = response.json()
        except httpx.HTTPError as exc:
            raise RoomProviderError(f"could not start recording: {exc}") from exc

        egress_id = body.get("egress_id") or body.get("egressId")
        if not egress_id:
            raise RoomProviderError(f"egress response had no egress id: {body}")
        return StartedRecording(egress_id=egress_id)

    def stop_recording(self, session_id: str, egress_id: str) -> None:
        admin_grant = VideoGrant(room=_room_name(session_id), room_join=False, room_record=True)
        token = build_access_token(self.credentials, "proctor-gateway", admin_grant)
        try:
            response = httpx.post(
                f"{self.api_url}/twirp/livekit.Egress/StopEgress",
                json={"egress_id": egress_id},
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
            _raise_for_twirp(response, "stop recording")
        except httpx.HTTPError as exc:
            raise RoomProviderError(f"could not stop recording: {exc}") from exc


class FakeRoomProvider:
    """Deterministic test double. No network call, no real server.

    Unlike `DeterministicEmbedder`/`DeterministicTranscriber`, the honest
    reason for this double is not licensing or dependency weight — it is
    that there is no LiveKit server available in this environment to test
    against at all. Tokens are still built with the real `build_access_token`
    (so token *shape* is genuinely exercised); only the network-calling
    Egress methods are faked.
    """

    def __init__(self, credentials: LiveKitCredentials | None = None) -> None:
        self.credentials = credentials or LiveKitCredentials(
            "fake-key", "fake-secret-at-least-32-bytes-long-for-hs256"
        )
        self.started: list[str] = []
        self.stopped: list[tuple[str, str]] = []
        self._next_id = 0

    def candidate_token(self, session_id: str, identity: str) -> str:
        return build_access_token(
            self.credentials, identity, VideoGrant.publisher(_room_name(session_id))
        )

    def proctor_token(self, session_id: str, identity: str) -> str:
        return build_access_token(
            self.credentials, identity, VideoGrant.subscriber(_room_name(session_id))
        )

    def start_recording(self, session_id: str) -> StartedRecording:
        self._next_id += 1
        egress_id = f"fake-egress-{self._next_id}"
        self.started.append(session_id)
        return StartedRecording(egress_id=egress_id)

    def stop_recording(self, session_id: str, egress_id: str) -> None:
        self.stopped.append((session_id, egress_id))
