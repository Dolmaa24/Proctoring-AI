"""Postgres implementation of `Store`.

Why this exists alongside `SqliteStore` rather than replacing it
-----------------------------------------------------------------
SQLite is single-writer. That is entirely adequate for one gateway
process on one machine, which is what the SQLite store was written for
and still the right default for local development and the test suite —
neither should need a running database container.

It stops being adequate the moment there is more than one gateway
process, which is also the point at which an institution wants the data
somewhere a person can query it with ordinary tools. Both stores
implement the same narrow `Store` protocol, so which one is in use is a
connection-string decision and no request handler knows the difference.

The SQL is deliberately written out again rather than shared with the
SQLite store through a dialect abstraction. The two diverge in enough
places — placeholders, upsert syntax, autoincrement, boolean handling,
window-function support — that a shared layer would be mostly branches,
and a subtly wrong branch in a store that holds the audit trail is worse
than duplication a reader can check line by line.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .store import SessionRecord

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
    created_ms         BIGINT NOT NULL,
    last_seq           BIGINT NOT NULL,
    last_monotonic_ms  BIGINT NOT NULL,
    first_client_ms    BIGINT,
    first_server_ms    BIGINT,
    events_received    BIGINT NOT NULL,
    ended_cleanly      BOOLEAN NOT NULL,
    attested_build     TEXT,
    integrity_breaches JSONB NOT NULL,
    breach_counts      JSONB NOT NULL,
    signal_counts      JSONB NOT NULL,
    updated_ms         BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS violations (
    id            BIGSERIAL PRIMARY KEY,
    session_id    TEXT NOT NULL,
    violation_id  TEXT NOT NULL,
    rule_id       TEXT NOT NULL,
    severity      TEXT NOT NULL,
    message       TEXT NOT NULL,
    opened_at_ms  BIGINT NOT NULL,
    fired_at_ms   BIGINT NOT NULL,
    duration_ms   BIGINT NOT NULL,
    resolved      BOOLEAN NOT NULL,
    recorded_ms   BIGINT NOT NULL,
    evidence      JSONB NOT NULL
);

-- Face templates, on their own retention clock. See the SQLite schema and
-- purge_templates_older_than for why this is a separate table from the
-- scores derived from it.
CREATE TABLE IF NOT EXISTS identity_templates (
    session_id   TEXT PRIMARY KEY,
    embedder     TEXT NOT NULL,
    dimensions   INTEGER NOT NULL,
    reference    JSONB NOT NULL,
    captures     INTEGER NOT NULL,
    min_pairwise DOUBLE PRECISION NOT NULL,
    created_ms   BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS identity_checks (
    id            BIGSERIAL PRIMARY KEY,
    session_id    TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    similarity    DOUBLE PRECISION,
    threshold     DOUBLE PRECISION NOT NULL,
    calibrated_on TEXT NOT NULL,
    issues        JSONB NOT NULL,
    recorded_ms   BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS audio_transcripts (
    transcript_id TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    transcript    TEXT NOT NULL,
    transcriber   TEXT NOT NULL,
    duration_ms   BIGINT NOT NULL,
    recorded_ms   BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS audio_checks (
    id            BIGSERIAL PRIMARY KEY,
    session_id    TEXT NOT NULL,
    transcript_id TEXT NOT NULL,
    label         TEXT NOT NULL,
    confidence    DOUBLE PRECISION NOT NULL,
    classifier    TEXT NOT NULL,
    recorded_ms   BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS recordings (
    recording_id   TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    status         TEXT NOT NULL,
    requested_ms   BIGINT NOT NULL,
    started_ms     BIGINT,
    stopped_ms     BIGINT,
    egress_id      TEXT,
    storage_ref    TEXT,
    failure_reason TEXT,
    updated_ms     BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS violations_by_session ON violations (session_id, recorded_ms);
CREATE INDEX IF NOT EXISTS violations_by_age ON violations (recorded_ms);
CREATE INDEX IF NOT EXISTS sessions_by_age ON sessions (updated_ms);
CREATE INDEX IF NOT EXISTS templates_by_age ON identity_templates (created_ms);
CREATE INDEX IF NOT EXISTS checks_by_session ON identity_checks (session_id, recorded_ms);
CREATE INDEX IF NOT EXISTS checks_by_age ON identity_checks (recorded_ms);
CREATE INDEX IF NOT EXISTS transcripts_by_age ON audio_transcripts (recorded_ms);
CREATE INDEX IF NOT EXISTS audio_checks_by_session ON audio_checks (session_id, recorded_ms);
CREATE INDEX IF NOT EXISTS audio_checks_by_age ON audio_checks (recorded_ms);
CREATE INDEX IF NOT EXISTS recordings_by_session ON recordings (session_id, requested_ms);
CREATE INDEX IF NOT EXISTS recordings_by_age ON recordings (updated_ms);
CREATE INDEX IF NOT EXISTS recordings_by_egress ON recordings (egress_id);
"""


