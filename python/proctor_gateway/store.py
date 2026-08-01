"""Durable state: sessions, violations, and the audit trail.

Persistence here is not only about convenience. Two of the three things
this module stores are load-bearing for correctness or for people:

**`last_seq` and `last_monotonic_ms` are a security property.** Replay
protection works by refusing a sequence number already seen. If those
counters live only in memory, restarting the gateway resets them to -1 and
a hostile client can re-send its entire earlier stream — every "face
present, looking at the screen" frame it has already used — and nothing
notices. A restart must not be a way to launder a replay.

**Violations are the audit trail.** They are what a human reviews before
deciding anything about a candidate. Losing them on a restart means a flag
was raised against someone and the evidence for it is gone, which is worse
than never having flagged at all.

**Evidence is personal data.** Landmark-derived observations about an
identifiable person, at rest on disk, indefinitely, is a liability under
GDPR Art. 9 and BIPA-style statutes. Hence `retention_days` and the purge
on startup — a proctoring system that silently accumulates biometric
evidence forever is a compliance incident waiting to be discovered.

SQLite because it is stdlib, single-file, ACID and entirely adequate at
this scale. The `Store` interface is deliberately narrow so that swapping
in Postgres for horizontal scale does not touch the request handlers.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id         TEXT PRIMARY KEY,
    exam_id            TEXT NOT NULL,
    candidate_ref      TEXT NOT NULL,
    created_ms         INTEGER NOT NULL,
    last_seq           INTEGER NOT NULL,
    last_monotonic_ms  INTEGER NOT NULL,
    first_client_ms    INTEGER,
    first_server_ms    INTEGER,
    events_received    INTEGER NOT NULL,
    ended_cleanly      INTEGER NOT NULL,
    attested_build     TEXT,
    integrity_breaches TEXT NOT NULL,
    breach_counts      TEXT NOT NULL,
    signal_counts      TEXT NOT NULL,
    updated_ms         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS violations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    violation_id  TEXT NOT NULL,
    rule_id       TEXT NOT NULL,
    severity      TEXT NOT NULL,
    message       TEXT NOT NULL,
    opened_at_ms  INTEGER NOT NULL,
    fired_at_ms   INTEGER NOT NULL,
    duration_ms   INTEGER NOT NULL,
    resolved      INTEGER NOT NULL,
    recorded_ms   INTEGER NOT NULL,
    evidence      TEXT NOT NULL
);

-- Face templates. Deliberately a separate table from the scores derived
-- from them, so the two can be expired on different clocks: an embedding
-- is a biometric template with no review value once the exam is over,
-- while "similarity was 0.41 at 10:32" is exactly what a reviewer needs
-- and carries far less risk if it leaks.
CREATE TABLE IF NOT EXISTS identity_templates (
    session_id  TEXT PRIMARY KEY,
    embedder    TEXT NOT NULL,
    dimensions  INTEGER NOT NULL,
    reference   TEXT NOT NULL,
    captures    INTEGER NOT NULL,
    min_pairwise REAL NOT NULL,
    created_ms  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS identity_checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    similarity    REAL,
    threshold     REAL NOT NULL,
    calibrated_on TEXT NOT NULL,
    issues        TEXT NOT NULL,
    recorded_ms   INTEGER NOT NULL
);

-- Audio: the same privacy split as identity, adapted. A transcript is far
-- more revealing than a face-match float — it is the actual content of
-- what was said, which can include third parties, health information, or
-- anything else spoken near an open microphone — so it lives on its own
-- short clock. The classification label and confidence carry the audit
-- value and survive on the ordinary long clock.
CREATE TABLE IF NOT EXISTS audio_transcripts (
    transcript_id TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    transcript    TEXT NOT NULL,
    transcriber   TEXT NOT NULL,
    duration_ms   INTEGER NOT NULL,
    recorded_ms   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audio_checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    transcript_id TEXT NOT NULL,
    label         TEXT NOT NULL,
    confidence    REAL NOT NULL,
    classifier    TEXT NOT NULL,
    recorded_ms   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS transcripts_by_age
    ON audio_transcripts (recorded_ms);
CREATE INDEX IF NOT EXISTS audio_checks_by_session
    ON audio_checks (session_id, recorded_ms);
CREATE INDEX IF NOT EXISTS audio_checks_by_age
    ON audio_checks (recorded_ms);
-- Recordings. Tracks a *reference* to where a recording ended up (an
-- egress id, a storage URI), never the video bytes — the same principle
-- as an identity template or an audio transcript, applied to the single
-- most sensitive artifact this platform touches. Purging a row here
-- removes our reference to the recording; it does not delete the object
-- from wherever the operator's storage actually lives (S3, GCS, ...) —
-- see purge_recordings_older_than.
CREATE TABLE IF NOT EXISTS recordings (
    recording_id    TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    status          TEXT NOT NULL,
    requested_ms    INTEGER NOT NULL,
    started_ms      INTEGER,
    stopped_ms      INTEGER,
    egress_id       TEXT,
    storage_ref     TEXT,
    failure_reason  TEXT,
    updated_ms      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS recordings_by_session
    ON recordings (session_id, requested_ms);
CREATE INDEX IF NOT EXISTS recordings_by_age
    ON recordings (updated_ms);
CREATE INDEX IF NOT EXISTS recordings_by_egress
    ON recordings (egress_id);
CREATE INDEX IF NOT EXISTS templates_by_age
    ON identity_templates (created_ms);
CREATE INDEX IF NOT EXISTS checks_by_session
    ON identity_checks (session_id, recorded_ms);
CREATE INDEX IF NOT EXISTS checks_by_age
    ON identity_checks (recorded_ms);
CREATE INDEX IF NOT EXISTS violations_by_session
    ON violations (session_id, recorded_ms);
CREATE INDEX IF NOT EXISTS violations_by_age
    ON violations (recorded_ms);
CREATE INDEX IF NOT EXISTS sessions_by_age
    ON sessions (updated_ms);
"""


