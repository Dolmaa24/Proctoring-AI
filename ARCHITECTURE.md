# Architecture

An edge-to-cloud automated proctoring platform. Real-time computer vision
runs on the candidate's machine; the cloud holds state, policy, identity and
evidence. Raw video never crosses the telemetry path.

This document records the decisions and, more importantly, what each one
does not buy us. A proctoring system that overstates its own guarantees is
dangerous, because the people acting on its output — invigilators, academic
integrity officers — will believe it.

---

## 1. Layout

```
python/
  proctor_protocol/   wire format: event schema, signing, key derivation
  proctor_fusion/     policy evaluation, temporal filtering  (no I/O)
  proctor_gateway/    FastAPI: ingest, integrity checks, proctor fan-out
  proctor_sim/        headless candidates, honest and hostile
  tests/              behavioural + adversarial + conformance suites
apps/client/src/
  protocol/           generated types + WebCrypto signing
  main/               Electron main: OS observation, signed transport
  renderer/           camera, MediaPipe, gaze geometry
policies/
  default.yaml        exam policy as reviewable data
tools/
  generate_ts_protocol.py   Pydantic schema  ->  TypeScript types
  conformance.ts            TS-signed frames for the Python verifier
```

Planned, not yet built: `apps/console` (proctor dashboard),
`services/identity` (ArcFace), `services/audio` (VAD → STT → intent),
`services/media` (SFU + recording).

### 1.1 Why the schema is generated

The Python models are the single source of truth; `make protocol` emits
their TypeScript equivalent, and a test fails if the checked-in file is
stale. Hand-maintaining both sides produces a specific, expensive bug:
someone adds a field in Python, the client never sends it, and nothing
errors anywhere — the signal simply never arrives, and it surfaces months
later as "why does this rule never fire".

`python/tests/test_conformance.py` closes the other half. It runs the real
TypeScript signing code and feeds its output to the real Python verifier,
covering every payload type, awkward floats and non-ASCII strings.

### 1.2 Process boundary inside the client

The session key, the sequence counter and the socket live in the Electron
**main** process. The renderer holds the camera and the models and can only
hand observations inward across a one-function IPC bridge.

This matters because the renderer is a browser context, and anyone who
opens DevTools has a JavaScript console inside it. With the key there,
forging telemetry is a paste; with it in main, an attacker has to modify
the packaged binary. Main also refuses `signal.environment` from the
renderer — a compromised page must not be able to claim a clean machine.

---

## 2. The central rule

> **The edge emits observations. The server decides violations.**

The client never reports "the candidate cheated". It reports
`yaw_deg=-42.0, confidence=0.88`. All policy lives server-side.

Two reasons, both load-bearing:

1. Thresholds can be retuned without reshipping a signed desktop binary to
   every candidate mid-exam-season.
2. A tampered client cannot suppress a verdict it never computed.

---

## 3. Trust model

The candidate has full control of the machine running the edge client. Root,
a debugger, a patched binary — assume all of it. Every guarantee below is
written against that assumption.

### What each defence actually buys

| Defence | Stops | Does not stop |
|---|---|---|
| HMAC per frame, per-session derived key | Tampering in flight; frames from an unprovisioned client | A candidate who extracts their own session key |
| Strict `seq` continuity | Dropping the incriminating window; reordering; replay | — |
| Monotonic-counter checks | Rewinding the clock to shrink a violation | Stretching it, up to the skew tolerance (§3.2) |
| Heartbeat + silence rule | Falling quiet while still connected | — |
| `stream_abandoned` on unclean disconnect | Quitting the app to stop reporting | — |
| Attestation of build/model hashes | Casually swapped models | A patched binary reporting expected hashes |

Two of these exist because a live run behaved differently from the test
harness, and both were ways to stop being proctored:

- A sequence gap originally froze `last_seq`, so *every* later frame also
  read as a gap and was discarded unevaluated. Dropping one early event
  left the candidate unwatched for the rest of the exam. Gaps now
  resynchronise: flagged, but the stream keeps being evaluated.
- Killing the client raised nothing beyond a disconnect notice, making
  "quit the app" cheaper than falling silent. Now an unclean disconnect
  raises `stream_abandoned`.

Integrity breaches are rate-limited per kind per session (10s) with full
counts retained. A persistent breach otherwise emits one flag per frame and
buries the console — under that volume a real phone detection is
unfindable, which is a safety failure rather than a cosmetic one.

The honest summary: cryptography here raises the cost of the *casual*
attack. The defences that matter against a determined one are continuity
(silence and gaps are themselves violations), server-side sampling, and the
session recording reviewed after the fact. Nothing in this design makes an
uninstrumented remote machine trustworthy, and no proctoring product's does.

### 3.1 Which clock times a rule

Rule durations use the client's **monotonic counter**, not server receipt
time. Server time seems safer but breaks under ordinary conditions: a
candidate on poor wifi whose client buffers three seconds of telemetry and
bursts it would have every sustained violation compressed below threshold.
"Have bad internet" must not be a way to defeat proctoring.

