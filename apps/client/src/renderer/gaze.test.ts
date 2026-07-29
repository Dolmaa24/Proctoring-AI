/**
 * Run: node --experimental-strip-types --test apps/client/src/renderer/gaze.test.ts
 *
 * Head-pose angles are checked against matrices built from known rotations,
 * so the Euler convention is pinned rather than assumed. Getting this wrong
 * is silent: yaw and roll swapped still produces plausible-looking numbers.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  type Landmark,
  LEFT_EYE_BOTTOM,
  LEFT_EYE_INNER,
  LEFT_EYE_OUTER,
  LEFT_EYE_TOP,
  LEFT_IRIS_CENTRE,
  RIGHT_EYE_BOTTOM,
  RIGHT_EYE_INNER,
  RIGHT_EYE_OUTER,
  RIGHT_EYE_TOP,
  RIGHT_IRIS_CENTRE,
  estimateGaze,
  eyeOpenness,
  headPoseFromMatrix,
} from "./gaze.ts";

/** Build a column-major 4x4 from intrinsic Z(roll) Y(yaw) X(pitch) rotations. */
function matrixFrom(yawDeg: number, pitchDeg: number, rollDeg: number): number[] {
  const [y, p, r] = [yawDeg, pitchDeg, rollDeg].map((d) => (d * Math.PI) / 180);
  const [cy, sy] = [Math.cos(y), Math.sin(y)];
  const [cp, sp] = [Math.cos(p), Math.sin(p)];
  const [cr, sr] = [Math.cos(r), Math.sin(r)];

  // R = Rz(roll) * Ry(yaw) * Rx(pitch), row-major.
  const R = [
    [cr * cy, cr * sy * sp - sr * cp, cr * sy * cp + sr * sp],
    [sr * cy, sr * sy * sp + cr * cp, sr * sy * cp - cr * sp],
    [-sy, cy * sp, cy * cp],
  ];

  const m = new Array(16).fill(0);
  for (let row = 0; row < 3; row++) {
    for (let col = 0; col < 3; col++) m[col * 4 + row] = R[row][col];
  }
  m[15] = 1;
  return m;
}

const close = (actual: number, expected: number, tolerance = 1e-6) =>
  assert.ok(
    Math.abs(actual - expected) < tolerance,
    `expected ${expected}, got ${actual}`,
  );

test("identity matrix is a level, forward-facing head", () => {
  const pose = headPoseFromMatrix(matrixFrom(0, 0, 0));
  close(pose.yaw_deg, 0);
  close(pose.pitch_deg, 0);
  close(pose.roll_deg, 0);
});

test("recovers each axis independently", () => {
  const yawOnly = headPoseFromMatrix(matrixFrom(30, 0, 0));
  close(yawOnly.yaw_deg, 30, 1e-4);
  close(yawOnly.pitch_deg, 0, 1e-4);
  close(yawOnly.roll_deg, 0, 1e-4);

  const pitchOnly = headPoseFromMatrix(matrixFrom(0, -20, 0));
  close(pitchOnly.pitch_deg, -20, 1e-4);
  close(pitchOnly.yaw_deg, 0, 1e-4);

  const rollOnly = headPoseFromMatrix(matrixFrom(0, 0, 15));
  close(rollOnly.roll_deg, 15, 1e-4);
  close(rollOnly.yaw_deg, 0, 1e-4);
});

test("recovers combined rotations", () => {
  const pose = headPoseFromMatrix(matrixFrom(25, -12, 8));
  close(pose.yaw_deg, 25, 1e-4);
  close(pose.pitch_deg, -12, 1e-4);
  close(pose.roll_deg, 8, 1e-4);
});

test("does not produce NaN at near-vertical gimbal lock", () => {
  const pose = headPoseFromMatrix(matrixFrom(90, 0, 0));
  assert.ok(Number.isFinite(pose.yaw_deg));
  assert.ok(Number.isFinite(pose.pitch_deg));
  assert.ok(Number.isFinite(pose.roll_deg));
  close(pose.roll_deg, 0);
});

test("rejects a malformed matrix rather than guessing", () => {
  assert.throws(() => headPoseFromMatrix([1, 0, 0, 1]), /4x4/);
});

// -- gaze -------------------------------------------------------------------

/** 478 landmarks with both irises centred and eyes open. */
function neutralFace(): Landmark[] {
  const points: Landmark[] = Array.from({ length: 478 }, () => ({
    x: 0.5,
    y: 0.5,
    z: 0,
  }));

  points[LEFT_EYE_OUTER] = { x: 0.35, y: 0.45, z: 0 };
  points[LEFT_EYE_INNER] = { x: 0.45, y: 0.45, z: 0 };
  points[LEFT_EYE_TOP] = { x: 0.4, y: 0.42, z: 0 };
  points[LEFT_EYE_BOTTOM] = { x: 0.4, y: 0.48, z: 0 };
  points[LEFT_IRIS_CENTRE] = { x: 0.4, y: 0.45, z: 0 };

  points[RIGHT_EYE_INNER] = { x: 0.55, y: 0.45, z: 0 };
  points[RIGHT_EYE_OUTER] = { x: 0.65, y: 0.45, z: 0 };
  points[RIGHT_EYE_TOP] = { x: 0.6, y: 0.42, z: 0 };
  points[RIGHT_EYE_BOTTOM] = { x: 0.6, y: 0.48, z: 0 };
  points[RIGHT_IRIS_CENTRE] = { x: 0.6, y: 0.45, z: 0 };

  return points;
}

