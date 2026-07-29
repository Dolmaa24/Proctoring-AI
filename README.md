# Proctoring AI

Edge-to-cloud automated exam proctoring. Computer vision runs on the
candidate's machine in real time; the backend holds policy, state, identity
and evidence. Raw video never touches the telemetry path.

Rebuilt from scratch. The original OpenCV desktop scripts are preserved on
the `upstream/master` remote for reference.

> **Not production ready.** No authentication on the proctor stream, no
> persistence, no identity verification yet. See ARCHITECTURE.md § 8.

---

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

Run the tests — no webcam, no models, no candidate required:

```bash
.venv/bin/python -m pytest python/tests -q
```

Start the gateway:

```bash
PROCTOR_MASTER_SECRET=dev-secret .venv/bin/uvicorn proctor_gateway.app:app --reload
```

Drive a simulated candidate against it:

```bash
.venv/bin/python -m proctor_sim --list
```

```bash
.venv/bin/python -m proctor_sim --scenario phone
```

```bash
.venv/bin/python -m proctor_sim --scenario look_away --tamper drop_events
```

Watch what a proctor would see:

```bash
websocat ws://localhost:8000/v1/proctor/stream
```

---

## What exists

| Component | State |
|---|---|
| `proctor_protocol` | Event schema, HMAC signing, per-session key derivation |
| `proctor_fusion` | Temporal filtering, declarative policy, no I/O |
| `proctor_gateway` | FastAPI ingest, integrity checks, proctor fan-out |
| `proctor_sim` | Scripted candidates + six client-tampering modes |
| `policies/default.yaml` | 16 rules, all flag-only |
| `apps/client` | Electron main process, signed transport, gaze geometry |

The client's OS observation, transport and gaze/head-pose maths are built
and tested. **It has not yet been run end-to-end against a live camera** —
the MediaPipe model assets are not vendored, so `npm start` will not work
until they are. Everything that can be tested without a webcam is.

Not yet built: SFU and recording, identity verification, audio pipeline,
proctor console, persistence.

---

## Two ideas worth knowing before reading the code

**The edge emits observations; the server decides violations.** The client
reports `yaw_deg=-42.0, confidence=0.88`, never "this candidate cheated".
Policy is server-side so it can be retuned without reshipping a binary, and
so a tampered client cannot suppress a verdict it never computed.

**Every rule is flag-only, and a test enforces it.** Nothing here ends an
exam on its own. The models have measured accuracy disparities across skin
tones and lighting; the heuristics penalise candidates with tics, ADHD, or a
habit of reading aloud. The first tests in the suite assert that innocent
behaviour produces *nothing* — a housemate walking past, a single spurious
detection, muttering while thinking, a bad webcam.

---

## Testing

```bash
make test
```

Python (61) and TypeScript (23). The suite has three parts, and the last
two are the interesting ones:

- **Behavioural** — does policy do the right thing for honest and dishonest
  candidates, including *not* flagging the honest ones.
- **Adversarial** — does the transport notice a hostile client. Forged
  signatures, replayed frames, dropped windows, stalled sequences, rewound
  clocks, quitting the app, and going silent mid-exam.
- **Conformance** — the real TypeScript signing code, verified by the real
  Python verifier, across every payload type. This is the only thing that
  would catch the two halves of the protocol drifting apart.

Scenarios are pure data (`proctor_sim/scenarios.py`), so a 15-second exam
runs in milliseconds with an injected clock. The client's geometry and
process matching are pure functions for the same reason — no webcam, no
Electron, no volunteer.

---

## Before building on this

- **Do not use Ultralytics YOLO** without an enterprise licence — it is
  AGPL-3.0 and that reaches network services. Use RF-DETR, YOLOX, or a
  self-trained detector.
- Face embeddings are biometric data under GDPR Art. 9, Illinois BIPA and
  Texas CUBI. Consent and deletion paths are product requirements.
- Read ARCHITECTURE.md § 3 before relying on any security property. It says
  plainly what each defence does not buy you, including a documented,
  unclosed gap in clock-stretch detection.

## Licence

MIT.
