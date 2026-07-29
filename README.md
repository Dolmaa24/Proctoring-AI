# Proctoring AI

Edge-to-cloud automated exam proctoring. Computer vision runs on the
candidate's machine in real time; the backend holds policy, state, identity
and evidence. Raw video never touches the telemetry path.

Rebuilt from scratch. The original OpenCV desktop scripts are preserved on
the `upstream/master` remote for reference.

> **Not production ready.** No persistence, no identity verification, no
> session recording. See ARCHITECTURE.md § 8 for the full list, and § 3.2
> for a documented, unclosed gap in clock-stretch detection.

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
PROCTOR_MASTER_SECRET=dev-secret PROCTOR_CONSOLE_TOKEN=dev-token .venv/bin/uvicorn proctor_gateway.app:app --reload
```

The proctor console is then at <http://localhost:8000/console>. It asks
for the token above; leave `PROCTOR_CONSOLE_TOKEN` unset and the gateway
generates one and logs it at startup. The proctor endpoints **fail
closed** — there is no unauthenticated mode, because they carry every
candidate's flags.

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



---

## What exists

| Component | State |
|---|---|
| `proctor_protocol` | Event schema, HMAC signing, per-session key derivation |
| `proctor_fusion` | Temporal filtering, declarative policy, no I/O |
| `proctor_gateway` | FastAPI ingest, integrity checks, proctor fan-out |
| `proctor_sim` | Scripted candidates + six client-tampering modes |
| `policies/default.yaml` | 16 rules, all flag-only |
| `apps/client` | Electron client: MediaPipe inference, signed transport, OS observation |
| `apps/console` | Proctor console: triage queue, per-session audit timeline |

The full chain has been run end-to-end — camera → MediaPipe → IPC → signed
WebSocket → gateway → fusion → proctor stream — against a synthetic camera
feed, in both the face-present and no-face cases.

Not yet built: SFU and recording, identity verification, audio pipeline,
persistence.

## Running the client

```bash
cd apps/client && npm install && npm run vendor
```

`npm run vendor` copies the MediaPipe WASM out of `node_modules` and
downloads the face landmarker model (3.8 MB, from Google's official host),
recording SHA-256 digests in `models/manifest.json`. The assets are
bundled rather than fetched at exam time: the client must work on a
locked-down network, and a model downloaded during an exam is a model
nobody has attested.

End-to-end, with no webcam and no volunteer:

```bash
npm run fixture && npm run e2e
```

That draws a synthetic face into a Y4M video, feeds it to Chromium's fake
capture device, and asserts the whole chain. `npm run e2e:absent` runs the
same check with a feed containing no face, which should raise
`candidate_absent` and nothing else.

The fixture is drawn procedurally rather than being a photograph. A
proctoring repo should not carry a scraped image of an identifiable person
as a test fixture, and it does not need to.

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

Python (83) and TypeScript (29). The suite has three parts, and the last
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
