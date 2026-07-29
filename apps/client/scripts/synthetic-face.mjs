/**
 * Draw a synthetic face straight into a Y4M video fixture.
 *
 * Purpose: exercise the *face-present* branch of the renderer end-to-end —
 * `facialTransformationMatrixes` arriving in the shape the code expects,
 * landmark indices lining up with the constants in gaze.ts. Those are
 * integration risks in our code, not in MediaPipe.
 *
 * Procedural rather than photographic, deliberately. A proctoring repo
 * should not carry a scraped photograph of an identifiable person as a
 * test fixture, and it does not need to: pixels are computed here, so no
 * real person's biometrics are involved and nothing has to be committed.
 *
 * Whether MediaPipe actually detects a drawn face is not guaranteed — it
 * is trained on photographs. If it does not, that is a limitation of the
 * fixture, not a failure of the client, and the e2e output says so.
 *
 *   node scripts/synthetic-face.mjs [out.y4m]
 */

import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const W = 640;
const H = 480;
const FPS = 30;
const FRAMES = 120;

const clientRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const outPath = path.resolve(process.argv[2] ?? path.join(clientRoot, "fixtures", "face.y4m"));

const rgb = new Uint8ClampedArray(W * H * 3);

const put = (x, y, r, g, b) => {
  if (x < 0 || y < 0 || x >= W || y >= H) return;
  const i = (y * W + x) * 3;
  rgb[i] = r;
  rgb[i + 1] = g;
  rgb[i + 2] = b;
};

const blend = (x, y, r, g, b, alpha) => {
  if (x < 0 || y < 0 || x >= W || y >= H || alpha <= 0) return;
  const i = (y * W + x) * 3;
  rgb[i] = rgb[i] * (1 - alpha) + r * alpha;
  rgb[i + 1] = rgb[i + 1] * (1 - alpha) + g * alpha;
  rgb[i + 2] = rgb[i + 2] * (1 - alpha) + b * alpha;
};

/** Soft-edged filled ellipse, so features do not alias into hard blocks. */
function ellipse(cx, cy, rx, ry, [r, g, b], softness = 1.5) {
  for (let y = Math.floor(cy - ry - 2); y <= cy + ry + 2; y++) {
    for (let x = Math.floor(cx - rx - 2); x <= cx + rx + 2; x++) {
      const d = ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2;
      if (d > 1 + softness / rx) continue;
      const edge = (1 + softness / rx - d) / (softness / rx);
      blend(x, y, r, g, b, Math.min(1, Math.max(0, edge)));
    }
  }
}

// Background: a plain, slightly uneven wall.
for (let y = 0; y < H; y++) {
  for (let x = 0; x < W; x++) {
    const shade = 176 + Math.sin(x / 90) * 5 + Math.cos(y / 110) * 4;
    put(x, y, shade, shade - 3, shade - 9);
  }
}

const CX = W / 2;
const CY = H / 2 + 10;
const SKIN = [222, 184, 152];
const SKIN_SHADOW = [198, 158, 128];

// Neck and shoulders, so the head is not a floating oval.
ellipse(CX, CY + 165, 60, 70, SKIN_SHADOW);
ellipse(CX, CY + 250, 210, 110, [70, 84, 110]);

// Head: forehead slightly wider than jaw.
ellipse(CX, CY - 6, 108, 138, SKIN);
ellipse(CX, CY + 78, 88, 66, SKIN);
// Ears
ellipse(CX - 108, CY + 6, 16, 30, SKIN_SHADOW);
ellipse(CX + 108, CY + 6, 16, 30, SKIN_SHADOW);
// Hair
ellipse(CX, CY - 96, 112, 74, [58, 42, 34]);
ellipse(CX - 96, CY - 40, 24, 66, [58, 42, 34]);
ellipse(CX + 96, CY - 40, 24, 66, [58, 42, 34]);

// Brow shadow gives the detector vertical structure to latch onto.
ellipse(CX - 42, CY - 44, 34, 9, [150, 116, 92]);
ellipse(CX + 42, CY - 44, 34, 9, [150, 116, 92]);
// Eyebrows
ellipse(CX - 42, CY - 52, 33, 7, [64, 46, 36]);
ellipse(CX + 42, CY - 52, 33, 7, [64, 46, 36]);

// Eyes: sclera, iris, pupil, catchlight. Centred irises = gaze forward.
for (const sign of [-1, 1]) {
  const ex = CX + sign * 42;
  const ey = CY - 22;
  ellipse(ex, ey, 27, 14, [246, 244, 240]);
  ellipse(ex, ey, 12, 12, [86, 106, 74]);
  ellipse(ex, ey, 6, 6, [22, 18, 16]);
  ellipse(ex - 3, ey - 4, 2.5, 2.5, [255, 255, 255], 0.6);
  // Upper lid line: the strongest single cue for eye landmarks.
  ellipse(ex, ey - 13, 27, 3, [126, 96, 78]);
}

// Nose: bridge shadow, tip highlight, nostrils.
ellipse(CX - 9, CY + 6, 5, 40, [206, 168, 138]);
ellipse(CX, CY + 34, 20, 15, SKIN);
ellipse(CX, CY + 40, 22, 9, [204, 164, 134]);
ellipse(CX - 10, CY + 42, 5, 3.5, [140, 104, 84]);
ellipse(CX + 10, CY + 42, 5, 3.5, [140, 104, 84]);

// Mouth
ellipse(CX, CY + 84, 36, 13, [188, 118, 110]);
ellipse(CX, CY + 84, 34, 2.5, [120, 66, 62]);
// Chin shading
ellipse(CX, CY + 116, 30, 12, [208, 170, 140]);

// -- RGB -> I420 -----------------------------------------------------------

const ySize = W * H;
const cSize = (W / 2) * (H / 2);
const Y = Buffer.alloc(ySize);
const U = Buffer.alloc(cSize);
const V = Buffer.alloc(cSize);
const clamp = (v) => Math.max(0, Math.min(255, Math.round(v)));

for (let y = 0; y < H; y++) {
  for (let x = 0; x < W; x++) {
    const i = (y * W + x) * 3;
    const [r, g, b] = [rgb[i], rgb[i + 1], rgb[i + 2]];
    Y[y * W + x] = clamp(0.257 * r + 0.504 * g + 0.098 * b + 16);
    if (y % 2 === 0 && x % 2 === 0) {
      const c = (y / 2) * (W / 2) + x / 2;
      U[c] = clamp(-0.148 * r - 0.291 * g + 0.439 * b + 128);
      V[c] = clamp(0.439 * r - 0.368 * g - 0.071 * b + 128);
    }
  }
}

const chunks = [Buffer.from(`YUV4MPEG2 W${W} H${H} F${FPS}:1 Ip A1:1 C420\n`)];
const marker = Buffer.from("FRAME\n");
for (let i = 0; i < FRAMES; i++) chunks.push(marker, Y, U, V);

const data = Buffer.concat(chunks);
await mkdir(path.dirname(outPath), { recursive: true });
await writeFile(outPath, data);
console.log(`wrote ${outPath} (${W}x${H}, ${FRAMES} frames, ${(data.length / 1e6).toFixed(1)} MB)`);
