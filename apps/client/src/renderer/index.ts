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
 *
 * The candidate-facing toasts here are the one place this file gets close
 * to deciding something, and they deliberately stop short: they warn
 * ("a phone is visible") so the candidate can fix it, while the question
 * of whether that becomes a flag stays entirely server-side. See toasts.ts.
 */

import {
  FaceLandmarker,
  FilesetResolver,
  type FaceLandmarkerResult,
  type ObjectDetector,
} from "@mediapipe/tasks-vision";

import type {
  FaceSignal,
  FrameQualitySignal,
  GazeSignal,
  HeadPoseSignal,
} from "../protocol/events.generated.ts";
import { enterFullscreen, requestConsent } from "./consent.ts";
import { estimateGaze, headPoseFromMatrix, type Landmark } from "./gaze.ts";
import { StrikeCounter, classifyKey, strikeMessage } from "./lockdown.ts";
import { joinMediaRoom } from "./media.ts";
import { createObjectDetector, detectionsToSignals, personCount } from "./objects.ts";
import { frameQuality, isPoorlyLit } from "./quality.ts";
import { TOASTS, ToastHost } from "./toasts.ts";
import type { Room } from "livekit-client";

const TELEMETRY_INTERVAL_MS = 100;
/** Object detection is far heavier than landmarking; running it every frame
 * would starve the gaze pipeline for no benefit. A phone does not appear
 * and vanish inside 500ms, and the policy's 800ms onset window is longer
 * than this interval either way. */
const OBJECT_INTERVAL_MS = 500;
const QUALITY_INTERVAL_MS = 1_000;
const MAX_FACES = 4;
const QUALITY_SAMPLE_EDGE = 128;

/** Below this the frame is reported as too blurred to read a face from.
 * The server decides what to do about it; this only drives the local
 * warning toast. */
const BLUR_TOAST_THRESHOLD = 0.06;

let landmarker: FaceLandmarker | null = null;
let detector: ObjectDetector | null = null;
let lastEmit = 0;
let lastObjectRun = 0;
let lastQualityRun = 0;
let mediaRoom: Room | null = null;
let toasts: ToastHost | null = null;

async function setupCamera(video: HTMLVideoElement): Promise<MediaStream> {
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { width: 640, height: 480, facingMode: "user" },
    audio: false,
  });
  video.srcObject = stream;
  await video.play();
  return stream;
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

  if (faceCount === 0) {
    toasts?.show(TOASTS.absent);
    return;
  }
  if (faceCount > 1) toasts?.show(TOASTS.multipleFaces);

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

function reportObjects(video: HTMLVideoElement, now: number): void {
  if (!detector || now - lastObjectRun < OBJECT_INTERVAL_MS) return;
  lastObjectRun = now;

  const result = detector.detectForVideo(video, now);
  const detections = result.detections ?? [];
  const frame = { width: video.videoWidth, height: video.videoHeight };

  for (const signal of detectionsToSignals(detections, frame)) {
    // `person` is reported for the count, but the candidate is themselves
    // a person: only warn once a *second* one is in frame. The server
    // applies the same reasoning in `second_person_detected`.
    if (signal.label === "person") {
      if (personCount(detections) > 1) toasts?.show(TOASTS.secondPerson);
    } else if (signal.label === "phone") {
      toasts?.show(TOASTS.phone);
    } else if (signal.label === "book") {
      toasts?.show(TOASTS.book);
    }
    void window.proctor.observe(signal);
  }
}

function reportQuality(video: HTMLVideoElement, canvas: HTMLCanvasElement, now: number): void {
  if (now - lastQualityRun < QUALITY_INTERVAL_MS) return;
  lastQualityRun = now;
  if (!video.videoWidth) return;

  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return;

  // Downsampled hard: variance-of-Laplacian over a full 640x480 frame every
  // second is wasted work for a number that only ever gets compared to a
  // threshold, and this runs on the same thread as the gaze pipeline.
  const scale = QUALITY_SAMPLE_EDGE / Math.max(video.videoWidth, video.videoHeight);
  canvas.width = Math.max(3, Math.round(video.videoWidth * scale));
  canvas.height = Math.max(3, Math.round(video.videoHeight * scale));
  context.drawImage(video, 0, 0, canvas.width, canvas.height);

  const { data } = context.getImageData(0, 0, canvas.width, canvas.height);
  const { sharpness, brightness } = frameQuality(data, canvas.width, canvas.height);

  const signal: FrameQualitySignal = {
    type: "signal.frame_quality",
    sharpness,
    brightness,
    face_covered: sharpness < BLUR_TOAST_THRESHOLD,
    confidence: 1,
  };
  void window.proctor.observe(signal);

  if (sharpness < BLUR_TOAST_THRESHOLD) toasts?.show(TOASTS.blurry);
  if (isPoorlyLit(brightness)) toasts?.show(TOASTS.poorLight);
}