def _violation_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
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
        # JSONB comes back already decoded, unlike SQLite's TEXT columns.
        "evidence": row["evidence"],
    }


def _recording_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
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


class PostgresStore:
    """Durable store backed by Postgres, over a small connection pool.

    Pooled because handlers run on the event loop thread while the tick
    loop and shutdown hooks do not, and a single shared connection would
    serialise every write behind whichever coroutine held it.
    """

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8) -> None:
        self.dsn = dsn
        self._pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        self._pool.wait(timeout=30)
        with self._pool.connection() as conn:
            conn.execute(_SCHEMA)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', %s) "
                "ON CONFLICT (key) DO NOTHING",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()

    # -- sessions ----------------------------------------------------------

    def load_sessions(self) -> list[SessionRecord]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT * FROM sessions").fetchall()
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
                integrity_breaches=row["integrity_breaches"],
                attested_build=row["attested_build"],
                breach_counts=row["breach_counts"],
                signal_counts=row["signal_counts"],
            )
            for row in rows
        ]

    def save_session(self, record: SessionRecord, now_ms: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, exam_id, candidate_ref, created_ms, last_seq,
                    last_monotonic_ms, first_client_ms, first_server_ms,
                    events_received, ended_cleanly, attested_build,
                    integrity_breaches, breach_counts, signal_counts, updated_ms
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (session_id) DO UPDATE SET
                    last_seq           = EXCLUDED.last_seq,
                    last_monotonic_ms  = EXCLUDED.last_monotonic_ms,
                    first_client_ms    = EXCLUDED.first_client_ms,
                    first_server_ms    = EXCLUDED.first_server_ms,
                    events_received    = EXCLUDED.events_received,
                    ended_cleanly      = EXCLUDED.ended_cleanly,
                    attested_build     = EXCLUDED.attested_build,
                    integrity_breaches = EXCLUDED.integrity_breaches,
                    breach_counts      = EXCLUDED.breach_counts,
                    signal_counts      = EXCLUDED.signal_counts,
                    updated_ms         = EXCLUDED.updated_ms
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
                    record.ended_cleanly,
                    record.attested_build,
                    json.dumps(list(record.integrity_breaches)),
                    json.dumps(record.breach_counts),
                    json.dumps(record.signal_counts),
                    now_ms,
                ),
            )
            conn.commit()

    # -- violations --------------------------------------------------------

    def append_violation(self, violation: dict[str, Any], now_ms: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO violations (
                    session_id, violation_id, rule_id, severity, message,
                    opened_at_ms, fired_at_ms, duration_ms, resolved,
                    recorded_ms, evidence
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                    bool(violation.get("resolved")),
                    now_ms,
                    json.dumps(violation.get("evidence", [])),
                ),
            )
            conn.commit()

    def load_violations(self, per_session: int) -> dict[str, list[dict[str, Any]]]:
        """Most recent `per_session` violations per session, oldest first.

        Oldest first because the caller replays them into the triage board
        in order, and the board's timeline and score both depend on
        arrival order.
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY session_id ORDER BY recorded_ms DESC, id DESC
                    ) AS rank FROM violations
                ) ranked WHERE rank <= %s
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
        carries no evidence. Taking the first match returns the row a
        reviewer actually wants: the measurements that caused the flag.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM violations WHERE violation_id = %s ORDER BY id ASC LIMIT 1",
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
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO identity_templates (
                    session_id, embedder, dimensions, reference, captures,
                    min_pairwise, created_ms
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (session_id) DO UPDATE SET
                    embedder     = EXCLUDED.embedder,
                    dimensions   = EXCLUDED.dimensions,
                    reference    = EXCLUDED.reference,
                    captures     = EXCLUDED.captures,
                    min_pairwise = EXCLUDED.min_pairwise,
                    created_ms   = EXCLUDED.created_ms
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
            conn.commit()

    def load_templates(self) -> dict[str, dict[str, Any]]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT * FROM identity_templates").fetchall()
        return {
            row["session_id"]: {
                "embedder": row["embedder"],
                "reference": tuple(row["reference"]),
                "captures": row["captures"],
                "min_pairwise": row["min_pairwise"],
            }
            for row in rows
        }

    def append_identity_check(self, session_id: str, result: dict[str, Any], now_ms: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO identity_checks (
                    session_id, outcome, similarity, threshold, calibrated_on,
                    issues, recorded_ms
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
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
            conn.commit()

    def load_identity_checks(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM identity_checks WHERE session_id = %s
                ORDER BY recorded_ms DESC, id DESC LIMIT %s
                """,
                (session_id, limit),
            ).fetchall()
        return [
            {
                "outcome": row["outcome"],
                "similarity": row["similarity"],
                "threshold": row["threshold"],
                "calibrated_on": row["calibrated_on"],
                "issues": row["issues"],
                "recorded_ms": row["recorded_ms"],
            }
            for row in rows
        ]

    def purge_templates_older_than(self, cutoff_ms: int) -> int:
        """Expire face templates on their own, shorter clock.

        The similarity scores derived from them survive on the longer
        retention clock, so the audit trail stays intact after the
        biometric itself is gone.
        """
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM identity_templates WHERE created_ms < %s", (cutoff_ms,)
            )
            conn.commit()
        return max(0, cursor.rowcount)

    # -- audio -------------------------------------------------------------

    def save_transcript(
        self,
        transcript_id: str,
        session_id: str,
        transcript: str,
        transcriber: str,
        duration_ms: int,
        now_ms: int,
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO audio_transcripts (
                    transcript_id, session_id, transcript, transcriber,
                    duration_ms, recorded_ms
                ) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (transcript_id) DO NOTHING
                """,
                (transcript_id, session_id, transcript, transcriber, duration_ms, now_ms),
            )
            conn.commit()

    def load_transcript(self, transcript_id: str) -> dict[str, Any] | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM audio_transcripts WHERE transcript_id = %s", (transcript_id,)
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
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO audio_checks (
                    session_id, transcript_id, label, confidence, classifier,
                    recorded_ms
                ) VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (session_id, transcript_id, label, confidence, classifier, now_ms),
            )
            conn.commit()

    def load_audio_checks(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM audio_checks WHERE session_id = %s
                ORDER BY recorded_ms DESC, id DESC LIMIT %s
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
        """Expire transcripts on their own, shorter clock — see save_transcript."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM audio_transcripts WHERE recorded_ms < %s", (cutoff_ms,)
            )
            conn.commit()
        return max(0, cursor.rowcount)

    # -- recordings --------------------------------------------------------

    def save_recording(self, record: dict[str, Any], now_ms: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO recordings (
                    recording_id, session_id, status, requested_ms, started_ms,
                    stopped_ms, egress_id, storage_ref, failure_reason, updated_ms
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (recording_id) DO UPDATE SET
                    status         = EXCLUDED.status,
                    started_ms     = EXCLUDED.started_ms,
                    stopped_ms     = EXCLUDED.stopped_ms,
                    egress_id      = EXCLUDED.egress_id,
                    storage_ref    = EXCLUDED.storage_ref,
                    failure_reason = EXCLUDED.failure_reason,
                    updated_ms     = EXCLUDED.updated_ms
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
            conn.commit()

    def load_recording(self, recording_id: str) -> dict[str, Any] | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM recordings WHERE recording_id = %s", (recording_id,)
            ).fetchone()
        return _recording_row_to_dict(row) if row is not None else None

    def load_recordings_for_session(self, session_id: str) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM recordings WHERE session_id = %s ORDER BY requested_ms ASC",
                (session_id,),
            ).fetchall()
        return [_recording_row_to_dict(row) for row in rows]

    def find_recording_by_egress_id(self, egress_id: str) -> dict[str, Any] | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM recordings WHERE egress_id = %s", (egress_id,)
            ).fetchone()
        return _recording_row_to_dict(row) if row is not None else None

    def purge_recordings_older_than(self, cutoff_ms: int) -> int:
        """Expire recording *references* on their own clock.

        Deletes our metadata row — the egress id, the storage reference,
        the lifecycle timestamps. It does **not** delete the actual
        recording from the operator's object storage; that is their
        storage lifecycle policy's job. Treating this purge as equivalent
        to deleting the video would be a false sense of compliance.
        """
        with self._pool.connection() as conn:
            cursor = conn.execute("DELETE FROM recordings WHERE updated_ms < %s", (cutoff_ms,))
            conn.commit()
        return max(0, cursor.rowcount)

    # -- retention ---------------------------------------------------------

    def purge_older_than(self, cutoff_ms: int) -> tuple[int, int]:
        """Delete data older than the cutoff. Returns (violations, sessions).

        Retention is not housekeeping. Every row here is an observation
        about an identifiable person derived from their face; keeping it
        past the point it is needed for review is the difference between a
        proctoring system and a biometric archive.
        """
        with self._pool.connection() as conn:
            violations = conn.execute(
                "DELETE FROM violations WHERE recorded_ms < %s", (cutoff_ms,)
            ).rowcount
            conn.execute("DELETE FROM identity_checks WHERE recorded_ms < %s", (cutoff_ms,))
            conn.execute("DELETE FROM audio_checks WHERE recorded_ms < %s", (cutoff_ms,))
            sessions = conn.execute(
                "DELETE FROM sessions WHERE updated_ms < %s", (cutoff_ms,)
            ).rowcount
            conn.commit()
        return (max(0, violations), max(0, sessions))

    def close(self) -> None:
        self._pool.close()


def connect(dsn: str) -> PostgresStore:
    """Open a Postgres store, surfacing a connection failure clearly.

    A gateway that silently fell back to in-memory storage when the
    database was unreachable would lose the replay-protection counters and
    the audit trail without anyone noticing, so this raises instead.
    """
    try:
        return PostgresStore(dsn)
    except psycopg.Error as exc:
        raise RuntimeError(
            f"could not open the Postgres store at the configured DSN: {exc}"
        ) from exc
