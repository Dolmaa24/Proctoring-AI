"""Identity verification through the real gateway.

Uses the deterministic test embedder, so what is under test is this
project's own code — enrolment, gating, temporal decision, storage,
retention and the flag path — rather than someone else's model.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from proctor_gateway import Settings, create_app

MASTER = b"identity-master-secret"
TOKEN = "identity-console-token"
PROTOCOLS = ["proctor.console.v1", f"token.{TOKEN}"]
DAY_MS = 86_400_000


class FakeClock:
    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self.now = start_ms

    def __call__(self) -> int:
        return self.now


def settings(clock: FakeClock, **overrides) -> Settings:
    base = dict(
        master_secret=MASTER,
        clock=clock,
        tick_interval_ms=50,
        console_token=TOKEN,
        identity_threshold=0.9,
        identity_calibrated_on="test fixture, deterministic embedder",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(clock: FakeClock) -> TestClient:
    return TestClient(create_app(settings(clock)))


def enrol_session(client: TestClient) -> str:
    response = client.post("/v1/sessions", json={"exam_id": "exam-1", "candidate_ref": "cand-1"})
    return response.json()["session_id"]


def capture(seed: str) -> str:
    return base64.b64encode(seed.encode()).decode()


def enrol(client: TestClient, session_id: str, seed: str = "alice"):
    return client.post(
        f"/v1/sessions/{session_id}/identity/enrol",
        json={"captures": [capture(seed)] * 3},
    )


def probe(client: TestClient, session_id: str, seed: str | None, **quality):
    body = {"image_b64": capture(seed) if seed else None}
    body.update(quality)
    return client.post(f"/v1/sessions/{session_id}/identity/probe", json=body)


# -- configuration gate -----------------------------------------------------


def test_identity_is_disabled_without_a_threshold(clock):
    """Fails closed. No cutoff is correct across populations, so there is
    no default that could be applied on the operator's behalf."""
    app = create_app(settings(clock, identity_threshold=None))
    with TestClient(app) as client:
        session_id = enrol_session(client)
        assert enrol(client, session_id).status_code == 503


def test_identity_is_disabled_without_a_calibration_record(clock):
    """A threshold with no stated population is not a specification."""
    app = create_app(settings(clock, identity_calibrated_on=""))
    with TestClient(app) as client:
        session_id = enrol_session(client)
        assert enrol(client, session_id).status_code == 503


# -- enrolment --------------------------------------------------------------


def test_enrolment_succeeds_with_consistent_captures(client):
    session_id = enrol_session(client)
    response = enrol(client, session_id)
    assert response.status_code == 201
    body = response.json()
    assert body["captures"] == 3
    assert body["min_pairwise_similarity"] == pytest.approx(1.0)


def test_enrolment_rejects_inconsistent_captures_with_a_usable_message(client):
    session_id = enrol_session(client)
    response = client.post(
        f"/v1/sessions/{session_id}/identity/enrol",
        json={"captures": [capture("alice"), capture("bob"), capture("carol")]},
    )
    assert response.status_code == 422
    # The candidate has to be able to act on this, not just be refused.
    assert "re-capture" in response.json()["detail"].lower()


def test_enrolment_requires_several_captures(client):
    session_id = enrol_session(client)
    response = client.post(
        f"/v1/sessions/{session_id}/identity/enrol",
        json={"captures": [capture("alice")]},
    )
    assert response.status_code == 422


def test_enrolment_on_an_unknown_session_is_404(client):
    assert enrol(client, "sess-nope").status_code == 404


def test_probing_before_enrolment_is_a_conflict_not_a_mismatch(client):
    session_id = enrol_session(client)
    assert probe(client, session_id, "alice").status_code == 409


# -- probing ----------------------------------------------------------------


def test_the_same_face_matches(client):
    session_id = enrol_session(client)
    enrol(client, session_id)
    body = probe(client, session_id, "alice").json()
    assert body["result"]["outcome"] == "match"
    assert body["result"]["similarity"] == pytest.approx(1.0)


def test_a_different_face_is_a_mismatch_and_carries_its_threshold(client):
    session_id = enrol_session(client)
    enrol(client, session_id)
    result = probe(client, session_id, "mallory").json()["result"]
    assert result["outcome"] == "mismatch"
    assert result["threshold"] == 0.9
    assert result["calibrated_on"], "a reviewer must see what cutoff was applied"


@pytest.mark.parametrize(
    "quality",
    [
        {"face_count": 0},
        {"detector_confidence": 0.1},
        {"yaw_deg": 70.0},
        {"face_fraction": 0.01},
    ],
    ids=["no-face", "dark", "turned-away", "too-far"],
)
def test_an_unusable_capture_is_never_a_mismatch(client, quality):
    """Being hard to photograph is not evidence of impersonation."""
    session_id = enrol_session(client)
    enrol(client, session_id)
    result = probe(client, session_id, "mallory", **quality).json()["result"]

    assert result["outcome"] == "not_assessable"
    assert result["similarity"] is None, "no number may be recorded for an unusable frame"


def test_a_single_mismatch_raises_no_flag(client):
    session_id = enrol_session(client)
    enrol(client, session_id)
    assert probe(client, session_id, "mallory").json()["finding"] is None


# -- the flag path ----------------------------------------------------------


def collect_violations(client: TestClient, run) -> list[dict]:
    violations: list[dict] = []
    with client.websocket_connect("/v1/proctor/stream", subprotocols=PROTOCOLS) as ws:
        ws.receive_json()
        run()
        for _ in range(200):
            try:
                message = ws.receive_json(mode="text")
            except Exception:
                break
            if message.get("kind") == "violation":
                violations.append(message)
            if len(violations) >= 1:
                break
    return violations


