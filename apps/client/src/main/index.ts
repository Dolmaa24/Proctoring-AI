/**
 * Electron main process: session lifecycle, OS observation, signed transport.
 *
 * Responsibilities are split so the renderer holds as little power as
 * possible. Main owns the session key, the sequence counter and the
 * socket; the renderer owns the camera and the models and can only hand
 * observations inward over IPC.
 */

import { app, BrowserWindow, ipcMain, screen } from "electron";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import path from "node:path";

import type { Payload } from "../protocol/events.generated.ts";
import { importSessionKey } from "../protocol/signing.ts";
import { readEnvironment } from "./environment.ts";
import { TelemetryClient, type Enrolment } from "./telemetry.ts";

const GATEWAY_URL = process.env.PROCTOR_GATEWAY_URL ?? "http://localhost:8000";
const EXAM_ID = process.env.PROCTOR_EXAM_ID ?? "demo-exam";
const CANDIDATE_REF = process.env.PROCTOR_CANDIDATE_REF ?? "demo-candidate";

const ENVIRONMENT_INTERVAL_MS = 2_000;
const HEARTBEAT_INTERVAL_MS = 1_000;

const dirname = path.dirname(fileURLToPath(import.meta.url));

let window: BrowserWindow | null = null;
let telemetry: TelemetryClient | null = null;
let framesProcessed = 0;
let lastHeartbeatFrames = 0;

async function enrol(): Promise<Enrolment> {
  const response = await fetch(`${GATEWAY_URL}/v1/sessions`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ exam_id: EXAM_ID, candidate_ref: CANDIDATE_REF }),
  });
  if (!response.ok) {
    throw new Error(`enrolment failed: ${response.status} ${await response.text()}`);
  }
  return (await response.json()) as Enrolment;
}

function createWindow(): BrowserWindow {
  const created = new BrowserWindow({
    width: 1100,
    height: 760,
    // Not a lockdown claim — see environment.ts. A visible exam window
    // makes it obvious when the candidate leaves, which is all it is for.
    webPreferences: {
      // `.mjs` because Electron only treats a preload as ESM by extension;
      // scripts/build.mjs renames tsc's output accordingly.
      preload: path.join(dirname, "preload.mjs"),
      contextIsolation: true,
      nodeIntegration: false,
      // Sandboxed preloads must be CommonJS, which rules out the ESM
      // preload above. The boundary that actually matters is preserved:
      // contextIsolation keeps the page off the preload's scope, and
      // nodeIntegration:false keeps Node out of the page. The preload
      // itself exposes exactly one function.
      sandbox: false,
    },
  });
  // Renderer console output is invisible from the terminal otherwise, which
  // makes a failing camera or model load undiagnosable in CI and during an
  // end-to-end run. Opt-in so a real exam session stays quiet.
  if (process.env.PROCTOR_DEBUG) {
    created.webContents.on("console-message", (_event, level, message, line, source) => {
      const where = source ? `${path.basename(source)}:${line}` : "renderer";
      console.log(`[renderer:${level}] ${where} ${message}`);
    });
  }

  created.loadFile(path.join(dirname, "..", "renderer", "index.html"));
  return created;
}

/**
 * What the client claims to be running.
 *
 * A matching hash does not prove an unmodified client — a patched binary
 * can report whatever it likes. It catches the accidental case (a stale
 * build, a swapped model file) and gives review something to check against.
 */
function buildAttestation(): Payload {
  const hash = (value: string) =>
    "sha256:" + createHash("sha256").update(value).digest("hex").slice(0, 16);
  return {
    type: "attestation",
    client_build: app.getVersion(),
    model_hashes: {
      // Replaced with real model file digests once the models are vendored.
      face_landmarker: hash("face_landmarker_v1"),
    },
    platform: `${process.platform}-${process.arch}`,
  };
}

async function startSession(): Promise<void> {
  const enrolment = await enrol();
  telemetry = new TelemetryClient(GATEWAY_URL, enrolment, importSessionKey);
  await telemetry.start();

  await telemetry.emit({ type: "lifecycle", phase: "session_start" });
  await telemetry.emit(buildAttestation());

  // Observations the renderer produces: gaze, head pose, faces, objects.
  // Validated shallowly here — the renderer is the least trusted part of
  // this process tree, so main does not forward arbitrary shapes onward.
  ipcMain.handle("proctor:observe", async (_event, payload: Payload) => {
    if (!telemetry || typeof payload?.type !== "string") return false;
    if (!payload.type.startsWith("signal.")) return false;
    if (payload.type === "signal.environment") {
      // Environment is main's to report. Accepting it from the renderer
      // would let a compromised page claim a clean machine.
      return false;
    }
    framesProcessed += 1;
    await telemetry.emit(payload);
    return true;
  });

  setInterval(async () => {
    if (!telemetry) return;
    const { signal } = await readEnvironment(window);
    await telemetry.emit(signal);
  }, ENVIRONMENT_INTERVAL_MS);

  setInterval(async () => {
    if (!telemetry) return;
    const delta = framesProcessed - lastHeartbeatFrames;
    lastHeartbeatFrames = framesProcessed;
    await telemetry.emit({
      type: "heartbeat",
      frames_processed: framesProcessed,
      edge_fps: delta / (HEARTBEAT_INTERVAL_MS / 1000),
      dropped_frames: telemetry.droppedFrames,
    });
  }, HEARTBEAT_INTERVAL_MS);
}

async function endSession(): Promise<void> {
  if (!telemetry) return;
  await telemetry.emit({ type: "lifecycle", phase: "session_end" });
  await telemetry.close();
  telemetry = null;
}

app.whenReady().then(async () => {
  window = createWindow();

  // A display being attached or removed mid-exam is worth reporting
  // promptly rather than waiting for the next sampling tick.
  screen.on("display-added", () => void reportEnvironmentNow());
  screen.on("display-removed", () => void reportEnvironmentNow());
  window.on("blur", () => void reportEnvironmentNow());
  window.on("focus", () => void reportEnvironmentNow());

  try {
    await startSession();
  } catch (error) {
    console.error("could not start proctoring session:", error);
    app.quit();
  }
});

async function reportEnvironmentNow(): Promise<void> {
  if (!telemetry) return;
  const { signal } = await readEnvironment(window);
  await telemetry.emit(signal);
}

app.on("before-quit", async (event) => {
  if (!telemetry) return;
  // Send the closing lifecycle event before the socket goes away, so the
  // gateway records a clean end rather than raising `stream_abandoned`.
  event.preventDefault();
  await endSession();
  app.exit(0);
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
