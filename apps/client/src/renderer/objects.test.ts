import assert from "node:assert/strict";
import { test } from "node:test";

import { COCO_TO_LABEL, detectionsToSignals, personCount } from "./objects.ts";

type Detection = Parameters<typeof detectionsToSignals>[0][number];

const FRAME = { width: 640, height: 480 };

function detection(name: string, score: number, box?: Partial<Detection["boundingBox"]>) {
  return {
    categories: [{ categoryName: name, score, index: 0, displayName: "" }],
    boundingBox: box
      ? { originX: 0, originY: 0, width: 100, height: 100, angle: 0, ...box }
      : undefined,
    keypoints: [],
  } as unknown as Detection;
}

test("a detected phone becomes a phone signal", () => {
  const signals = detectionsToSignals([detection("cell phone", 0.91)], FRAME);
  assert.equal(signals.length, 1);
  assert.equal(signals[0].label, "phone");
  assert.equal(signals[0].type, "signal.object");
  assert.equal(signals[0].confidence, 0.91);
});

test("COCO classes outside the closed enum are dropped, not guessed at", () => {
  // The protocol's ObjectLabel is a closed set on purpose. A detector that
  // sees a laptop must not have it coerced into some adjacent label.
  const signals = detectionsToSignals(
    [detection("laptop", 0.99), detection("tv", 0.99), detection("toothbrush", 0.99)],
    FRAME,
  );
  assert.deepEqual(signals, []);
});

test("low-confidence detections are not put on the wire at all", () => {
  assert.deepEqual(detectionsToSignals([detection("cell phone", 0.2)], FRAME), []);
});

test("several detections of one label collapse to the most confident", () => {
  // Three phones in frame is not three times the evidence that a phone is
  // present; emitting three would inflate the fusion engine's sample count
  // and the triage score for a single reviewable fact.
  const signals = detectionsToSignals(
    [detection("cell phone", 0.55), detection("cell phone", 0.93), detection("cell phone", 0.71)],
    FRAME,
  );
  assert.equal(signals.length, 1);
  assert.equal(signals[0].confidence, 0.93);
});

test("distinct labels each produce their own signal", () => {
  const signals = detectionsToSignals(
    [detection("cell phone", 0.8), detection("person", 0.9), detection("book", 0.7)],
    FRAME,
  );
  assert.deepEqual(new Set(signals.map((s) => s.label)), new Set(["phone", "person", "book"]));
});

test("bounding boxes are normalised to the [0,1] range the schema requires", () => {
  const signals = detectionsToSignals(
    [detection("cell phone", 0.8, { originX: 320, originY: 240, width: 160, height: 120 })],
    FRAME,
  );
  assert.deepEqual(signals[0].bbox, { x: 0.5, y: 0.5, w: 0.25, h: 0.25 });
});

test("a box running past the frame edge is clamped rather than rejected", () => {
  // Detectors do return boxes that overhang the edge. The Pydantic schema
  // rejects out-of-range values, so an unclamped box would fail server-side
  // validation and the whole signal would be lost.
  const signals = detectionsToSignals(
    [detection("cell phone", 0.8, { originX: 600, originY: 460, width: 400, height: 400 })],
    FRAME,
  );
  const bbox = signals[0].bbox!;
  assert.ok(bbox.x >= 0 && bbox.x <= 1, `x out of range: ${bbox.x}`);
  assert.ok(bbox.y >= 0 && bbox.y <= 1, `y out of range: ${bbox.y}`);
  assert.ok(bbox.w > 0 && bbox.x + bbox.w <= 1, `w out of range: ${bbox.w}`);
  assert.ok(bbox.h > 0 && bbox.y + bbox.h <= 1, `h out of range: ${bbox.h}`);
});

test("a detection with no bounding box still reports the label", () => {
  const signals = detectionsToSignals([detection("cell phone", 0.8)], FRAME);
  assert.equal(signals[0].bbox, null);
});

test("personCount counts only confident people", () => {
  const detections = [
    detection("person", 0.9),
    detection("person", 0.8),
    detection("person", 0.1),
    detection("cell phone", 0.9),
  ];
  assert.equal(personCount(detections), 2);
});

/**
 * The other half of python/tests/test_policy_object_labels.py.
 *
 * That suite asserts the shipped policy never matches on a label nothing
 * can emit; this one pins what this detector actually emits, so the two
 * cannot drift apart silently. `smartwatch` and `headphones` are absent
 * on purpose — COCO-80 has no such classes — and `wearable_detected` in
 * policies/default.yaml is inert as a result. Adding either here without
 * a detector that genuinely produces it would make that policy rule look
 * live while still never firing.
 */
test("the COCO mapping emits exactly the labels declared detectable", () => {
  assert.deepEqual(
    [...new Set(Object.values(COCO_TO_LABEL))].sort(),
    ["book", "person", "phone"],
    "keep in step with DETECTABLE_LABELS in python/proctor_protocol/events.py",
  );
});

test("no COCO category maps to a wearable label", () => {
  const wearables = new Set(["smartwatch", "headphones"]);
  const mapped = Object.entries(COCO_TO_LABEL).filter(([, label]) => wearables.has(label));
  assert.deepEqual(
    mapped,
    [],
    "COCO-80 has no smartwatch or headphones class; a mapping here would be a guess",
  );
});