@dataclass(slots=True)
class SessionRecord:
    """A session as it was last written. `connected` is deliberately absent.

    Nothing is connected immediately after a restart, so restoring a stored
    `connected=True` would show a proctor a live candidate who is not there
    and would suppress the silence rule for a session with no client.
    """

    session_id: str
    exam_id: str
    candidate_ref: str
    created_ms: int
    last_seq: int
    last_monotonic_ms: int
    first_client_ms: int | None
    first_server_ms: int | None
    events_received: int
    ended_cleanly: bool
    attested_build: str | None
    integrity_breaches: list[str]
    breach_counts: dict[str, int]
    signal_counts: dict[str, int]


class Store(Protocol):
    def load_sessions(self) -> list[SessionRecord]: ...
    def save_session(self, record: SessionRecord, now_ms: int) -> None: ...
    def append_violation(self, violation: dict[str, Any], now_ms: int) -> None: ...
    def load_violations(self, per_session: int) -> dict[str, list[dict[str, Any]]]: ...
    def load_violation(self, violation_id: str) -> dict[str, Any] | None: ...
    def purge_older_than(self, cutoff_ms: int) -> tuple[int, int]: ...
    def save_template(
        self,
        session_id: str,
        embedder: str,
        reference: tuple[float, ...],
        captures: int,
        min_pairwise: float,
        now_ms: int,
    ) -> None: ...
    def load_templates(self) -> dict[str, dict[str, Any]]: ...
    def append_identity_check(
        self, session_id: str, result: dict[str, Any], now_ms: int
    ) -> None: ...
    def load_identity_checks(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]: ...
    def purge_templates_older_than(self, cutoff_ms: int) -> int: ...
    def save_transcript(
        self,
        transcript_id: str,
        session_id: str,
        transcript: str,
        transcriber: str,
        duration_ms: int,
        now_ms: int,
    ) -> None: ...
    def load_transcript(self, transcript_id: str) -> dict[str, Any] | None: ...
    def append_audio_check(
        self,
        session_id: str,
        transcript_id: str,
        label: str,
        confidence: float,
        classifier: str,
        now_ms: int,
    ) -> None: ...
    def load_audio_checks(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]: ...
    def purge_transcripts_older_than(self, cutoff_ms: int) -> int: ...
    def save_recording(self, record: dict[str, Any], now_ms: int) -> None: ...
    def load_recording(self, recording_id: str) -> dict[str, Any] | None: ...
    def load_recordings_for_session(self, session_id: str) -> list[dict[str, Any]]: ...
    def find_recording_by_egress_id(self, egress_id: str) -> dict[str, Any] | None: ...
    def purge_recordings_older_than(self, cutoff_ms: int) -> int: ...
    def close(self) -> None: ...


