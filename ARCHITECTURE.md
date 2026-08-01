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
  proctor_identity/   enrolment, face matching, temporal decision (no I/O)
  proctor_audio/      transcription, intent classification, escalation (no I/O)
  proctor_sim/        headless candidates, honest and hostile
  tests/              behavioural + adversarial + conformance suites
apps/client/src/
  protocol/           generated types + WebCrypto signing
  main/               Electron main: OS observation, signed transport
  renderer/           camera, MediaPipe, gaze geometry
apps/console/         static proctor console: triage queue + timeline
policies/
  default.yaml        exam policy as reviewable data
tools/
  generate_ts_protocol.py   Pydantic schema  ->  TypeScript types
  conformance.ts            TS-signed frames for the Python verifier
```

Planned, not yet built: `services/media` (SFU + recording), client-side
microphone capture and VAD (see § 5.5.4).

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

## 5.1 Triage and the console

`proctor_gateway/triage.py` aggregates the violation stream into one row
per session, ordered so the sessions worth a look sort first. Ordering is
a recency-weighted flag count with a three-minute half-life, so a candidate
who had one bad moment early on is not pinned to the top of an
invigilator's screen for the rest of the exam.

Three decisions in that file matter more than the arithmetic:

**The score is server-side.** Two proctors must see the same queue, it must
survive a refresh, and the logic deciding whose name floats to the top is
consequential enough to belong somewhere unit tested. A decay function in
dashboard JavaScript is none of those.

**The score is never displayed.** It orders the queue; the console renders
a coarse band (`quiet` / `notice` / `review`). A number beside a person's
name is read as a confidence value by a tired human under time pressure, no
matter how it is labelled, and this number is not one.

**INFO severity contributes zero.** Capture-quality notes are context for
review. A candidate with a bad webcam must not drift up the queue for it.

The console (`apps/console/`) is static and served by the gateway. Its
visual design is deliberately calm — the obvious design is a wall of red
alerts, and it is the wrong one, because colour primes judgement and these
flags come from models with measurable error rates against real people.

## 5.2 Proctor authentication

`/v1/proctor/*` requires a bearer token — as a subprotocol on the
WebSocket, since browsers cannot set headers on a handshake and a query
string would land in access logs.

It **fails closed**. When `PROCTOR_CONSOLE_TOKEN` is unset the gateway
generates a random token and logs it rather than leaving the endpoint open,
so local work stays friction-free without there ever being a deployment
that is accidentally public. These endpoints carry every candidate's flags
and evidence; unauthenticated, the port is a live feed of who is being
accused of what.

## 5.3 Persistence

`proctor_gateway/store.py`. SQLite, WAL, single file, behind a narrow
`Store` interface so Postgres can replace it without touching the request
handlers. `PROCTOR_DB_PATH=:memory:` selects a real no-op implementation
rather than branching the gateway.

**This is a security requirement, not a convenience.** Replay protection
works by refusing a sequence number already seen. With `last_seq` in memory
only, restarting the gateway resets it to −1 and a client can re-send its
entire earlier stream — every "face present, looking at the screen" frame
it has already spent. A restart must not be a way to launder a replay, and
`test_restart_does_not_launder_a_replay` is the test that says so.

Session state is checkpointed on **every frame**, so the window in which a
crash could permit a replay is one frame wide. Measured: ~43,000
checkpoints/sec, which is roughly 4,300 concurrent candidates at 10Hz
before the store becomes the bottleneck. `synchronous=NORMAL` trades a
few commits on power loss for that throughput; `FULL` would fsync per
commit and make ingest disk-bound.

What is deliberately **not** persisted: the fusion engine's per-rule onset
timers. A candidate mid-look-away when the gateway restarts gets a fresh
onset window. That errs toward not flagging, which is the right direction,
and a candidate cannot trigger a restart to exploit it.

Sessions are always restored as **disconnected**. Restoring `connected=True`
would show a proctor a live candidate who is not there, and would have the
silence rule evaluated against a stream that does not exist.

### 5.3.1 Retention

`PROCTOR_RETENTION_DAYS`, default 30. Violations and finished sessions
older than the window are purged at startup.

This is not housekeeping. Every evidence row is an observation about an
identifiable person derived from their face; keeping it past the point it
is needed for review is the difference between a proctoring system and a
biometric archive. Institutions with a shorter statutory limit should lower
it, and `0` disables purging for jurisdictions that mandate longer holds.

Purge runs **before** restore. The other order loads expired rows into
memory and only then deletes them from disk, so the board keeps showing
candidates whose data was supposed to be gone and their sessions still
accept a telemetry socket — retention that applies only to the database is
not retention. That bug existed for about ten minutes and is now pinned by
`test_websocket_for_a_purged_session_is_rejected`.

### 5.3.2 Interaction with the master secret

Session keys are derived from `PROCTOR_MASTER_SECRET`. With persistence on
and that secret unset, a restart restores the sessions but derives
*different* keys for them, so every frame from a resuming client fails its
signature check and the candidate is flagged for something the server did.
The startup warning says this explicitly when both conditions hold.

## 5.4 Identity verification

`python/proctor_identity/`. Enrolment builds a reference template from
several captures; periodic probes are compared by cosine similarity;
sustained disagreement is reported as an ordinary flag.

This is the most consequential thing the platform can say about a person —
that someone else sat their exam — and it is said by the component with
the largest documented accuracy gap. NIST's FRVT 1:1 evaluations found
false-match rates varying by one to two orders of magnitude across
demographic groups, highest for West and East African and East Asian
faces, for women, and at the extremes of age. Four consequences are built
into the code rather than written down and forgotten:

**There is no default threshold.** `MatchPolicy` requires one, and
`Threshold.calibrated_on` requires a record of what population it was
measured against. Identity verification stays disabled until both
`PROCTOR_IDENTITY_THRESHOLD` and `PROCTOR_IDENTITY_CALIBRATED_ON` are set.
A cutoff with no provenance is a guess wearing a number.

**Unusable captures are `NOT_ASSESSABLE`, never `MISMATCH`.** A dark room,
a turned head, a distant face — none of these produce a similarity score
at all, because a number computed from an unusable frame reflects the
lighting rather than the person and would then sit in someone's audit
trail looking like evidence. Being hard to photograph is not evidence of
impersonation, and conflating the two is precisely how a system ends up
failing hardest the people its models were already worst at.

**One low frame is never enough.** Three of the last five assessable
probes must disagree, and `VerificationPolicy` refuses to be configured
below two. A long run of unassessable probes reports separately as
`UNOBSERVABLE` — "we cannot see", not "it is someone else".

**Findings carry their own caveat and their own numbers.** The evidence is
the similarities and the threshold applied, and the message shown to a
proctor states the demographic accuracy caveat inline. A reviewer sees
`0.41 against 0.55 (calibrated: …)`, not the word "mismatch".

Identity reuses the ordinary violation record rather than a parallel
channel, so it inherits `action: flag` and `requires_human_review: true`
like every other rule. Nothing here ends an exam.

### 5.4.1 Why embedding happens server-side

The client could embed locally and send only a vector, which keeps face
images off the network. That is the better privacy answer and the wrong
security answer: a client that computes its own identity evidence can
resend the enrolment vector forever and the check becomes decorative.

So the image crosses the wire and the server embeds it. The compensating
controls are that **no face image is ever written to disk** — handlers
embed and discard — and that templates expire on their own much shorter
clock (`PROCTOR_TEMPLATE_RETENTION_DAYS`, default 1) than the similarity
scores derived from them. A template has no review value once the exam is
over; a human compares recordings, not vectors. The audit trail therefore
outlives the biometric.

Templates embedded by a different model are dropped on restore rather than
reused: comparing vectors across networks yields a meaningless similarity,
and a meaningless similarity that crosses a threshold is an accusation.

### 5.4.2 Model licensing — read before choosing weights

**No face model is bundled, deliberately.** Most readily available
ArcFace weights, including the entire InsightFace model zoo, are released
for **non-commercial research use only**. Shipping one would hand every
downstream user a licence violation — the same shape of problem as
Ultralytics' AGPL licence (§7).

`PROCTOR_FACE_MODEL` points at your own licensed ONNX export. Without one,
the deterministic test double is used; it is not a face model and never
claims to be, and exists so that this project's own logic can be tested
without a licensed model or a photograph of a real person.

### 5.4.3 Out of scope: ID document capture

The blueprint this was built from also called for scanning a government
ID. That is not implemented and is not a small addition: it introduces
name, date of birth, address and document number into a service that
currently holds only an opaque `candidate_ref`, and it carries its own
retention, accuracy and discrimination questions. It should be scoped
separately rather than folded in.

## 5.5 Audio pipeline

`python/proctor_audio/`. The edge already reports coarse voice-activity
telemetry independently of this package — `signal.audio` in
`proctor_protocol`, evaluated by the `sustained_speech` fusion rule — which
answers "was the candidate talking". This package answers the harder
question underneath it: transcribe sustained speech, and classify *why*
someone was talking rather than just that they were.

### 5.5.1 Why this is not the original project's keyword matching

The original Proctoring-AI project stripped stopwords from the transcript
and the exam paper and reported the overlap as a cheating signal. That
fails in the direction that matters: "what is the answer to number four"
shares almost no vocabulary with the question paper and passes straight
through, while a candidate reading the question aloud to concentrate — not
cheating — matches the paper's own wording and gets flagged for nothing.
Keyword overlap measures lexical similarity, not intent.

`KeywordIntentClassifier` exists to make that failure a runnable difference
rather than a claim in a docstring — `test_audio.py` puts it side by side
with the LLM-backed classifier on a paraphrase built to defeat a fixed
trigger list, and the keyword double loses. It is never imported by
`proctor_gateway`.

### 5.5.2 Two gates, for two different risks

Identity verification and the audio pipeline both fail closed on missing
configuration, but on **different** provenance requirements, because they
carry different legal exposure:

- Identity's problem is **measurement accuracy** varying by population, so
  it requires `identity_threshold` + `identity_calibrated_on` — a record of
  what the cutoff was measured against.
- Audio's dominant *additional* exposure is **consent**: recording and
  transcribing a person's voice implicates wiretap and all-party-consent
  statutes in the US and GDPR Art. 6/7 in a way a webcam frame comparison
  does not. It requires `audio_consent_notice` — a record of what
  candidates were told before their microphone was recorded.

Within audio there is a second, independent tier. Consent gates
*transcription itself* (the audio pipeline is enabled or it is not).
Whether an LLM additionally judges intent is a separate switch
(`Settings.llm_complete`, injected code, not an environment variable — this
repository does not hardcode a call to any model vendor):

| Consent notice | `llm_complete` | Behaviour |
|---|---|---|
| unset | — | `503`. No audio is transcribed at all. |
| set | `None` | Transcripts are produced and stored for a human to read. No automated intent read. The coarse VAD-only `sustained_speech` rule still applies. |
| set | injected | Transcripts *and* sustained-disagreement escalation to a hard flag. |

### 5.5.3 One low frame is never enough, again

Same discipline as identity verification: three of the last five
classified chunks must read `seeking_help` before anything escalates
(`AudioMonitorPolicy`, which refuses to be configured below two), and a
malformed or unparsable LLM response becomes `unclear` — never
`seeking_help` — because failing toward silence is the safe direction when
the cost of a false positive is a person wrongly investigated.

### 5.5.4 The privacy split, adapted

Same shape as identity's template/score split, adapted to a different
sensitivity profile: a transcript is not a biometric template, but it is
*more* revealing in another way — it is the actual content of what was
said, which can include third parties, health information, or anything
else spoken near an open microphone.

- **Raw transcripts** live on their own short clock
  (`audio_transcript_retention_days`, default 1) in `audio_transcripts`.
- **Classification labels and confidences** — the audit value — survive on
  the ordinary long retention clock in `audio_checks`.
- The violation raised on escalation embeds the labels, confidences and a
  `transcript_ref`, and deliberately **not** the transcript text —
  `_audio_violation` strips `transcript_excerpt` before persisting.
  Baking the actual words into the long-retained violation record would
  defeat the point of giving transcripts a shorter clock. The transcript
  is fetchable separately, on demand, via
  `GET /v1/proctor/sessions/{id}/audio/transcripts/{transcript_id}`, for as
  long as it still exists — once purged, that endpoint 404s like any other
  expired record, and the flag survives without the words.

**Closed.** The console now renders evidence for every rule — fusion,
identity, and audio — via the same mechanism: `TimelineEntry.evidence_count`
in the board snapshot, and `GET /v1/proctor/sessions/{id}/violations/{id}`
fetched lazily when a proctor opens a specific flag. Audio's
`transcript_ref` is dereferenced the same way, one click further in, via the
existing transcript endpoint — see § 5.5.8.

### 5.5.5 Whisper is not bundled, but not because of its licence

Unlike ArcFace, Whisper's code and model weights are released by OpenAI
under the MIT licence, so vendoring a small model would not be a licence
violation the way ArcFace weights would be. It is excluded from this
repository anyway, for a practical reason: the runtime dependency
(`faster-whisper`/ctranslate2, or `openai-whisper`/torch) is hundreds of
megabytes, disproportionate for a feature that ships off by default.
`WhisperTranscriber`'s error message says exactly this — it must not claim
a licensing block that does not exist, because that claim would be false
and this document has spent real effort being precise about which
licences actually block what.

### 5.5.6 Not built: client-side capture

The blueprint this was built from calls for Silero VAD running locally in
the browser/Electron renderer, with audio chunks uploaded only once
sustained speech is detected. That client-side capture and VAD wiring is
not built — this increment is the server-side pipeline (transcription,
classification, temporal escalation, storage, retention, and the gateway
endpoints), mirroring how identity verification's backend was built without
also wiring an Electron enrolment-capture UI. Test coverage instead drives
the gateway endpoints directly with base64 chunks, the same way the
identity endpoints are tested.

### 5.5.7 A discovered issue, fixed while building this

Both `embedder.embed(...)` (identity) and, now, `transcriber.transcribe(...)`
/ `intent_classifier.classify(...)` (audio) are synchronous calls that, with
a real model behind them, are CPU-bound. Calling them directly inside an
`async def` handler blocks FastAPI's event loop for that duration — for
every other candidate's telemetry websocket, not just the one being
processed. All four call sites now run through `asyncio.to_thread`. The
identity endpoints had this latent issue already; it is fixed here rather
than left for audio alone to get right, since leaving it asymmetric while
noticing it would be negligent.

### 5.5.8 Rendering evidence in the console

The board snapshot (`GET /v1/proctor/sessions`) never carries raw evidence
— `TimelineEntry` carries `evidence_count` only, an integer. This is not an
oversight; it is sized deliberately against how the console actually
polls. `console.js` refetches the *entire* board on every WebSocket
message it receives, by design (a comment there explains why: scoring and
ordering stay server-side, so the client re-reads rather than
reimplementing decay logic that would drift). Embedding up to 64 evidence
samples per flag, per session, in every one of those refetches would make
an ordinary exam room's traffic pattern expensive for no benefit — a
proctor is not reading raw signal samples for every flag in the queue at
once.

Full evidence is one click away instead:
`GET /v1/proctor/sessions/{session_id}/violations/{violation_id}` returns
the complete stored record — evidence array, message, severity — for the
one violation a proctor actually opens. `store.load_violation` returns the
*firing* row rather than a later resolution row for the same
`violation_id`: the fusion engine reuses the id across both, and the
resolution row carries no evidence (`evidence=()`), so naively taking the
latest row for an id would silently show nothing for a resolved flag.

Audio's evidence carries a `transcript_ref` rather than the words
themselves (§ 5.5.4); the console dereferences it one level further, via
the existing `GET .../audio/transcripts/{transcript_id}` endpoint, cached
client-side and rendered inline once fetched. If the transcript has since
been purged, that endpoint 404s and the console shows exactly that rather
than failing silently — the flag and its labels survive independently of
whether the words still exist.

## 5.6 Media plane: SFU and recording

Everything above this section describes the telemetry path: signed
observations, never raw video. This section describes the other path —
the actual audio/video call between a candidate and a proctor, and its
optional recording — which is a genuinely separate system with its own
trust boundary, not an extension of the telemetry protocol.

**Scope, stated up front.** This is a backend integration layer against a
self-hosted [LiveKit](https://livekit.io/) (Apache-2.0) deployment: token
issuance, webhook verification, and recording lifecycle tracking. It was
built and tested with no live LiveKit server available in this
environment — see the confidence note in
`proctor_media.provider`'s module docstring for exactly which parts rest
on solid ground (tokens, webhooks — long-stable, widely-used API surface)
versus which are a best-effort against a documented but more
version-sensitive RPC (the Egress recording calls). Test this against
your actual deployed server version before relying on it in production.

### 5.6.1 Why LiveKit, and why self-hosted

Chosen over a hosted-only alternative because this project already treats
"an institution can run this entirely on infrastructure it controls" as a
requirement, not a nice-to-have — the same reasoning behind vendoring
MediaPipe and keeping face/audio models pluggable rather than calling out
to a hosted API. LiveKit is Apache-2.0, self-hostable, and its access-token
and webhook formats are stable enough to implement confidently without a
live server (§ 5.6 scope note above).

### 5.6.2 Token model: the elevation-of-privilege boundary

`proctor_media.tokens.VideoGrant` has two constructors, and this is
deliberate: a caller cannot assemble an arbitrary grant, only ask for
`VideoGrant.publisher(room)` or `VideoGrant.subscriber(room)`. Their fields
are fixed, not caller-configurable:

- **Candidate grant** (`publisher`): `canPublish=True`,
  `canSubscribe=False`, `roomRecord=False`, `roomAdmin=False`. A leaked
  candidate token is useless for watching or recording anyone — it can
  only publish the candidate's own camera/mic into their own room.
- **Proctor grant** (`subscriber`): `canPublish=False`,
  `canSubscribe=True`, `roomRecord=True`. Can watch and can start a
  recording, never publish.

This asymmetry is enforced structurally, in the dataclass, rather than
left to each call site to remember to set the right five fields correctly
— the same design instinct as `Threshold` and `MatchPolicy` requiring
explicit construction elsewhere in this codebase.

The gateway layers its own authorization on top: `POST
/v1/sessions/{id}/media/token` (candidate grant) needs only the
`session_id` — the same bearer-capability model already used by the
identity and audio endpoints (see § 5.4/5.5; a 96-bit
`secrets.token_hex(12)` value, never logged, never guessable, but a
capability token rather than a signed request). `POST
/v1/proctor/sessions/{id}/media/token` (proctor grant) additionally
requires the console bearer token. Nothing a candidate's client holds can
ever reach the proctor grant shape — proven in
`test_a_candidate_cannot_obtain_a_proctor_grant`
(`python/tests/test_media_api.py`). Moving session-scoped auth to a
stronger signature-based scheme is a documented hardening step for a real
deployment, not something this repository does today; it would apply
identically to identity, audio, and media.

### 5.6.3 Webhook verification

LiveKit signs webhook deliveries with a JWT in a custom `Authorize`
header whose payload commits to `sha256(body)`. `verify_webhook` checks,
in order: the JWT's own HMAC signature (wrong secret → rejected), its
`iss` claim against the configured API key (a webhook signed by a
*different* LiveKit project's credentials → rejected), and — the check
that actually matters — that the claimed `sha256` matches the hash of the
body **as received**. Verifying only the JWT's signature would pass a
validly-signed header attached to a swapped body; the body-hash comparison
is what catches that
(`test_a_tampered_body_is_rejected_even_with_a_validly_signed_header`,
`python/tests/test_media.py`).

### 5.6.4 Recording lifecycle: not a temporal-discipline problem

`proctor_media.recording.RecordingRecord` is a plain state machine
(`REQUESTED → ACTIVE → {STOPPING, AVAILABLE, FAILED}`), and deliberately
has none of the onset/sustained-disagreement machinery built for the
fusion engine's rules, identity's mismatch gate, or audio's seeking-help
gate. Those all exist to stop a probabilistic inference about a
candidate's behaviour from firing on one noisy sample. Starting or
stopping a recording is not an inference about the candidate at all — a
proctor clicked a button — so there is nothing here for that kind of
discipline to protect against.

What the state machine does have to account for is delivery order, twice
over, and both were found by writing the tests rather than by inspection
first:

- A recording can reach `AVAILABLE` directly from `ACTIVE` with no local
  `STOPPING` step ever happening — the candidate disconnects, the room
  closes, and the completion webhook arrives having never been asked to
  stop locally.
- The same is true one step earlier, from `REQUESTED`: a proctor can click
  "stop" (or the recording can simply finish) before the `egress_started`
  confirmation webhook has arrived at all. LiveKit gives no ordering
  guarantee across webhook retries, and a proctor's local click is not a
  webhook in the first place, so `REQUESTED` reaches every later state
  directly, exactly as `ACTIVE` does.

An out-of-order or duplicate delivery that would otherwise corrupt state
(e.g. a late `egress_started` arriving after `egress_ended` already
marked a recording `FAILED`) is logged and ignored rather than raised as
an error back to LiveKit — a webhook endpoint that 500s on a delivery
order it does not control just invites the sender to retry the same
problematic delivery forever.

`RecordingRecord.as_dict()` never carries recording bytes or a playable
URL, only a `storage_ref` — the same "reference, not the artifact"
principle as an identity template or an audio transcript, applied to the
single most sensitive artifact this platform touches.

### 5.6.5 Consent and retention

`media_consent_notice` is one gate for both joining the room and
recording it, not two: a live video call between a candidate and a
proctor is itself processing of biometric-adjacent data before anyone
presses record, so gating only the recording would be false comfort.

`recording_retention_days` (default 14) is its own, shorter clock than the
30-day violation default — deliberately, this is the single most
sensitive artifact the platform touches. Purging a recording row deletes
this project's *reference* to it (the egress id, the storage URI, the
lifecycle timestamps); it does not delete the actual object from wherever
the operator's storage backend holds it (S3, GCS, ...). That is the
operator's storage lifecycle policy's job — the same boundary
`LiveKitRoomProvider` draws around not handling object storage
credentials itself. Treating the metadata purge as equivalent to deleting
the video would be a false sense of compliance.

### 5.6.6 The Electron client side

The renderer joins and publishes through `livekit-client` (Apache-2.0),
vendored the same way as MediaPipe's WASM bundle — a single self-contained
ESM file (`livekit-client` ships one at `dist/livekit-client.esm.mjs`,
confirmed to carry no external imports) copied into `models/` and mapped
in the inline import map, so it resolves over `file://` with no bundler.
`scripts/vendor-assets.mjs` copies it and records its digest in
`manifest.json` alongside the MediaPipe assets, for the same reason: an
asset swapped after the fact should be visible, not silent.

**Handing the renderer a token is a different call than withholding the
telemetry session key** (`telemetry.ts`), not an inconsistency in it.
That key signs arbitrary observations with Node's HMAC primitives and has
no reason to ever leave main; a LiveKit token has to reach the renderer
regardless, because `livekit-client` needs `RTCPeerConnection` and
`getUserMedia`, which only exist in a DOM context. What keeps this safe
is the token's own shape (§ 5.6.2), not where it is held.

`joinMediaRoom` (`src/renderer/media.ts`) is deliberately not load-bearing
for monitoring: it is called after MediaPipe is already running, publishes
the same camera `MediaStreamTrack` MediaPipe reads from (not a second
`getUserMedia` call — one device, one consumer count, no double prompt),
and any failure — media disabled, no LiveKit reachable, connection
refused — is caught and logged, never thrown back into the caller. The
signed telemetry path is what the fusion engine rules on; the live call
is additive. Microphone access is requested separately, and only after a
room join actually succeeds — MediaPipe never touches audio, so asking
earlier would be a permission prompt with no purpose behind it yet.

**CSP.** The renderer's Content-Security-Policy pins `script-src` to an
exact hash of the inline import map, so allowing `livekit-client` to
resolve meant regenerating that hash — `scripts/build.mjs` now recomputes
it from the actual file and fails the build if the CSP's pinned value
doesn't match, rather than trusting whoever last edited the import map to
have done the same arithmetic by hand. Separately, `connect-src` had to
widen from implicit `'self'` to `'self' wss: https:`: the operator's
LiveKit URL is a runtime deployment value, fetched from the gateway after
enrolment, not a build-time constant `script-src`'s exact-hash approach
could pin. This is a real, if narrow, loosening — only network
destinations are broadened, not script execution — verified live: a
`ws://` (unencrypted) LiveKit URL is correctly refused by this CSP, and a
`wss://` URL is correctly allowed through to attempt (and, against no
real server, correctly fail) its connection.

## 5.7 The exam shell: consent, detection, lockdown

### 5.7.1 Consent is a gate, not a banner

Nothing is captured before the candidate accepts. The camera is not
opened, no model is loaded, and no signal is emitted until the button in
`consent.ts` is pressed. This ordering is the whole point: a disclaimer
displayed while the webcam light is already on is *notification*, and
notification and consent are not interchangeable.

The dialog names every capture individually — camera and what is inferred
from it, microphone, recording and its retention window, process and
display checks, and the lockdown allowance. "Your session is monitored"
is the kind of sentence written to avoid alarming people, and it leaves a
candidate genuinely unaware that a video file is being kept.

There is deliberately **no decline button**. This shell cannot offer a
meaningful alternative — the exam does not proceed either way — and a
decline button that closes the app would dress an institutional
requirement up as a free choice. The candidate declines by closing the
window, which is honest about what declining costs them. An institution
deploying this owes them a real route to an unproctored alternative; that
route is not something a dialog can provide.

Consent reaches the server twice, on purpose. The client's *main* process
emits an `exam_start` lifecycle event on the signed telemetry stream —
ordered and tamper-evident alongside every observation — and separately
POSTs `/v1/sessions/{id}/consent` so the gateway learns about it without
having to parse the telemetry stream for control flow. The renderer does
neither directly: main rejects any payload from it that is not a
`signal.*`, because a renderer able to originate arbitrary lifecycle
phases could claim `identity_verified`.

### 5.7.2 Recording follows the room, not the click

The obvious implementation — start recording when consent is granted —
does not work, and the reason is worth recording because it is not
obvious from the outside. At the moment consent is given the candidate
has not joined the media room yet (the dialog is shown *before* the
camera opens, per § 5.7.1), so there is no room to record and LiveKit
answers `requested room does not exist`.

Recording is therefore started from LiveKit's `room_started` webhook.
That also means a candidate who drops and reconnects gets a recording
without any retry logic, and it keeps the candidate's client unable to
start or stop a recording — those endpoints stay proctor-only.

Two races had to be handled, both found by running the real stack rather
than by reading the code:

- `egress_started` routinely arrives *before* the gateway's own
  `StartRoomCompositeEgress` call returns and writes its row. Both paths
  now key the row on the egress id, and whichever lands first wins; the
  other finds the existing row instead of duplicating it or walking the
  status backwards.
- `room_started` can be delivered more than once. Starting a second
  egress for a session that already has a live one would produce two
  files and two rows for one exam, so the start path is idempotent per
  session.

### 5.7.3 Object and frame-quality detection

Object detection uses MediaPipe's EfficientDet-Lite0 (Apache-2.0), which
is what finally lets the `phone_detected` and `second_person_detected`
rules fire — they had been in the shipped policy since the beginning with
nothing emitting `signal.object`, because the obvious detector (YOLO) is
AGPL-3.0 and § 7 rules it out for network-deployed services.

Stated rather than hidden: COCO-80 has `cell phone`, `person` and `book`
but **no smartwatch and no headphones class**, so `wearable_detected`
still cannot fire. The rule stays in the policy because deleting it would
hide the gap from anyone reading the policy to see what is intended.

`signal.frame_quality` (variance-of-Laplacian sharpness plus brightness)
exists so that "we could not see" is distinguishable from "they did
something". Every other visual signal degrades silently when the camera
is bad: a blurred or dark frame lowers gaze confidence, loses iris
landmarks and drops face detections, which — read without context — looks
exactly like a candidate turning away. Its rule is `soft`, because an
unusable camera is overwhelmingly a cheap webcam in a dim room rather
than deliberate obstruction, and this code cannot tell those apart.

### 5.7.4 Lockdown, and what it is not

The shell blocks fullscreen exit, clipboard, developer tools, print,
find, and right-click. This stops the accidental and the casual — the
reflexive Cmd-C, the ESC that drops fullscreen mid-question. **It does
not stop anyone determined.** A candidate can alt-tab at the OS level,
use a second machine, or photograph the screen, and no amount of
`preventDefault` in a renderer changes that. It is a guardrail plus an
observation channel; the OS-level signals in the main process are what
notice the serious cases.

The three-warning allowance exists because ESC is muscle memory. Someone
who hits one in the first minute of a stressful exam has not cheated, and
a system that escalates on the first press generates flags a human then
dismisses — which trains reviewers to dismiss flags generally. An
exhausted allowance is a deliberate pattern, which is a far more
reviewable claim.

The count is local so the candidate can be warned instantly and
accurately, but it is **not the authority**: `LockdownSignal.strike` is an
observation like everything else, and the server counts the signals it
received. A tampered client reporting `strike: 0` forever does not
thereby have zero strikes.

This is also the one place the fusion engine's onset guard had to be
relaxed. `Rule` normally refuses a HARD rule with `onset_ms=0`, because a
noisy detector firing on one frame becomes an accusation. A keystroke is
not a detector sample — there is no "sustained Ctrl+C" to confirm, and
requiring a window would make the rule fire on the second press or never
— so `Rule.discrete` opts a rule out of that guard. It defaults to false,
must be set per rule, and has tests pinning both the default and that
ordinary detector rules are still rejected.

### 5.7.5 Toasts are warnings, not verdicts

The top-right notices are addressed to the candidate, not the proctor. A
candidate who does not know they have been flagged cannot correct what
caused it — moving a phone off the desk, turning on a light, sitting back
in frame — and silent observation followed by a post-exam accusation is
the failure mode this project exists to avoid. The wording is corrective
("A phone is visible — please move it out of view"), never accusatory.

They are driven by local detections, which makes them immediate but also
means **a toast is not proof a flag was raised**: the server applies onset
windows and confidence thresholds the renderer does not, so a brief
phone-shaped blur can toast without ever becoming a violation. Warning
early and sometimes unnecessarily is kinder than warning late and never,
but it does mean a toast must never be phrased as though a decision has
been made.

## 5.8 Storage backends

`SqliteStore` and `PostgresStore` implement the same `Store` protocol, and
which one is in use is a connection-string decision no request handler can
observe. SQLite stays the default because it is single-file, stdlib, and
entirely adequate for one gateway process — the test suite and local
development should not need a database container.

Postgres exists for the point at which either of those stops being true:
more than one gateway process (SQLite is single-writer), or an institution
that wants the audit trail somewhere a person can query with ordinary
tools.

The SQL is written out twice rather than shared through a dialect
abstraction. The two diverge in placeholders, upsert syntax,
autoincrement, boolean handling and JSON decoding, and a shared layer
would be mostly branches — a subtly wrong branch in the store that holds
the audit trail is worse than duplication a reader can check line by line.

`python/tests/test_store_backends.py` runs one parametrised suite against
both, because a behaviour that holds in one and not the other is a bug
that otherwise surfaces only in whichever environment is less tested. The
Postgres runs skip when no DSN is configured, so a laptop without Docker
still gets a green suite — they are not optional where it matters.

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
client (consent gate, edge inference, object and frame-quality detection,
exam lockdown, LiveKit publishing — § 5.6.6, § 5.7), proctor console
(including on-demand violation evidence and transcript rendering,
§ 5.5.8), persistence on SQLite *or* Postgres (§ 5.8), identity
verification, audio pipeline, and the media plane (§ 5.6) — 279 Python
(plus 19 Postgres-backed) + 56 TypeScript tests.

The full chain has been run end-to-end against a synthetic camera feed in
both the face-present and no-face cases (`apps/client/scripts/e2e.mjs`),
and the media plane against the real docker-compose stack: consent → room
join → publish → `room_started` webhook → egress → a playable MP4 on disk
with its path recorded in Postgres.

Four bugs surfaced only from that live run, none of which any amount of
unit testing would have caught, and all of which are the argument for
running the real thing:

1. The Egress RPC was being POSTed to a `ws://` URL. Twirp is HTTP; the
   server answered 404, which reads like a wrong path rather than a wrong
   scheme.
2. LiveKit sends its webhook JWT in `Authorization` (bare, no `Bearer`),
   not the `Authorize` header this code had been written against — every
   delivery was rejected 401 and no recording ever started.
3. Egress webhooks carry no top-level `room` object; the room name is
   inside `egressInfo.roomName`. Reading only `room.name` left every
   egress event with an empty room name.
4. `PROCTOR_LIVEKIT_URL` was serving two different network perspectives —
   the gateway's own API calls and the address handed to clients. Inside
   Docker these are necessarily different, so it is now split into
   `livekit_url` and `livekit_public_url`.

Two findings from live runs are worth recording, because they are the
argument for doing this at all. The process blacklist matched the
substring `parsec` to catch the game-streaming app, and on macOS that
matches Apple's `parsecd` and `parsec-fbf` — CoreParsec, the Spotlight
suggestions daemon, present on every Mac. **Every macOS candidate would
have been flagged for running remote-control software and screen
sharing.** The unit tests did not catch it because they only tested
against process names someone had thought of. Matching is now by exact
basename with OS-vendor paths excluded, and a test runs against the real
process table of whatever machine it is on.

**A send-ordering race, found by running the real chain rather than by
inspection — now fixed.** `apps/client/scripts/e2e.mjs` showed
`stream_sequence_gap`/`stream_replay` firing against an honest client:
the gateway was correctly detecting frames arriving out of sequence
order, but the client, not an attacker, was the cause.
`TelemetryClient.emit()` assigned a frame's sequence number
synchronously, then signed it asynchronously before sending. The
renderer's detection loop fires several observations per tick without
awaiting each other, so two overlapping `emit()` calls could have their
signing resolve in the opposite order their sequence numbers were
assigned in, and whichever finished signing first was sent first. The
client was flagging itself for the one attack that counter exists to
detect.

`emit` now chains dispatch on the previous frame, so frames reach the
socket in sequence order while signing still overlaps; `close` drains
the chain, since dropping frames mid-signature at shutdown would
manufacture the same gap on the exit path. Three consecutive
twelve-second end-to-end runs at ~90 samples each now report no
integrity breaches, where the race previously fired on two runs in
three at that throughput.

Two things about this are worth keeping in mind rather than filing away.
First, it was invisible to unit tests by construction: with a
well-behaved signer the ordering is correct by luck, so the regression
tests inject a signer that resolves in reverse order on purpose.
Second, `telemetry.ts` had no unit tests at all, because its constructor
used TypeScript parameter properties and Node's `--experimental-strip-types`
refuses them — the module could not be loaded by the test runner. That is
a reminder that "no tests" is sometimes a toolchain fact rather than a
choice, and worth checking for directly.

Not built: a browser-extension form factor — an extension can do camera
inference, fullscreen, tab focus and keystroke capture, but cannot
inspect OS processes or detect additional displays, so it would be a
materially weaker product that should not be described as equivalent.
Also not built: client-side microphone capture and VAD (§ 5.5.6), ID
document capture (§ 5.4.3). The proctor stream is now token-authenticated and
fails closed, but there is still no per-proctor identity, no audit of who
looked at whom, and no TLS termination here — do not expose this gateway
publicly without putting those in front of it.
