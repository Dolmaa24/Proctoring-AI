/**
 * Turning MediaPipe Face Landmarker output into gaze and head-pose signals.
 *
 * Pure functions, no MediaPipe imports, so the geometry can be tested
 * against known inputs instead of against a webcam and a volunteer.
 *
 * A calibration caveat that belongs in the code rather than a design doc:
 * the gaze estimate here is *uncalibrated*. It infers direction of regard
 * from where the iris sits between the eye corners, which varies with
 * interpupillary distance, eye shape, glasses, camera placement and how
 * far the candidate sits from the screen. The constants below are a
 * reasonable population average and nothing more.
 *
 * The consequence is that absolute angles should not be trusted to a few
 * degrees, and the shipped policy reflects that: `gaze_off_screen` keys off
 * the coarse `on_screen` boolean with a wide margin and a 2.5s onset,
 * rather than off a precise angle. A real deployment should run a short
 * per-candidate calibration at exam start and store the offsets; without
 * it, expect systematically worse accuracy for anyone whose face differs
 * from the average these constants encode.
 */

export interface Landmark {
  x: number;
  y: number;
  z: number;
}

export interface HeadPose {
  yaw_deg: number;
  pitch_deg: number;
  roll_deg: number;
}

export interface GazeEstimate {
  yaw_deg: number;
  pitch_deg: number;
  on_screen: boolean;
  confidence: number;
}

// MediaPipe Face Landmarker indices (478-point model, refined landmarks on).
export const LEFT_EYE_OUTER = 33;
export const LEFT_EYE_INNER = 133;
export const RIGHT_EYE_INNER = 362;
export const RIGHT_EYE_OUTER = 263;
export const LEFT_IRIS_CENTRE = 468;
export const RIGHT_IRIS_CENTRE = 473;
export const LEFT_EYE_TOP = 159;
export const LEFT_EYE_BOTTOM = 145;
export const RIGHT_EYE_TOP = 386;
export const RIGHT_EYE_BOTTOM = 374;

const RAD_TO_DEG = 180 / Math.PI;

/**
 * Horizontal degrees of regard per unit of normalised iris offset.
 *
 * Offset is measured as (iris centre − eye centre) / eye width, so it runs
 * roughly ±0.5 at the extremes of comfortable eye movement. Empirically
 * that range corresponds to something near ±45°, hence the scale.
 */
const GAZE_YAW_SCALE_DEG = 90;

/** Vertical equivalent. Smaller because eyes travel less vertically. */
const GAZE_PITCH_SCALE_DEG = 60;

/**
 * Combined head + eye deviation tolerated before regard counts as off-screen.
 *
 * Wide on purpose. A typical laptop screen at arm's length subtends about
 * ±15°, but the estimate is uncalibrated and the cost of a false positive
 * is an innocent person flagged, so this sits well outside the screen.
 */
const ON_SCREEN_YAW_LIMIT_DEG = 32;
const ON_SCREEN_PITCH_LIMIT_DEG = 26;

/**
 * Decompose MediaPipe's 4x4 facial transformation matrix into Euler angles.
 *
 * The matrix arrives column-major. Angles use the RzRyRx convention:
 * yaw about Y (turning left/right), pitch about X (nodding), roll about Z
 * (tilting). Positive yaw is the candidate's head turning to their left.
 */
export function headPoseFromMatrix(matrix: readonly number[]): HeadPose {
  if (matrix.length !== 16) {
    throw new Error(`expected a 4x4 matrix, got ${matrix.length} elements`);
  }

  // Column-major to row-major element access: R[row][col] === m[col*4 + row]
  const r = (row: number, col: number) => matrix[col * 4 + row];

  const r00 = r(0, 0);
  const r10 = r(1, 0);
  const r20 = r(2, 0);
  const r21 = r(2, 1);
  const r22 = r(2, 2);

  const sy = Math.hypot(r21, r22);

  // Gimbal lock: looking near-vertically, roll and yaw become degenerate.
  // Report roll as zero rather than emitting a wildly unstable value.
  if (sy < 1e-6) {
    return {
      yaw_deg: Math.atan2(-r20, sy) * RAD_TO_DEG,
      pitch_deg: Math.atan2(-r21, r22) * RAD_TO_DEG,
      roll_deg: 0,
    };
  }

  return {
    yaw_deg: Math.atan2(-r20, sy) * RAD_TO_DEG,
    pitch_deg: Math.atan2(r21, r22) * RAD_TO_DEG,
    roll_deg: Math.atan2(r10, r00) * RAD_TO_DEG,
  };
}

