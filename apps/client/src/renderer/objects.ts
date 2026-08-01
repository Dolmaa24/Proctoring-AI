/**
 * Object detection: mapping a COCO detector's output onto `ObjectLabel`.
 *
 * The mapping function is pure and lives here with no MediaPipe import, so
 * it can be tested against known detector output rather than against a
 * webcam and a phone. `createObjectDetector` is the only part that touches
 * MediaPipe.
 *
 * Why EfficientDet-Lite and not YOLO
 * ----------------------------------
 * Ultralytics YOLO is AGPL-3.0, and that reaches network-deployed services
 * — ARCHITECTURE.md § 7 rules it out, which is why this project shipped
 * with object rules in the policy but nothing emitting `signal.object`.
 * EfficientDet-Lite0 is Apache-2.0 and runs in the MediaPipe WASM runtime
 * already vendored for the face landmarker, so object detection costs one
 * model file and no new licence obligations.
 *
 * What this model cannot see, stated plainly
 * ------------------------------------------
 * COCO-80 has `cell phone`, `person`, and `book`. It has **no smartwatch
 * class and no headphones class**. The `wearable_detected` rule in
 * policies/default.yaml therefore cannot fire from this detector — it is
 * not disabled, it simply will never receive a matching signal until a
 * detector trained on those classes is supplied. Leaving the rule in place
 * with nothing feeding it is deliberate: removing it would hide the gap,
 * and a reviewer reading the policy should be able to see what is intended
 * as well as what currently works.
 *
 * That claim is enforced, not just written down. `DETECTABLE_LABELS` and
 * `UNDETECTABLE_LABELS` in python/proctor_protocol/events.py name the
 * split; the tests beside this file assert this mapping matches the
 * former, and python/tests/test_policy_object_labels.py asserts the
 * policy never quietly grows a second inert rule. A comment on its own
 * would have gone stale the first time someone swapped the model.
 */

import { FilesetResolver, ObjectDetector, type Detection } from "@mediapipe/tasks-vision";

import type { BoundingBox, ObjectLabel, ObjectSignal } from "../protocol/events.generated.ts";

/**
 * COCO category name -> the closed `ObjectLabel` set.
 *
 * Deliberately not a fuzzy or substring match. The protocol's enum is a
 * closed set precisely so the fusion engine cannot grow rules for classes
 * the model was never evaluated on, and a loose mapping here would defeat
 * that by quietly admitting whatever COCO happens to call something.
 */
export const COCO_TO_LABEL: Record<string, ObjectLabel> = {
  "cell phone": "phone",
  person: "person",
  book: "book",
};

/** Detections below this are not reported at all. The policy applies its
 * own, higher thresholds per rule; this only keeps obvious noise off the
 * wire. */
const REPORT_FLOOR = 0.4;

export interface DetectorFrame {
  width: number;
  height: number;
}

function toBoundingBox(detection: Detection, frame: DetectorFrame): BoundingBox | null {
  const box = detection.boundingBox;
  if (!box || frame.width <= 0 || frame.height <= 0) return null;

  // MediaPipe reports pixels; the protocol wants [0,1] normalised against
  // the frame, origin top-left. Clamped because a detector can return a
  // box that runs marginally past the edge, and the schema rejects that.
  const x = Math.min(Math.max(box.originX / frame.width, 0), 1);
  const y = Math.min(Math.max(box.originY / frame.height, 0), 1);
  const w = Math.min(Math.max(box.width / frame.width, Number.MIN_VALUE), 1 - x || Number.MIN_VALUE);
  const h = Math.min(
    Math.max(box.height / frame.height, Number.MIN_VALUE),
    1 - y || Number.MIN_VALUE,
  );
  return { x, y, w, h };
}

/**
 * Map raw detections to signals, keeping only the highest-confidence
 * detection per label.
 *
 * One signal per label per frame, not one per detection: three phones in
 * frame is not three times the evidence that a phone is present, and
 * emitting three would inflate the fusion engine's sample counts and the
 * triage score for what a reviewer sees as a single fact.
 *
 * `person` is a documented exception in how it is *read* downstream, not
 * here: the candidate themselves is a person, so `second_person_detected`
 * keys off the count. See `detectionsToSignals`'s caller in index.ts.
 */
export function detectionsToSignals(
  detections: Detection[],
  frame: DetectorFrame,
): ObjectSignal[] {
  const best = new Map<ObjectLabel, { confidence: number; bbox: BoundingBox | null }>();

  for (const detection of detections) {
    const category = detection.categories?.[0];
    if (!category?.categoryName) continue;

    const label = COCO_TO_LABEL[category.categoryName];
    if (!label) continue;

    const confidence = category.score ?? 0;
    if (confidence < REPORT_FLOOR) continue;

    const existing = best.get(label);
    if (existing && existing.confidence >= confidence) continue;
    best.set(label, { confidence, bbox: toBoundingBox(detection, frame) });
  }

  return [...best.entries()].map(([label, { confidence, bbox }]) => ({
    type: "signal.object" as const,
    label,
    confidence,
    bbox,
  }));
}

/** How many people the detector sees, before any policy is applied. */
export function personCount(detections: Detection[], floor = 0.5): number {
  return detections.filter(
    (d) => d.categories?.[0]?.categoryName === "person" && (d.categories[0].score ?? 0) >= floor,
  ).length;
}

export async function createObjectDetector(): Promise<ObjectDetector> {
  const fileset = await FilesetResolver.forVisionTasks("./models/wasm");
  return ObjectDetector.createFromOptions(fileset, {
    baseOptions: {
      modelAssetPath: "./models/efficientdet_lite0.tflite",
      delegate: "GPU",
    },
    runningMode: "VIDEO",
    scoreThreshold: REPORT_FLOOR,
    maxResults: 8,
  });
}
