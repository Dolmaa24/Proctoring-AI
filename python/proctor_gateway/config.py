"""Gateway configuration."""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field


def wall_clock_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class Settings:
    master_secret: bytes
    """Root secret for per-session key derivation. Never leaves the server."""

    clock: Callable[[], int] = field(default=wall_clock_ms)
    """Server clock, injectable.

    Exists so the test suite can drive a 15-minute exam through the real
    gateway in milliseconds. Without it, any test of a sustained violation
    would have to sleep for the duration it is testing, which in practice
    means those tests do not get written.
    """

    tick_interval_ms: int = 1_000
    """How often absence-of-telemetry rules are evaluated."""

    clock_skew_tolerance_ms: int = 2_000
    """Permitted drift between client-reported and server-observed elapsed time.

    Generous, because laptops suspend, throttle background timers, and lose
    scheduling slices under load. This is looking for a clock being *moved*,
    not for imprecision.
    """

    max_sequence_gap: int = 0
    """Tolerated gap in the telemetry sequence. Zero: every event must arrive.

    The transport is an ordered, reliable WebSocket, so a gap is not packet
    loss. It means events were dropped before transmission, which is exactly
    the cheapest attack a hostile client has.
    """

    policy_path: str | None = None

    db_path: str = ":memory:"
    """Where durable state lives. `:memory:` disables persistence entirely.

    Defaults to ephemeral so constructing `Settings` directly — as the test
    suite does — never touches the filesystem. `from_env` defaults to a real
    file, because a gateway that forgets `last_seq` on restart lets a client
    replay its whole earlier stream.
    """

    retention_days: int = 30
    """How long violations and finished sessions are kept.

    Evidence rows are observations about identifiable people derived from
    their faces. Keeping them past the point they are needed for review
    turns a proctoring system into a biometric archive, so retention is a
    first-class setting rather than a cleanup script someone might run.
    Institutions with a shorter statutory limit should lower it.
    """

    console_token: str = ""
    """Bearer token required to read the proctor stream.

    The proctor endpoints carry every candidate's flags and evidence —
    biometric-derived data about identifiable people. Unauthenticated, the
    port is a live feed of who is being accused of what.

    When unset, `from_env` generates a random token and logs it rather than
    leaving the endpoint open. Failing closed with a printed dev token
    keeps local work friction-free without ever producing a deployment that
    is accidentally public.
    """

    @classmethod
    def from_env(cls) -> Settings:
        raw = os.environ.get("PROCTOR_MASTER_SECRET")
        if raw:
            master = raw.encode("utf-8")
        else:
            # Ephemeral secret so `make dev` works with no setup. Sessions do
            # not survive a restart, which is correct for development and
            # loudly wrong for production — hence the warning in main().
            master = secrets.token_bytes(32)
        return cls(
            master_secret=master,
            policy_path=os.environ.get("PROCTOR_POLICY_PATH"),
            console_token=os.environ.get("PROCTOR_CONSOLE_TOKEN") or secrets.token_urlsafe(24),
            db_path=os.environ.get("PROCTOR_DB_PATH", "proctor.db"),
            retention_days=int(os.environ.get("PROCTOR_RETENTION_DAYS", "30")),
        )

    @property
    def has_persistent_secret(self) -> bool:
        return bool(os.environ.get("PROCTOR_MASTER_SECRET"))

    @property
    def has_configured_console_token(self) -> bool:
        return bool(os.environ.get("PROCTOR_CONSOLE_TOKEN"))
