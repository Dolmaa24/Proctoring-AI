# Proctoring AI

Edge-to-cloud automated exam proctoring. Computer vision runs on the
candidate's machine in real time; the backend holds policy, state, identity
and evidence. Raw video never touches the telemetry path.

Rebuilt from scratch. The original OpenCV desktop scripts are preserved on
the `upstream/master` remote for reference.

> **Not production ready.** See ARCHITECTURE.md § 8 for the full list of
> what is and is not built, and § 3.2 for a documented, unclosed gap in
> clock-stretch detection. Note also that `wearable_detected` in the
> shipped policy is inert by design — the bundled COCO-80 detector has no
> smartwatch or headphones class — and is kept, and tested as inert, so
> the gap stays visible rather than silently disappearing.

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

The proctor console is then at <http://localhost:8000/console>.

### Getting the console token

The console asks for a token before it will show you anything. The proctor
endpoints **fail closed** — there is no unauthenticated mode, because they
carry every candidate's flags and evidence. You do not have to create the
token anywhere; there are two ways to get one.

**Set it yourself.** Whatever you put in `PROCTOR_CONSOLE_TOKEN` *is* the
token — it is a shared secret you choose, not something registered with a
service:

```bash
PROCTOR_MASTER_SECRET=dev-secret PROCTOR_CONSOLE_TOKEN=dev-token .venv/bin/uvicorn proctor_gateway.app:app --reload
```

Then type `dev-token` into the console prompt.

**Or let the gateway generate one.** Leave `PROCTOR_CONSOLE_TOKEN` unset
and it makes a random one for that run and prints it at startup:

```
WARNING  PROCTOR_CONSOLE_TOKEN is unset; generated a token for this run only:
             kJ3n8-QpX7vT2mR9wLzY4dFhB6sN1cVe
             console: http://localhost:8000/console
```

Copy that value into the prompt. It changes on every restart, so set it
explicitly for anything you come back to.

Under Docker the same warning goes to the container log:

```bash
docker compose logs gateway | grep -A2 "PROCTOR_CONSOLE_TOKEN is unset"
```

Two things worth knowing: the token is stored in your browser's local
storage once entered, so you are not retyping it every refresh; and it
grants access to every candidate's flags, so treat it like any other
credential rather than something to paste into a shared document.

### Running the whole stack in Docker

Postgres, a database browser, a real self-hosted LiveKit + egress (so
recording actually works), and the gateway:

```bash
cp .env.example .env
```

Fill in the four required secrets — the file says which and how to
generate them — then:

```bash
docker compose up -d
```

| | | |
|---|---|---|
| Gateway + console | <http://localhost:8000/console> | token from `.env` |
| Adminer (browse the database) | <http://localhost:8080> | server `postgres`, user/password from `.env` |
| LiveKit | `ws://localhost:7880` | |
| Recordings | `./data/recordings/` | MP4 per session, plus a JSON sidecar |

**Reading sessions by hand.** Adminer is the point-and-click route.
Everything is equally reachable with `psql`, since Postgres is published
on a host port:

```bash
docker compose exec postgres psql -U proctor -d proctor -c "SELECT session_id, exam_id, events_received FROM sessions;"
```

The tables worth knowing: `sessions` (one row per exam sitting, including
the replay-protection counters), `violations` (the audit trail, with
evidence as JSONB), `recordings` (a *reference* to each video — never the
video itself), and `identity_templates` / `audio_transcripts`, which are
on deliberately shorter retention clocks than the rest.

**Recording lifecycle.** A recording is not started when the candidate
consents — at that moment they have not joined the media room yet, so
there is nothing to record. The gateway starts it from LiveKit's
`room_started` webhook and follows it through `requested → active →
available`, writing the file path into `recordings.storage_ref` when
egress reports the file is written. Purging a row removes that reference;
the MP4 under `./data/recordings/` is yours to manage, and no retention
setting in this project deletes it.

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
| `policies/default.yaml` | 18 rules, all flag-only |
| `apps/client` | Electron client: consent gate, MediaPipe inference, object/quality detection, exam lockdown, signed transport, OS observation, LiveKit publish |
| `apps/console` | Proctor console: triage queue, per-session audit timeline |
| `proctor_gateway.store` | SQLite **or** Postgres persistence + retention |
| `proctor_identity` | Enrolment, face matching, temporal decision |
| `proctor_audio` | Transcription, intent classification, temporal escalation |
| `proctor_media` | LiveKit tokens, webhook verification, recording lifecycle |

The full chain has been run end-to-end — camera → MediaPipe → IPC → signed
WebSocket → gateway → fusion → proctor stream — against a synthetic camera
feed, in both the face-present and no-face cases. The media plane has been
run against the real compose stack too: consent → room join → publish →
`room_started` webhook → egress → a playable MP4 on disk with its path
recorded in Postgres.

### What the candidate sees

The session opens with a **disclaimer that names everything captured** —
camera, microphone, recording and its retention window, process and
display checks, and the lockdown rules. Nothing starts until they accept:
the camera is not opened and no model is loaded before that, because a
disclaimer shown while the webcam light is already on is notification
rather than consent.

On accept the window goes fullscreen and monitoring begins. Detections
raise a **notice in the top-right corner** — "A phone is visible", "More
than one face is visible", "Your camera looks blurred" — worded to let the
candidate fix the problem rather than to accuse them. A toast is not proof
a flag was raised: the server applies confirmation windows the client does
not.

Leaving fullscreen, copy/paste, developer tools and right-click are
blocked, with **three warnings** and a visible count before further
attempts are recorded for review. ESC and Cmd-C are muscle memory, and a
system that escalates on the first press produces flags reviewers learn to
dismiss.

