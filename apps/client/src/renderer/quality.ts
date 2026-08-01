/**
 * Frame quality: is the camera actually showing us a usable picture?
 *
 * Pure functions over raw pixel data, so sharpness and brightness can be
 * tested against synthetic images instead of a smeared lens and a
 * volunteer.
 *
 * Why this signal exists at all
 * ----------------------------
 * Every other visual signal in this client degrades silently when the
 * camera is bad. A blurred or dark frame lowers gaze confidence, loses
 * iris landmarks, and drops face detections — which, read without
 * context, looks exactly like a candidate turning away or leaving. Making
 * frame quality its own reported observation lets the server distinguish
 * "we could not see" from "they did something", which is the difference
 * between an equipment problem and an accusation.
 *
 * It is deliberately *not* framed as tamper detection. A candidate with a
 * cheap webcam in a dim room is the common case; someone smearing the lens
 * on purpose is the rare one, and this code cannot tell them apart. The
 * policy rule it feeds is worded accordingly.
 */

/** Grayscale luma from RGBA bytes, ITU-R BT.601 coefficients. */
function luma(data: Uint8ClampedArray, index: number): number {
  return 0.299 * data[index] + 0.587 * data[index + 1] + 0.114 * data[index + 2];
}

export interface FrameQuality {
  sharpness: number;
  brightness: number;
}

/**
 * Normalised variance-of-Laplacian, the standard cheap blur estimate.
 *
 * The 4-neighbour Laplacian responds to local intensity change; its
 * variance across the image is high for crisp edges and collapses toward
 * zero for a smeared or flat image. The result is squashed through a
 * saturating curve into [0,1] because the protocol requires that range and
 * because the raw variance has no upper bound and no physical meaning —
 * it is only ever compared against a threshold.
 *
 * `SATURATION` is the raw variance that maps to ~0.5. It was chosen so
 * that ordinary webcam footage lands comfortably above the policy
 * threshold and heavy blur lands below it; it is not calibrated to any
 * standard, and a deployment with unusual cameras should re-check it
 * rather than trust the constant.
 */
const SATURATION = 250;

export function frameQuality(
  data: Uint8ClampedArray,
  width: number,
  height: number,
): FrameQuality {
  if (width < 3 || height < 3) return { sharpness: 0, brightness: 0 };

  let sum = 0;
  let sumSq = 0;
  let brightnessSum = 0;
  let count = 0;

  for (let y = 1; y < height - 1; y++) {
    for (let x = 1; x < width - 1; x++) {
      const i = (y * width + x) * 4;
      const centre = luma(data, i);
      const laplacian =
        4 * centre -
        luma(data, i - 4) -
        luma(data, i + 4) -
        luma(data, i - width * 4) -
        luma(data, i + width * 4);

      sum += laplacian;
      sumSq += laplacian * laplacian;
      brightnessSum += centre;
      count++;
    }
  }

  if (count === 0) return { sharpness: 0, brightness: 0 };

  const mean = sum / count;
  const variance = Math.max(0, sumSq / count - mean * mean);

  return {
    // x / (x + k) saturates smoothly to 1 and is monotonic, so ordering is
    // preserved even where the absolute value is not meaningful.
    sharpness: variance / (variance + SATURATION),
    brightness: brightnessSum / count / 255,
  };
}

/**
 * Whether the frame is too dark or too washed out to read a face from.
 *
 * Separate from sharpness because the failure modes are different and a
 * reviewer benefits from knowing which one happened: a backlit candidate
 * silhouetted against a window is a lighting problem, a smeared lens is
 * not, and neither is misconduct.
 */
export function isPoorlyLit(brightness: number): boolean {
  return brightness < 0.12 || brightness > 0.95;
}