class MemoryStore:
    """No-op store. Keeps the ephemeral path a real implementation.

    Used by the test suite and by anyone running without a database, so
    that "no persistence" is a store choice rather than a branch threaded
    through the gateway.
    """

    def load_sessions(self) -> list[SessionRecord]:
        return []

    def save_session(self, record: SessionRecord, now_ms: int) -> None:
        return None

    def append_violation(self, violation: dict[str, Any], now_ms: int) -> None:
        return None

    def load_violations(self, per_session: int) -> dict[str, list[dict[str, Any]]]:
        return {}

    def load_violation(self, violation_id: str) -> dict[str, Any] | None:
        return None

    def purge_older_than(self, cutoff_ms: int) -> tuple[int, int]:
        return (0, 0)

    def save_template(
        self,
        session_id: str,
        embedder: str,
        reference: tuple[float, ...],
        captures: int,
        min_pairwise: float,
        now_ms: int,
    ) -> None:
        return None

    def load_templates(self) -> dict[str, dict[str, Any]]:
        return {}

    def append_identity_check(self, session_id: str, result: dict[str, Any], now_ms: int) -> None:
        return None

    def load_identity_checks(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return []

    def purge_templates_older_than(self, cutoff_ms: int) -> int:
        return 0

    def save_transcript(
        self,
        transcript_id: str,
        session_id: str,
        transcript: str,
        transcriber: str,
        duration_ms: int,
        now_ms: int,
    ) -> None:
        return None

    def load_transcript(self, transcript_id: str) -> dict[str, Any] | None:
        return None

    def append_audio_check(
        self,
        session_id: str,
        transcript_id: str,
        label: str,
        confidence: float,
        classifier: str,
        now_ms: int,
    ) -> None:
        return None

    def load_audio_checks(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return []

    def purge_transcripts_older_than(self, cutoff_ms: int) -> int:
        return 0

    def save_recording(self, record: dict[str, Any], now_ms: int) -> None:
        return None

    def load_recording(self, recording_id: str) -> dict[str, Any] | None:
        return None

    def load_recordings_for_session(self, session_id: str) -> list[dict[str, Any]]:
        return []

    def find_recording_by_egress_id(self, egress_id: str) -> dict[str, Any] | None:
        return None

    def purge_recordings_older_than(self, cutoff_ms: int) -> int:
        return 0

    def close(self) -> None:
        return None


def _violation_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Shared between `load_violations` (bulk, per session) and
    `load_violation` (single, by id) so the two cannot drift in shape —
    the console's evidence view depends on both returning the same fields.
    """
    return {
        "session_id": row["session_id"],
        "violation_id": row["violation_id"],
        "rule_id": row["rule_id"],
        "severity": row["severity"],
        "message": row["message"],
        "opened_at_ms": row["opened_at_ms"],
        "fired_at_ms": row["fired_at_ms"],
        "duration_ms": row["duration_ms"],
        "resolved": bool(row["resolved"]),
        "recorded_ms": row["recorded_ms"],
        "evidence": json.loads(row["evidence"]),
    }


def _recording_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "recording_id": row["recording_id"],
        "session_id": row["session_id"],
        "status": row["status"],
        "requested_ms": row["requested_ms"],
        "started_ms": row["started_ms"],
        "stopped_ms": row["stopped_ms"],
        "egress_id": row["egress_id"],
        "storage_ref": row["storage_ref"],
        "failure_reason": row["failure_reason"],
    }


class SqliteStore:
    """Single-file durable store.

    Runs in WAL mode with `synchronous=NORMAL`. That trades a small window
    on power loss (the last few commits) for write throughput high enough
    to checkpoint session state on every telemetry event. The alternative,
    `synchronous=FULL`, fsyncs per commit and would make the ingest path
    disk-bound. The residual exposure is bounded and stated in the module
    docstring; losing the last second of sequence state to a power cut is
    acceptable, losing all of it to an ordinary restart is not.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        # check_same_thread=False plus an explicit lock: handlers run on the
        # event loop thread but the tick loop and shutdown hooks may not.
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_SCHEMA)
            self._db.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._db.commit()

    # -- sessions ----------------------------------------------------------

    def load_sessions(self) -> list[SessionRecord]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM sessions").fetchall()
        return [
            SessionRecord(
                session_id=row["session_id"],
                exam_id=row["exam_id"],
                candidate_ref=row["candidate_ref"],
                created_ms=row["created_ms"],
                last_seq=row["last_seq"],
                last_monotonic_ms=row["last_monotonic_ms"],
                first_client_ms=row["first_client_ms"],
                first_server_ms=row["first_server_ms"],
                events_received=row["events_received"],
                ended_cleanly=bool(row["ended_cleanly"]),
                attested_build=row["attested_build"],
                integrity_breaches=json.loads(row["integrity_breaches"]),
                breach_counts=json.loads(row["breach_counts"]),
                signal_counts=json.loads(row["signal_counts"]),
            )
            for row in rows
        ]

    def save_session(self, record: SessionRecord, now_ms: int) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO sessions (
                    session_id, exam_id, candidate_ref, created_ms, last_seq,
                    last_monotonic_ms, first_client_ms, first_server_ms,
                    events_received, ended_cleanly, attested_build,
                    integrity_breaches, breach_counts, signal_counts, updated_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                    last_seq          = excluded.last_seq,
                    last_monotonic_ms = excluded.last_monotonic_ms,
                    first_client_ms   = excluded.first_client_ms,
                    first_server_ms   = excluded.first_server_ms,
                    events_received   = excluded.events_received,
                    ended_cleanly     = excluded.ended_cleanly,
                    attested_build    = excluded.attested_build,
                    integrity_breaches= excluded.integrity_breaches,
                    breach_counts     = excluded.breach_counts,
                    signal_counts     = excluded.signal_counts,
                    updated_ms        = excluded.updated_ms
                """,
                (
                    record.session_id,
                    record.exam_id,
                    record.candidate_ref,
                    record.created_ms,
                    record.last_seq,
                    record.last_monotonic_ms,
                    record.first_client_ms,
                    record.first_server_ms,
                    record.events_received,
                    int(record.ended_cleanly),
                    record.attested_build,
                    json.dumps(record.integrity_breaches),
                    json.dumps(record.breach_counts),
                    json.dumps(record.signal_counts),
                    now_ms,
                ),
            )
            self._db.commit()

    # -- violations --------------------------------------------------------

    def append_violation(self, violation: dict[str, Any], now_ms: int) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO violations (
                    session_id, violation_id, rule_id, severity, message,
                    opened_at_ms, fired_at_ms, duration_ms, resolved,
                    recorded_ms, evidence
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    violation["session_id"],
                    violation["violation_id"],
                    violation["rule_id"],
                    violation["severity"],
                    violation["message"],
                    violation.get("opened_at_ms", 0),
                    violation.get("fired_at_ms", 0),
                    violation.get("duration_ms", 0),
                    int(bool(violation.get("resolved"))),
                    now_ms,
                    json.dumps(violation.get("evidence", [])),
                ),
            )
            self._db.commit()

    def load_violations(self, per_session: int) -> dict[str, list[dict[str, Any]]]:
        """Most recent `per_session` violations per session, oldest first.

        Oldest first because the caller replays them into the triage board
        in order, and the board's timeline and score both depend on
        arrival order.
        """
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY session_id ORDER BY recorded_ms DESC, id DESC
                    ) AS rank FROM violations
                ) WHERE rank <= ?
                ORDER BY recorded_ms ASC, id ASC
                """,
                (per_session,),
            ).fetchall()

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["session_id"], []).append(_violation_row_to_dict(row))
        return grouped

    def load_violation(self, violation_id: str) -> dict[str, Any] | None:
        """The earliest stored row for one violation_id — the firing event.

        A violation's id is reused for its later resolution row, which
        carries no evidence (`evidence=()` in the fusion engine). Ordering
        by `id ASC` and taking the first match returns the row a reviewer
        actually wants: the measurements that caused the flag to fire.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM violations WHERE violation_id = ? ORDER BY id ASC LIMIT 1",
                (violation_id,),
            ).fetchone()
        return _violation_row_to_dict(row) if row is not None else None

    # -- identity ----------------------------------------------------------

    def save_template(
        self,
        session_id: str,
        embedder: str,
        reference: tuple[float, ...],
        captures: int,
        min_pairwise: float,
        now_ms: int,
    ) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO identity_templates (
                    session_id, embedder, dimensions, reference, captures,
                    min_pairwise, created_ms
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                    embedder     = excluded.embedder,
                    dimensions   = excluded.dimensions,
                    reference    = excluded.reference,
                    captures     = excluded.captures,
                    min_pairwise = excluded.min_pairwise,
                    created_ms   = excluded.created_ms
                """,
                (
                    session_id,
                    embedder,
                    len(reference),
                    json.dumps(list(reference)),
                    captures,
                    min_pairwise,
                    now_ms,
                ),
            )
            self._db.commit()

    def load_templates(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM identity_templates").fetchall()
        return {
            row["session_id"]: {
                "embedder": row["embedder"],
                "reference": tuple(json.loads(row["reference"])),
                "captures": row["captures"],
                "min_pairwise": row["min_pairwise"],
            }
            for row in rows
        }

    def append_identity_check(self, session_id: str, result: dict[str, Any], now_ms: int) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO identity_checks (
                    session_id, outcome, similarity, threshold, calibrated_on,
                    issues, recorded_ms
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    session_id,
                    result["outcome"],
                    result["similarity"],
                    result["threshold"],
                    result["calibrated_on"],
                    json.dumps(result.get("issues", [])),
                    now_ms,
                ),
            )
            self._db.commit()

    def load_identity_checks(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM identity_checks WHERE session_id = ?
                ORDER BY recorded_ms DESC, id DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [
            {
                "outcome": row["outcome"],
                "similarity": row["similarity"],
                "threshold": row["threshold"],
                "calibrated_on": row["calibrated_on"],
                "issues": json.loads(row["issues"]),
                "recorded_ms": row["recorded_ms"],
            }
            for row in rows
        ]

    def purge_templates_older_than(self, cutoff_ms: int) -> int:
        """Expire face templates on their own, shorter clock.

        A template has no review value once the exam is over — a reviewer
        compares recordings, not vectors — but it is the highest-risk row
        in the database. The similarity scores derived from it survive on
        the longer retention clock, so the audit trail stays intact after
        the biometric itself is gone.
        """
        with self._lock:
            removed = self._db.execute(
                "DELETE FROM identity_templates WHERE created_ms < ?", (cutoff_ms,)
            ).rowcount
            self._db.commit()
        return max(0, removed)

    # -- audio ---------------------------------------------------------

    def save_transcript(
        self,
        transcript_id: str,
        session_id: str,
        transcript: str,
        transcriber: str,
        duration_ms: int,
        now_ms: int,
    ) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO audio_transcripts (
                    transcript_id, session_id, transcript, transcriber,
                    duration_ms, recorded_ms
                ) VALUES (?,?,?,?,?,?)
                """,
                (transcript_id, session_id, transcript, transcriber, duration_ms, now_ms),
            )
            self._db.commit()

    def load_transcript(self, transcript_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM audio_transcripts WHERE transcript_id = ?", (transcript_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "transcript_id": row["transcript_id"],
            "session_id": row["session_id"],
            "transcript": row["transcript"],
            "transcriber": row["transcriber"],
            "duration_ms": row["duration_ms"],
            "recorded_ms": row["recorded_ms"],
        }

    def append_audio_check(
        self,
        session_id: str,
        transcript_id: str,
        label: str,
        confidence: float,
        classifier: str,
        now_ms: int,
    ) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO audio_checks (
                    session_id, transcript_id, label, confidence, classifier,
                    recorded_ms
                ) VALUES (?,?,?,?,?,?)
                """,
                (session_id, transcript_id, label, confidence, classifier, now_ms),
            )
            self._db.commit()

    def load_audio_checks(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT * FROM audio_checks WHERE session_id = ?
                ORDER BY recorded_ms DESC, id DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [
            {
                "transcript_id": row["transcript_id"],
                "label": row["label"],
                "confidence": row["confidence"],
                "classifier": row["classifier"],
                "recorded_ms": row["recorded_ms"],
            }
            for row in rows
        ]

    def purge_transcripts_older_than(self, cutoff_ms: int) -> int:
        """Expire transcripts on their own, shorter clock — see `save_transcript`."""
        with self._lock:
            removed = self._db.execute(
                "DELETE FROM audio_transcripts WHERE recorded_ms < ?", (cutoff_ms,)
            ).rowcount
            self._db.commit()
        return max(0, removed)

    # -- recordings ----------------------------------------------------------

    def save_recording(self, record: dict[str, Any], now_ms: int) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO recordings (
                    recording_id, session_id, status, requested_ms, started_ms,
                    stopped_ms, egress_id, storage_ref, failure_reason, updated_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(recording_id) DO UPDATE SET
                    status         = excluded.status,
                    started_ms     = excluded.started_ms,
                    stopped_ms     = excluded.stopped_ms,
                    egress_id      = excluded.egress_id,
                    storage_ref    = excluded.storage_ref,
                    failure_reason = excluded.failure_reason,
                    updated_ms     = excluded.updated_ms
                """,
                (
                    record["recording_id"],
                    record["session_id"],
                    record["status"],
                    record["requested_ms"],
                    record.get("started_ms"),
                    record.get("stopped_ms"),
                    record.get("egress_id"),
                    record.get("storage_ref"),
                    record.get("failure_reason"),
                    now_ms,
                ),
            )
            self._db.commit()

    def load_recording(self, recording_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM recordings WHERE recording_id = ?", (recording_id,)
            ).fetchone()
        return _recording_row_to_dict(row) if row is not None else None

    def load_recordings_for_session(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM recordings WHERE session_id = ? ORDER BY requested_ms ASC",
                (session_id,),
            ).fetchall()
        return [_recording_row_to_dict(row) for row in rows]

    def find_recording_by_egress_id(self, egress_id: str) -> dict[str, Any] | None:
        """Webhook deliveries key on `egressId`, not `session_id` or
        `recording_id` — this is the one lookup path in the whole module
        that needs a different key, hence its own indexed query rather
        than a scan over `load_recordings_for_session`.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM recordings WHERE egress_id = ?", (egress_id,)
            ).fetchone()
        return _recording_row_to_dict(row) if row is not None else None

    def purge_recordings_older_than(self, cutoff_ms: int) -> int:
        """Expire recording *references* on their own clock.

        This deletes our metadata row — the egress id, the storage
        reference, the lifecycle timestamps. It does **not** delete the
        actual recording from wherever the operator's storage lives (S3,
        GCS, ...); that is the operator's storage lifecycle policy's job,
        the same boundary `LiveKitRoomProvider` draws around not handling
        object storage credentials itself. Treating this purge as
        equivalent to deleting the video would be a false sense of
        compliance.
        """
        with self._lock:
            removed = self._db.execute(
                "DELETE FROM recordings WHERE updated_ms < ?", (cutoff_ms,)
            ).rowcount
            self._db.commit()
        return max(0, removed)

    # -- retention ---------------------------------------------------------

    def purge_older_than(self, cutoff_ms: int) -> tuple[int, int]:
        """Delete data older than the cutoff. Returns (violations, sessions).

        Retention is not housekeeping. Every row here is an observation
        about an identifiable person derived from their face; keeping it
        past the point it is needed for review is the difference between a
        proctoring system and a biometric archive.
        """
        with self._lock:
            violations = self._db.execute(
                "DELETE FROM violations WHERE recorded_ms < ?", (cutoff_ms,)
            ).rowcount
            self._db.execute("DELETE FROM identity_checks WHERE recorded_ms < ?", (cutoff_ms,))
            self._db.execute("DELETE FROM audio_checks WHERE recorded_ms < ?", (cutoff_ms,))
            sessions = self._db.execute(
                "DELETE FROM sessions WHERE updated_ms < ?", (cutoff_ms,)
            ).rowcount
            self._db.commit()
        return (max(0, violations), max(0, sessions))

    def close(self) -> None:
        with self._lock:
            self._db.commit()
            self._db.close()


def open_store(path: str) -> Store:
    """Select a backend from the connection string.

    - `:memory:`                 -> `MemoryStore`, no persistence at all
    - `postgres://` / `postgresql://` -> `PostgresStore`
    - anything else              -> `SqliteStore` at that file path

    Postgres is imported lazily so that the ordinary SQLite and in-memory
    paths — which are what the test suite and local development use — do
    not require the driver to be installed or a database to be reachable.
    """
    if path == ":memory:":
        return MemoryStore()
    if path.startswith(("postgres://", "postgresql://")):
        from .postgres_store import connect

        return connect(path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return SqliteStore(path)