def test_sustained_mismatch_reaches_the_proctor_as_a_reviewable_flag(client):
    session_id = enrol_session(client)
    enrol(client, session_id)

    def run():
        for _ in range(4):
            probe(client, session_id, "mallory")

    violations = collect_violations(client, run)
    assert violations, "a sustained mismatch must reach the console"
    flag = violations[0]
    assert flag["rule_id"] == "identity_mismatch"
    assert flag["severity"] == "hard"


def test_identity_never_acts_on_its_own(client):
    """The most important assertion about this subsystem.

    Alleging that someone else sat an exam is the most serious claim the
    platform can make, and it is made by a measurement with known, large
    accuracy differences across demographic groups. It flags; a person
    decides.
    """
    session_id = enrol_session(client)
    enrol(client, session_id)

    def run():
        for _ in range(4):
            probe(client, session_id, "mallory")

    flag = collect_violations(client, run)[0]
    assert flag["action"] == "flag"
    assert flag["requires_human_review"] is True


def test_the_flag_carries_the_similarities_and_its_own_caveat(client):
    session_id = enrol_session(client)
    enrol(client, session_id)

    def run():
        for _ in range(4):
            probe(client, session_id, "mallory")

    flag = collect_violations(client, run)[0]
    payload = flag["evidence"][0]["payload"]
    assert payload["similarities"], "the numbers are the evidence"
    assert payload["threshold"] == 0.9
    assert "demographic" in flag["message"], (
        "the caveat must travel with the finding, not sit in a policy document"
    )


# -- audit trail and retention ----------------------------------------------


def test_identity_checks_are_recorded_for_review(clock, tmp_path):
    app = create_app(settings(clock, db_path=str(tmp_path / "id.db")))
    with TestClient(app) as client:
        session_id = enrol_session(client)
        enrol(client, session_id)
        probe(client, session_id, "alice")
        probe(client, session_id, "mallory")

        history = client.get(
            f"/v1/proctor/sessions/{session_id}/identity",
            headers={"authorization": f"Bearer {TOKEN}"},
        ).json()

    outcomes = [c["outcome"] for c in history["checks"]]
    assert "match" in outcomes and "mismatch" in outcomes


def test_identity_history_requires_the_console_token(client):
    session_id = enrol_session(client)
    assert client.get(f"/v1/proctor/sessions/{session_id}/identity").status_code == 401


def test_templates_expire_before_the_scores_derived_from_them(clock, tmp_path):
    """The privacy split that makes this design defensible.

    A face template has no review value once the exam is over — a human
    compares recordings, not vectors — but it is the highest-risk row in
    the database. The similarity scores outlive it, so the audit trail
    survives the biometric being deleted.
    """
    db = str(tmp_path / "retain.db")
    app = create_app(settings(clock, db_path=db, template_retention_days=1))
    with TestClient(app) as client:
        session_id = enrol_session(client)
        enrol(client, session_id)
        probe(client, session_id, "alice")

    clock.now += 3 * DAY_MS
    later = create_app(settings(clock, db_path=db, template_retention_days=1))
    with TestClient(later) as client:
        store = later.state.store
        assert store.load_templates() == {}, "the template must be gone"

        history = client.get(
            f"/v1/proctor/sessions/{session_id}/identity",
            headers={"authorization": f"Bearer {TOKEN}"},
        ).json()
        assert history["checks"], "the reviewable scores must survive"


def test_enrolment_survives_a_restart(clock, tmp_path):
    db = str(tmp_path / "restart.db")
    with TestClient(create_app(settings(clock, db_path=db))) as first:
        session_id = enrol_session(first)
        enrol(first, session_id)

    with TestClient(create_app(settings(clock, db_path=db))) as second:
        # No re-enrolment: the probe must be comparable immediately.
        assert probe(second, session_id, "alice").json()["result"]["outcome"] == "match"


def test_templates_from_a_different_model_are_not_reused(clock, tmp_path):
    """Comparing vectors across models yields a meaningless similarity —
    and a meaningless similarity that crosses a threshold is an accusation."""
    db = str(tmp_path / "model.db")
    first_app = create_app(settings(clock, db_path=db))
    with TestClient(first_app) as first:
        session_id = enrol_session(first)
        enrol(first, session_id)
        # Overwrite the stored template as if it came from another model.
        first_app.state.store.save_template(
            session_id, "onnx:some-other-model:deadbeef", (1.0, 0.0), 3, 1.0, clock.now
        )

    with TestClient(create_app(settings(clock, db_path=db))) as second:
        assert probe(second, session_id, "alice").status_code == 409, (
            "a template from another model must force re-enrolment"
        )


# -- input handling ---------------------------------------------------------


def test_malformed_capture_is_rejected(client):
    session_id = enrol_session(client)
    response = client.post(
        f"/v1/sessions/{session_id}/identity/enrol",
        json={"captures": ["not!valid!base64", "also!bad", "still!bad"]},
    )
    assert response.status_code == 400


def test_oversized_capture_is_rejected(client):
    session_id = enrol_session(client)
    huge = base64.b64encode(b"x" * (5 * 1024 * 1024)).decode()
    response = client.post(
        f"/v1/sessions/{session_id}/identity/enrol", json={"captures": [huge] * 3}
    )
    assert response.status_code == 400