const LEVEL = { yaw_deg: 0, pitch_deg: 0, roll_deg: 0 };

test("centred irises on a level head read as on-screen", () => {
  const gaze = estimateGaze(neutralFace(), LEVEL);
  close(gaze.yaw_deg, 0, 1e-9);
  assert.equal(gaze.on_screen, true);
  assert.ok(gaze.confidence > 0.5);
});

test("eyes at their travel limit read as off-screen even with a level head", () => {
  const points = neutralFace();
  // Iris travel tops out near half the eye width; 0.45 is a hard look aside.
  points[LEFT_IRIS_CENTRE] = { x: 0.4 - 0.045, y: 0.45, z: 0 };
  points[RIGHT_IRIS_CENTRE] = { x: 0.6 - 0.045, y: 0.45, z: 0 };

  const gaze = estimateGaze(points, LEVEL);
  assert.ok(gaze.yaw_deg < -35, `expected a strong leftward estimate, got ${gaze.yaw_deg}`);
  assert.equal(gaze.on_screen, false);
});

test("a moderate eye glance is deliberately tolerated", () => {
  // A third of eye width is roughly 30 degrees of regard — past the edge of
  // a laptop screen, but inside the on-screen limit on purpose. The estimate
  // is uncalibrated (see gaze.ts), and a candidate glancing across a wide
  // monitor must not be flagged on the strength of a population-average
  // constant. This test exists so that tightening the limit is a conscious
  // decision with a visible cost, rather than a silent tuning change.
  const points = neutralFace();
  points[LEFT_IRIS_CENTRE] = { x: 0.4 - 0.033, y: 0.45, z: 0 };
  points[RIGHT_IRIS_CENTRE] = { x: 0.6 - 0.033, y: 0.45, z: 0 };

  const gaze = estimateGaze(points, LEVEL);
  assert.ok(gaze.yaw_deg < -25, "the deviation is still measured and reported");
  assert.equal(gaze.on_screen, true, "but it does not on its own count as looking away");
});

test("a turned head reads as off-screen even with centred eyes", () => {
  const gaze = estimateGaze(neutralFace(), { yaw_deg: 45, pitch_deg: 0, roll_deg: 0 });
  assert.equal(gaze.on_screen, false);
});

test("head and eye rotation compose rather than cancel", () => {
  const points = neutralFace();
  points[LEFT_IRIS_CENTRE] = { x: 0.4 + 0.02, y: 0.45, z: 0 };
  points[RIGHT_IRIS_CENTRE] = { x: 0.6 + 0.02, y: 0.45, z: 0 };

  const headOnly = estimateGaze(neutralFace(), { yaw_deg: 20, pitch_deg: 0, roll_deg: 0 });
  const both = estimateGaze(points, { yaw_deg: 20, pitch_deg: 0, roll_deg: 0 });
  assert.ok(both.yaw_deg > headOnly.yaw_deg, "eye offset must add to head yaw");
});

test("a small glance stays on-screen", () => {
  const points = neutralFace();
  points[LEFT_IRIS_CENTRE] = { x: 0.4 + 0.005, y: 0.45, z: 0 };
  points[RIGHT_IRIS_CENTRE] = { x: 0.6 + 0.005, y: 0.45, z: 0 };
  const gaze = estimateGaze(points, { yaw_deg: 5, pitch_deg: 0, roll_deg: 0 });
  assert.equal(gaze.on_screen, true, "reading across a screen is not looking away");
});

test("a blink is reported at low confidence, not as a gaze reading", () => {
  const points = neutralFace();
  points[LEFT_EYE_TOP] = { x: 0.4, y: 0.4498, z: 0 };
  points[LEFT_EYE_BOTTOM] = { x: 0.4, y: 0.4502, z: 0 };

  const gaze = estimateGaze(points, LEVEL);
  assert.ok(
    gaze.confidence < 0.5,
    "closed lids make the iris landmarks meaningless; the estimate must say so",
  );
});

test("falls back to head pose when the model has no iris landmarks", () => {
  const coarse = neutralFace().slice(0, 468);
  const gaze = estimateGaze(coarse, { yaw_deg: 50, pitch_deg: 0, roll_deg: 0 });
  assert.equal(gaze.on_screen, false);
  assert.ok(gaze.confidence < 0.5, "a head-pose-only estimate must not claim iris accuracy");
});

test("angles stay inside the range the protocol permits", () => {
  const points = neutralFace();
  points[LEFT_IRIS_CENTRE] = { x: 0.4 - 0.5, y: 0.45, z: 0 };
  points[RIGHT_IRIS_CENTRE] = { x: 0.6 - 0.5, y: 0.45, z: 0 };
  const gaze = estimateGaze(points, { yaw_deg: -80, pitch_deg: 0, roll_deg: 0 });
  assert.ok(gaze.yaw_deg >= -90 && gaze.yaw_deg <= 90);
});

test("eye openness is scale invariant", () => {
  const small = eyeOpenness(
    { x: 0, y: 0, z: 0 },
    { x: 0.1, y: 0, z: 0 },
    { x: 0.05, y: 0.03, z: 0 },
    { x: 0.05, y: 0, z: 0 },
  );
  const large = eyeOpenness(
    { x: 0, y: 0, z: 0 },
    { x: 1, y: 0, z: 0 },
    { x: 0.5, y: 0.3, z: 0 },
    { x: 0.5, y: 0, z: 0 },
  );
  close(small, large, 1e-9);
});
