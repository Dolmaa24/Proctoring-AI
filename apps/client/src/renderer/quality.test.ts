import assert from "node:assert/strict";
import { test } from "node:test";

import { frameQuality, isPoorlyLit } from "./quality.ts";

/** An RGBA buffer filled by a per-pixel function returning 0-255 grey. */
function image(width: number, height: number, shade: (x: number, y: number) => number) {
  const data = new Uint8ClampedArray(width * height * 4);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const i = (y * width + x) * 4;
      const value = shade(x, y);
      data[i] = value;
      data[i + 1] = value;
      data[i + 2] = value;
      data[i + 3] = 255;
    }
  }
  return { data, width, height };
}

const flat = image(32, 32, () => 128);
const checkerboard = image(32, 32, (x, y) => ((x + y) % 2 === 0 ? 0 : 255));
const softGradient = image(32, 32, (x) => Math.round((x / 31) * 255));

test("a flat image has no sharpness", () => {
  const { sharpness } = frameQuality(flat.data, flat.width, flat.height);
  assert.equal(sharpness, 0);
});

test("a hard-edged image is much sharper than a smooth gradient", () => {
  const sharp = frameQuality(checkerboard.data, 32, 32).sharpness;
  const soft = frameQuality(softGradient.data, 32, 32).sharpness;
  assert.ok(sharp > soft, `expected checkerboard (${sharp}) > gradient (${soft})`);
});

test("sharpness stays inside the range the protocol permits", () => {
  // The schema rejects anything outside [0,1], so an unbounded raw
  // variance leaking through here would fail server-side validation and
  // the signal would be dropped entirely.
  for (const img of [flat, checkerboard, softGradient]) {
    const { sharpness, brightness } = frameQuality(img.data, img.width, img.height);
    assert.ok(sharpness >= 0 && sharpness <= 1, `sharpness out of range: ${sharpness}`);
    assert.ok(brightness >= 0 && brightness <= 1, `brightness out of range: ${brightness}`);
  }
});

test("blurring a sharp image lowers its measured sharpness", () => {
  // The property that actually matters: this must respond monotonically to
  // real blur, not just produce some number.
  const blurred = image(32, 32, (x, y) => {
    // A 2x2 box blur of the checkerboard: adjacent opposites average out.
    const a = (x + y) % 2 === 0 ? 0 : 255;
    const b = (x + 1 + y) % 2 === 0 ? 0 : 255;
    return (a + b) / 2;
  });
  const sharp = frameQuality(checkerboard.data, 32, 32).sharpness;
  const soft = frameQuality(blurred.data, 32, 32).sharpness;
  assert.ok(soft < sharp, `blurred (${soft}) should be below sharp (${sharp})`);
});

test("brightness tracks the actual image level", () => {
  assert.ok(Math.abs(frameQuality(flat.data, 32, 32).brightness - 128 / 255) < 0.01);
  const dark = image(32, 32, () => 10);
  assert.ok(frameQuality(dark.data, 32, 32).brightness < 0.1);
});

test("degenerate frame sizes do not throw", () => {
  const tiny = image(2, 2, () => 128);
  assert.deepEqual(frameQuality(tiny.data, 2, 2), { sharpness: 0, brightness: 0 });
});

test("poor lighting is flagged at both ends, not just darkness", () => {
  // A candidate silhouetted against a window is as unreadable as one in
  // the dark, and equally not misconduct.
  assert.equal(isPoorlyLit(0.05), true);
  assert.equal(isPoorlyLit(0.99), true);
  assert.equal(isPoorlyLit(0.5), false);
});