/**
 * Block restricted keys and count strikes.
 *
 * `preventDefault` stops the casual and the reflexive, not the determined
 * — see lockdown.ts. Every attempt is reported regardless of whether the
 * block succeeded, because the attempt is the observation.
 */
function installLockdown(): void {
  const counter = new StrikeCounter();

  window.addEventListener(
    "keydown",
    (event) => {
      const restricted = classifyKey(event);
      if (!restricted) return;

      event.preventDefault();
      event.stopPropagation();

      const { signal, state } = counter.record(restricted);
      void window.proctor.observe(signal);

      const message = strikeMessage(state);
      toasts?.show({ key: `strike-${state.strike}`, tone: "strike", ...message });

      if (restricted.event === "fullscreen_exit") void enterFullscreen();
    },
    // Capture phase: a handler on the document or an embedded exam page
    // must not be able to swallow the event before this sees it.
    { capture: true },
  );

  document.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    const { signal, state } = counter.record({
      event: "context_menu",
      detail: "right-click — context menu",
    });
    void window.proctor.observe(signal);
    toasts?.show({
      key: `strike-${state.strike}`,
      tone: "strike",
      ...strikeMessage(state),
    });
  });

  // The browser drops fullscreen for reasons other than a keypress (an OS
  // dialog, a display change). Re-entering is best-effort; the durable
  // record of the window state is `signal.environment` from the main
  // process, which does not depend on this listener firing at all.
  document.addEventListener("fullscreenchange", () => {
    if (!document.fullscreenElement) void enterFullscreen();
  });
}

async function main(): Promise<void> {
  const video = document.querySelector<HTMLVideoElement>("#camera")!;
  const status = document.querySelector<HTMLElement>("#status")!;
  toasts = new ToastHost(document.querySelector<HTMLElement>("#toasts")!);

  const media = await window.proctor.getMediaJoin();

  // Consent first, before the camera is ever opened. A disclaimer shown
  // while the webcam light is already on is notification, not consent.
  await requestConsent({
    recording: media !== null,
    microphone: media !== null,
    retentionDays: 14,
    allowance: 3,
  });

  await window.proctor.grantConsent();
  await enterFullscreen();
  installLockdown();

  let stream: MediaStream;
  try {
    stream = await setupCamera(video);
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

  // Object detection is additive: a failure here must not take the gaze
  // and face pipeline down with it, since those carry most of the policy.
  try {
    detector = await createObjectDetector();
  } catch (error) {
    console.error("object detector unavailable; phone/person rules will not fire:", error);
  }

  status.textContent = "Monitoring active.";
  status.dataset.state = "ok";

  // Not load-bearing for monitoring: MediaPipe's observations keep
  // flowing over the signed telemetry path regardless of whether the
  // live video call could be joined. See media.ts.
  const videoTrack = stream.getVideoTracks()[0];
  if (videoTrack) {
    joinMediaRoom(videoTrack)
      .then((room) => {
        mediaRoom = room;
      })
      .catch((error: unknown) => {
        console.error("media room join failed:", error);
      });
  }
  window.addEventListener("beforeunload", () => {
    void mediaRoom?.disconnect();
  });

  const qualityCanvas = document.createElement("canvas");
  let lastVideoTime = -1;
  const tick = () => {
    if (landmarker && video.currentTime !== lastVideoTime) {
      lastVideoTime = video.currentTime;
      const now = performance.now();
      report(landmarker.detectForVideo(video, now), now);
      reportObjects(video, now);
      reportQuality(video, qualityCanvas, now);
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

void main();