function midpoint(a: Landmark, b: Landmark): { x: number; y: number } {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

/**
 * Vertical eye opening relative to width — the standard blink ratio.
 *
 * Used as a confidence gate rather than a blink detector per se: when the
 * lids are closed the iris landmarks are meaningless, and reporting a
 * confident gaze estimate from them would be a fabrication. The fusion
 * engine treats low confidence as absence of information, which is exactly
 * right for a blink.
 */
export function eyeOpenness(
  outer: Landmark,
  inner: Landmark,
  top: Landmark,
  bottom: Landmark,
): number {
  const width = Math.hypot(outer.x - inner.x, outer.y - inner.y);
  if (width < 1e-9) return 0;
  return Math.hypot(top.x - bottom.x, top.y - bottom.y) / width;
}

const EYE_CLOSED_RATIO = 0.15;

/**
 * Estimate direction of regard from iris position and head pose.
 *
 * Gaze is head pose plus eye-in-head rotation: someone facing forward with
 * their eyes hard left, and someone whose whole head is turned left, are
 * both looking away, and the signal has to capture each.
 */
export function estimateGaze(
  landmarks: readonly Landmark[],
  headPose: HeadPose,
): GazeEstimate {
  const required = Math.max(RIGHT_IRIS_CENTRE, RIGHT_EYE_BOTTOM);
  if (landmarks.length <= required) {
    // The 468-point model without refined landmarks has no iris points.
    // Fall back to head pose alone rather than inventing an eye estimate.
    return {
      yaw_deg: clampAngle(headPose.yaw_deg),
      pitch_deg: clampAngle(headPose.pitch_deg),
      on_screen: withinScreen(headPose.yaw_deg, headPose.pitch_deg),
      confidence: 0.4,
    };
  }

  const leftOuter = landmarks[LEFT_EYE_OUTER];
  const leftInner = landmarks[LEFT_EYE_INNER];
  const rightInner = landmarks[RIGHT_EYE_INNER];
  const rightOuter = landmarks[RIGHT_EYE_OUTER];

  const leftOpen = eyeOpenness(
    leftOuter,
    leftInner,
    landmarks[LEFT_EYE_TOP],
    landmarks[LEFT_EYE_BOTTOM],
  );
  const rightOpen = eyeOpenness(
    rightOuter,
    rightInner,
    landmarks[RIGHT_EYE_TOP],
    landmarks[RIGHT_EYE_BOTTOM],
  );

  const leftOffset = irisOffset(landmarks[LEFT_IRIS_CENTRE], leftOuter, leftInner);
  const rightOffset = irisOffset(landmarks[RIGHT_IRIS_CENTRE], rightInner, rightOuter);

  const eyeYaw = ((leftOffset.x + rightOffset.x) / 2) * GAZE_YAW_SCALE_DEG;
  const eyePitch = ((leftOffset.y + rightOffset.y) / 2) * GAZE_PITCH_SCALE_DEG;

  const yaw = headPose.yaw_deg + eyeYaw;
  const pitch = headPose.pitch_deg + eyePitch;

  const closed = Math.min(leftOpen, rightOpen) < EYE_CLOSED_RATIO;

  return {
    yaw_deg: clampAngle(yaw),
    pitch_deg: clampAngle(pitch),
    on_screen: withinScreen(yaw, pitch),
    // A blink is not evidence of anything. Reporting it at low confidence
    // lets the fusion engine hold its state rather than reset a timer.
    confidence: closed ? 0.1 : 0.9,
  };
}

function irisOffset(
  iris: Landmark,
  cornerA: Landmark,
  cornerB: Landmark,
): { x: number; y: number } {
  const centre = midpoint(cornerA, cornerB);
  const width = Math.hypot(cornerA.x - cornerB.x, cornerA.y - cornerB.y);
  if (width < 1e-9) return { x: 0, y: 0 };
  return { x: (iris.x - centre.x) / width, y: (iris.y - centre.y) / width };
}

function withinScreen(yaw: number, pitch: number): boolean {
  return (
    Math.abs(yaw) <= ON_SCREEN_YAW_LIMIT_DEG &&
    Math.abs(pitch) <= ON_SCREEN_PITCH_LIMIT_DEG
  );
}

/** The protocol constrains gaze angles to ±90°. */
function clampAngle(degrees: number): number {
  return Math.max(-90, Math.min(90, degrees));
}