Absence rules use **server** time, because a client cannot be asked to
self-report that it stopped talking.

The counter is validated on every frame against server elapsed time.

### 3.2 Known limitation: clock stretch

A rewind is caught absolutely — a monotonic counter that moves backwards is
definitionally broken, so no tolerance applies.

A *stretch* — advancing the counter slower than real time — is bounded but
not eliminated. From one-way timestamps, clock stretch and network delay are
mathematically indistinguishable: both make client elapsed time lag server
elapsed time. So the skew tolerance is a real budget, and a client can hide
up to `clock_skew_tolerance_ms` of cumulative violation.

Consequences, stated plainly:

- Tolerance must sit **below the shortest rule onset**. The gateway warns at
  startup when it does not. It currently does not: tolerance is 2000ms and
  the shortest onset is `phone_detected` at 800ms.
- Lowering tolerance to close the gap causes false skew flags on any network
  hiccup longer than the tolerance, because delay reads as skew.
- **The actual fix is round-trip probes** — the server periodically pings,
  the client echoes, and the round-trip lets delay be separated from offset
  the way NTP does. Not yet built. Until it is, treat sub-second onsets as
  best-effort against a clock-manipulating client.

---

## 4. Data flow

```
Electron client ──WebRTC──▶ SFU ──▶ recording store        (media plane)
       │                                    ▲
       │ MediaPipe / ONNX inference, local  │ random server-side
       ▼                                    │ sampling for audit
  observations ──signed WS frames──▶ gateway ──▶ fusion engine
                                        │              │
                                   integrity      violations
                                    checks             │
                                                       ▼
                                              proctor console (WS)
```

Telemetry is ~1KB/s per candidate. Video is three orders of magnitude more,
which is the entire reason inference is at the edge.

---

## 5. Fusion engine

Pure logic, no clock of its own, no I/O — the full policy surface is
testable in milliseconds. Three durations per rule:

- **`onset_ms`** — how long a condition must hold *continuously* to count.
  Even HARD rules get a nonzero onset, so no single detector false positive
  can flag anyone.
- **`release_ms`** — hysteresis. Once a condition is building, it must be
  false for this long before the timer resets. Without it, one dropped
  detection mid-violation restarts the clock and nothing ever fires; a
  candidate who noticed could blink their way out of every rule.
- **`cooldown_ms`** — two minutes of looking away is one reviewable event,
  not forty-eight.

Low-confidence samples are treated as *absence of information*: they neither
advance the onset timer nor start the release timer. A detector losing
confidence must not be able to either manufacture or cancel a violation.

---

## 6. Human review

Every rule shipped in this repo is `action: flag`, and a test enforces it.
`LOCK_EXAM` exists in the schema because customers ask for it; the model
refuses to construct a rule that locks an exam without also flagging it for
review.

This is not squeamishness. Face detection and liveness models have measured
accuracy disparities across skin tones and lighting. Behavioural heuristics
penalise candidates with tics, ADHD, motor disabilities, or a habit of
reading questions aloud. A flag is a claim about a person that a human
should evaluate before it has consequences.

Correspondingly, the first tests in the gateway suite are the ones asserting
that innocent behaviour produces *nothing*: a housemate crossing the frame, a
single spurious phone detection, muttering while thinking, a bad webcam.

---

## 7. Licensing and compliance

- **Do not use Ultralytics YOLO** (v5/v8/v11) unless the project buys an
  enterprise licence. It is AGPL-3.0 and that reaches network-deployed
  services. Permissive alternatives: RF-DETR, YOLOX, or a self-trained
  detector over the four classes in `ObjectLabel`.
- Face embeddings are **biometric data**: GDPR Art. 9, Illinois BIPA, Texas
  CUBI. Consent, retention limits and deletion paths are product
  requirements, not backlog items.
- Audio → transcript → LLM intent analysis is the highest-risk component in
  the blueprint. It must produce review material, never a verdict.

---

## 8. Status

Built and tested: protocol, fusion engine, gateway, simulator, Electron
client (61 Python + 29 TypeScript tests). The full chain has been run
end-to-end against a synthetic camera feed in both the face-present and
no-face cases; see `apps/client/scripts/e2e.mjs`.

One finding from that run is worth recording, because it is the argument
for doing it at all. The process blacklist matched the substring `parsec`
to catch the game-streaming app, and on macOS that matches Apple's
`parsecd` and `parsec-fbf` — CoreParsec, the Spotlight suggestions daemon,
present on every Mac. **Every macOS candidate would have been flagged for
running remote-control software and screen sharing.** The unit tests did
not catch it because they only tested against process names someone had
thought of. Matching is now by exact basename with OS-vendor paths
excluded, and a test runs against the real process table of whatever
machine it is on.

Not built: Electron client and edge inference, SFU and recording, identity
verification, audio pipeline, proctor console, persistence (everything is
in-memory), authn/authz on the proctor stream (currently unauthenticated —
**do not expose this gateway publicly as it stands**).