State survives a restart — sessions, the triage board, and the audit
trail. Sequence state in particular *must* survive: without it a restart
would let a client replay its earlier stream unchallenged.

```bash
PROCTOR_DB_PATH=proctor.db PROCTOR_RETENTION_DAYS=30
```

Evidence rows are observations about identifiable people derived from
their faces, so retention is a setting rather than a cleanup script:
data past the window is purged at startup. `:memory:` disables
persistence entirely.

Identity verification is built but **off by default**. It stays disabled
until a threshold *and* a record of what population it was calibrated
against are both supplied:

```bash
PROCTOR_IDENTITY_THRESHOLD=0.55 PROCTOR_IDENTITY_CALIBRATED_ON="internal, 2026-06"
```

There is no default cutoff because there is no cutoff that is correct
across populations — face-match error rates vary by one to two orders of
magnitude between demographic groups. **No face model is bundled**: the
commonly available ArcFace weights are non-commercial-research-only.
Point `PROCTOR_FACE_MODEL` at your own licensed ONNX export.

The audio pipeline is built but **off by default**, gated on a different
requirement than identity: not measurement accuracy, but *consent* —
recording and transcribing a candidate's voice carries wiretap and
all-party-consent exposure a webcam frame comparison does not.

```bash
PROCTOR_AUDIO_CONSENT_NOTICE="candidates shown recording notice X at exam start"
```

That alone enables transcription: chunks are transcribed and stored for a
human to read, with no automated judgement on top. A second, independent
switch adds intent classification — sustained "seeking outside help"
escalates to a hard flag after three of the last five classified chunks
agree, never on one. That switch is **code, not an environment variable**
(`Settings(llm_complete=...)`): this repo does not hardcode a call to any
model vendor, so wiring a real classifier means writing a small adapter
around your own model client. See ARCHITECTURE.md § 5.5.2 for the full
tier table.

As with identity, no speech model is bundled — but unlike ArcFace, that is
not a licensing block. Whisper's weights are MIT-licensed by OpenAI;
they're excluded here because the dependency (torch or ctranslate2) is
hundreds of megabytes for a feature that ships off by default. Point
`PROCTOR_AUDIO_MODEL` at your own downloaded model.

The console renders evidence for every rule — fusion, identity, and audio
— on demand: the board snapshot carries only a sample count per flag
(embedding the full samples in a payload that's refetched on every
WebSocket message would be wasteful at exam-room scale), and opening a
flag fetches `GET /v1/proctor/sessions/{id}/violations/{id}` for the full
record. Audio's evidence carries a `transcript_ref` rather than the words
themselves; the console dereferences that one level further, and shows
"no longer available" once it's past retention rather than failing
silently.

The media plane — a self-hosted [LiveKit](https://livekit.io/) SFU for the
candidate/proctor call, plus optional recording — is gated the same way
as identity and audio:

```bash
PROCTOR_MEDIA_CONSENT_NOTICE="candidates shown a live-video notice at exam start"
PROCTOR_LIVEKIT_URL=wss://livekit.example.org PROCTOR_LIVEKIT_API_KEY=... PROCTOR_LIVEKIT_API_SECRET=...
```

Leaving `PROCTOR_LIVEKIT_URL` unset selects a fake room provider — no
network call, no real server — the same "empty selects the test double"
convention as the face and speech models, except the reason here is
simply that no LiveKit server was available to test against, not
licensing or dependency weight. Candidate tokens can only ever publish;
proctor tokens (subscribe + start recording) are gated behind the console
token, the same elevation-of-privilege boundary the rest of the proctor
API already enforces. The Electron client joins and publishes to its room
via `livekit-client`, vendored the same way as the MediaPipe assets below.
See ARCHITECTURE.md § 5.6.

Not yet built: client-side microphone capture/VAD outside a media call,
ID document capture.

## Running the client

```bash
cd apps/client && npm install && npm run vendor
```

`npm run vendor` copies the MediaPipe WASM and the `livekit-client` SDK
out of `node_modules`, and downloads two models from Google's official
host — the face landmarker (3.8 MB) and EfficientDet-Lite0 (13.8 MB, for
phone/person/book detection) — recording SHA-256 digests in
`models/manifest.json`. The assets are bundled rather than fetched at
exam time: the client must work on a locked-down network, and a model
downloaded during an exam is a model nobody has attested.

The object detector is EfficientDet-Lite specifically because it is
Apache-2.0. **Do not swap in Ultralytics YOLO** — it is AGPL-3.0 and that
reaches network-deployed services. One honest gap: COCO-80 has no
smartwatch or headphones class, so the `wearable_detected` rule in the
policy cannot fire from this model. The rule is left in place rather than
deleted so the gap is visible instead of silently absent.

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

Python (279, plus 19 that need Postgres — see below) and TypeScript (56).
The store tests run the same assertions against both SQLite and Postgres,
since a behaviour that holds in one and not the other is a bug that
otherwise only surfaces in whichever environment is less tested:

```bash
PROCTOR_TEST_POSTGRES_DSN=postgresql://proctor:PASSWORD@localhost:5433/proctor .venv/bin/python -m pytest python/tests/test_store_backends.py -q
```

They skip cleanly when that variable is unset, so a laptop without Docker
still gets a green suite. The rest of the suite has three parts, and the last
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
  Texas CUBI. Consent and deletion paths are product requirements. BIPA in
  particular carries statutory damages and a private right of action.
- **No ArcFace weights are bundled** — the common ones are
  non-commercial-research-only. See ARCHITECTURE.md § 5.4.2.
- Read ARCHITECTURE.md § 3 before relying on any security property. It says
  plainly what each defence does not buy you, including a documented,
  unclosed gap in clock-stretch detection.

## Licence

MIT.
