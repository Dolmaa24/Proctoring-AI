/**
 * Renderer: camera capture and edge inference.
 *
 * Runs the models locally and reports *observations* over IPC. It never
 * decides anything — no thresholds, no verdicts — because policy lives on
 * the server where it can be retuned and where a tampered client cannot
 * suppress it. See ARCHITECTURE.md § 2.
 *
 * Detection runs as fast as the hardware allows; telemetry is downsampled
 * to 10Hz. Sending every frame's landmarks would put the video bitrate
 * back on the telemetry path and defeat the point of edge inference.
 */

import {
  FaceLandmarker,
  FilesetResolver,
  type FaceLandmarkerResult,
} from "@mediapipe/tasks-vision";

import type { FaceSignal, GazeSignal, HeadPoseSignal } from "../protocol/events.generated.ts";
import { estimateGaze, headPoseFromMatrix, type Landmark } from "./gaze.ts";

const TELEMETRY_INTERVAL_MS = 100;
const MAX_FACES = 4;

let landmarker: FaceLandmarker | null = null;
let lastEmit = 0;

async function setupCamera(video: HTMLVideoElement): Promise<void> {
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { width: 640, height: 480, facingMode: "user" },
    audio: false,
  });
  video.srcObject = stream;
  await video.play();
}

async function setupLandmarker(): Promise<FaceLandmarker> {
  // Assets are bundled with the app rather than fetched from a CDN: the
  // exam client must work on a locked-down network, and a model loaded
  // from the internet at exam time is a model nobody has attested.
  const fileset = await FilesetResolver.forVisionTasks("./models/wasm");
  return FaceLandmarker.createFromOptions(fileset, {
    baseOptions: {
      modelAssetPath: "./models/face_landmarker.task",
      delegate: "GPU",
    },
    runningMode: "VIDEO",
    numFaces: MAX_FACES,
    // Iris landmarks. Without these the gaze estimate degrades to head
    // pose alone, which `estimateGaze` reports at reduced confidence.
    outputFaceBlendshapes: false,
    outputFacialTransformationMatrixes: true,
  });
}

function report(result: FaceLandmarkerResult, timestampMs: number): void {
  if (timestampMs - lastEmit < TELEMETRY_INTERVAL_MS) return;
  lastEmit = timestampMs;

  const faceCount = result.faceLandmarks?.length ?? 0;

  const face: FaceSignal = {
    type: "signal.face",
    face_count: faceCount,
    // Confidence here is about detection quality, not identity. The model
    // gives no per-face score in this mode, so this reflects only whether
    // a usable face was found at all.
    confidence: faceCount > 0 ? 0.9 : 0.6,
  };
  void window.proctor.observe(face);

  if (faceCount === 0) return;

  // The primary face is the first the model returns. When several are
  // present the fusion engine flags the count; picking a "main" candidate
  // out of a crowd is an identity question, handled server-side.
  const landmarks = result.faceLandmarks[0] as Landmark[];
  const matrix = result.facialTransformationMatrixes?.[0]?.data;
  if (!matrix) return;

  const pose = headPoseFromMatrix(Array.from(matrix));
  const headPose: HeadPoseSignal = {
    type: "signal.head_pose",
    yaw_deg: pose.yaw_deg,
    pitch_deg: pose.pitch_deg,
    roll_deg: pose.roll_deg,
    confidence: 0.9,
  };
  void window.proctor.observe(headPose);

  const estimate = estimateGaze(landmarks, pose);
  const gaze: GazeSignal = {
    type: "signal.gaze",
    yaw_deg: estimate.yaw_deg,
    pitch_deg: estimate.pitch_deg,
    on_screen: estimate.on_screen,
    confidence: estimate.confidence,
  };
  void window.proctor.observe(gaze);
}

async function main(): Promise<void> {
  const video = document.querySelector<HTMLVideoElement>("#camera")!;
  const status = document.querySelector<HTMLElement>("#status")!;

  try {
    await setupCamera(video);
    landmarker = await setupLandmarker();
  } catch (error) {
    // The candidate needs to know their session is not being recorded
    // correctly. Failing quietly would leave them to discover it after
    // the exam, when nothing can be done about it.
    status.textContent =
      "Camera or model unavailable. Contact your invigilator before continuing.";
    status.dataset.state = "error";
    console.error(error);
    return;
  }

  status.textContent = "Monitoring active.";
  status.dataset.state = "ok";

  let lastVideoTime = -1;
  const tick = () => {
    if (landmarker && video.currentTime !== lastVideoTime) {
      lastVideoTime = video.currentTime;
      const now = performance.now();
      report(landmarker.detectForVideo(video, now), now);
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

void main();
