/**
 * Emit TypeScript-signed frames covering every payload type.
 *
 * Consumed by python/tests/test_conformance.py, which feeds each frame to
 * the real `verify_frame`. If the two halves of the protocol ever disagree
 * — key import, base64 alphabet, HMAC input, JSON encoding of a float or a
 * non-ASCII string — the Python side rejects the frame and the test fails.
 *
 * Run: node --experimental-strip-types tools/conformance.ts <sessionKeyB64>
 */

import type { Envelope, Payload } from "../apps/client/src/protocol/events.generated.ts";
import { importSessionKey, signEnvelope } from "../apps/client/src/protocol/signing.ts";

const SESSION_ID = "sess-conformance01";

const payloads: Payload[] = [
  // Floats chosen to be awkward: values whose shortest round-trip
  // representation differs between Python's repr and JS's toString are
  // exactly where a canonical-JSON scheme would have broken.
  {
    type: "signal.gaze",
    yaw_deg: 0.1 + 0.2,
    pitch_deg: -3.333333333333333,
    on_screen: false,
    confidence: 0.8800000000000001,
  },
  {
    type: "signal.head_pose",
    yaw_deg: 1e-7,
    pitch_deg: -0.0,
    roll_deg: 89.99999999999999,
    confidence: 1,
  },
  {
    type: "signal.face",
    face_count: 2,
    primary_bbox: { x: 0.125, y: 0.25, w: 0.5, h: 0.75 },
    confidence: 0.5,
  },
  { type: "signal.object", label: "smartwatch", confidence: 0.7, bbox: null },
  { type: "signal.liveness", score: 0.3333333333333333, confidence: 0.9 },
  { type: "signal.audio", speech_active: true, energy_db: -22.5, confidence: 0.65 },
  {
    type: "signal.environment",
    window_focused: false,
    monitor_count: 3,
    blacklisted_processes: ["obs64.exe", "AnyDesk", "Ünïcodé-Prøcess"],
    screen_share_active: true,
    confidence: 1,
  },
  { type: "heartbeat", frames_processed: 900, edge_fps: 29.97, dropped_frames: 3 },
  { type: "lifecycle", phase: "session_end", detail: "candidate finished — done" },
  {
    type: "attestation",
    client_build: "0.1.0+abcdef",
    model_hashes: { face_landmarker: "sha256:aaaa", detector: "sha256:bbbb" },
    platform: "darwin-arm64",
  },
];

const sessionKeyB64 = process.argv[2];
if (!sessionKeyB64) {
  console.error("usage: conformance.ts <sessionKeyB64>");
  process.exit(2);
}

const key = await importSessionKey(sessionKeyB64);
const frames: string[] = [];

for (const [index, payload] of payloads.entries()) {
  const envelope: Envelope = {
    v: 1,
    session_id: SESSION_ID,
    seq: index,
    ts_client_ms: 1_700_000_000_000 + index,
    ts_monotonic_ms: index * 100,
    payload,
  };
  frames.push(await signEnvelope(envelope, key));
}

console.log(JSON.stringify({ session_id: SESSION_ID, frames }));
