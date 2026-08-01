/**
 * End-to-end run: real Electron client, real gateway, synthetic camera.
 *
 * Verifies the whole chain in one go —
 *
 *   camera -> MediaPipe -> renderer -> IPC -> main -> signed WebSocket
 *          -> gateway integrity checks -> fusion engine -> proctor stream
 *
 * The camera is Chromium's fake capture device, so the real getUserMedia
 * path runs (permissions, video element, frame timing) with no webcam and
 * no volunteer. Pass --video <file.y4m> to feed a specific clip.
 *
 *   node scripts/e2e.mjs
 *   node scripts/e2e.mjs --video fixtures/camera.y4m --seconds 12
 *
 * The consent gate is dismissed over the DevTools protocol rather than by
 * teaching the app a "skip consent" env var. That distinction matters:
 * a bypass compiled into the renderer is a bypass that exists in the
 * shipped binary, and the one thing this gate must never have is an
 * off switch reachable from outside the dialog. Driving the real button
 * from outside the process leaves the production path with no such door,
 * and exercises the same code a candidate's click does.
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import WebSocket from "ws";

const clientRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(clientRoot, "..", "..");

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = args.indexOf(name);
  return i > -1 ? args[i + 1] : fallback;
};

const PORT = Number(flag("--port", "8099"));
const SECONDS = Number(flag("--seconds", "10"));
const VIDEO = flag("--video", null);
const CDP_PORT = Number(flag("--cdp-port", "9222"));
const GATEWAY = `http://localhost:${PORT}`;
const CONSOLE_TOKEN = "e2e-console-token";

const observed = { signals: new Map(), violations: [], sessions: new Set() };
let consentAccepted = false;
let gateway;
let electron;

function note(kind) {
  observed.signals.set(kind, (observed.signals.get(kind) ?? 0) + 1);
}

async function startGateway() {
  const proc = spawn(
    path.join(repoRoot, ".venv", "bin", "uvicorn"),
    ["proctor_gateway.app:app", "--port", String(PORT), "--log-level", "warning"],
    {
      cwd: repoRoot,
      env: {
        ...process.env,
        PYTHONPATH: "python",
        PROCTOR_MASTER_SECRET: "e2e-secret",
        PROCTOR_CONSOLE_TOKEN: CONSOLE_TOKEN,
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  proc.stderr.on("data", (chunk) => {
    const text = chunk.toString();
    if (text.includes("Traceback")) process.stderr.write(`[gateway] ${text}`);
  });

  for (let attempt = 0; attempt < 50; attempt++) {
    try {
      const response = await fetch(`${GATEWAY}/health`);
      if (response.ok) return proc;
    } catch {
      /* not up yet */
    }
    await delay(200);
  }
  throw new Error("gateway did not become healthy");
}

function watchProctor() {
  // The proctor stream fails closed (see config.py's `console_token`) —
  // it carries every candidate's flags, so an unauthenticated watcher
  // gets rejected rather than let through.
  const socket = new WebSocket(`ws://localhost:${PORT}/v1/proctor/stream`, [
    "proctor.console.v1",
    `token.${CONSOLE_TOKEN}`,
  ]);
  socket.on("message", (raw) => {
    const message = JSON.parse(raw.toString());
    if (message.kind === "violation") observed.violations.push(message);
    if (message.session_id) observed.sessions.add(message.session_id);
  });
  // An unhandled 'error' event crashes the whole Node process, skipping
  // the try/catch below (it fires async, after that block has returned)
  // and orphaning the spawned gateway subprocess to sit on this port
  // forever. A rejected connection here is a bug to report, not a reason
  // to leak a process.
  socket.on("error", (error) => {
    console.error("proctor stream connection failed:", error.message);
  });
  return socket;
}

function startElectron() {
  const electronBinary = require_electron();
  const chromiumFlags = [
    "--use-fake-ui-for-media-stream", // auto-grant the camera prompt
    "--use-fake-device-for-media-stream",
    // Test-harness only, and deliberately not a launch flag the app sets
    // for itself: this opens a debugging surface that can run arbitrary
    // script in the renderer, which is exactly what the lockdown module
    // exists to keep a candidate away from.
    `--remote-debugging-port=${CDP_PORT}`,
    // The detection loop runs on requestAnimationFrame, which Chromium
    // throttles hard for windows it considers hidden. One run of this
    // script produced a single face sample in twelve seconds because of
    // it — with no human at the keyboard the window does not reliably
    // stay frontmost, especially now that consent takes it fullscreen
    // and macOS moves it to its own Space.
    //
    // These are Chromium's standard flags for that, but be careful how
    // much credit you give them: removing them again did *not* reproduce
    // the freeze, so the trigger is something focus-dependent and
    // intermittent that they mitigate rather than eliminate. The rate
    // floor in report() is the part that actually protects this script —
    // it turns a stalled pipeline into a failure instead of a pass.
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-background-timer-throttling",
  ];
  if (VIDEO) {
    // Both flags: the file flag selects the *content*, the device flag
    // still has to select the fake capture device itself.
    chromiumFlags.push(`--use-file-for-fake-video-capture=${path.resolve(clientRoot, VIDEO)}`);
  }

  const proc = spawn(electronBinary, [clientRoot, ...chromiumFlags], {
    cwd: clientRoot,
    env: { ...process.env, PROCTOR_GATEWAY_URL: GATEWAY, PROCTOR_EXAM_ID: "e2e" },
    stdio: ["ignore", "pipe", "pipe"],
  });

  const relay = (stream, label) =>
    stream.on("data", (chunk) => {
      for (const line of chunk.toString().split("\n")) {
        if (!line.trim()) continue;
        // Electron is noisy on stderr about unrelated system frameworks.
        if (/^\[\d+:\d+/.test(line) || line.includes("IMKClient")) continue;
        console.log(`  [${label}] ${line}`);
      }
    });
  relay(proc.stdout, "electron");
  relay(proc.stderr, "electron");
  return proc;
}

function require_electron() {
  const candidate = path.join(clientRoot, "node_modules", ".bin", "electron");
  if (!existsSync(candidate)) throw new Error("electron is not installed; run npm install");
  return candidate;
}

/**
 * Click the real consent button in the renderer, over CDP.
 *
 * `userGesture: true` is not incidental. `requestFullscreen()` requires
 * user activation, so without it the click would dismiss the dialog but
 * the fullscreen call behind it would reject — and the run would quietly
 * exercise a path no candidate ever takes.
 */
function clickConsent(wsUrl) {
  return new Promise((resolve) => {
    const socket = new WebSocket(wsUrl);
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      socket.close();
      resolve(value);
    };

    socket.on("open", () => {
      socket.send(
        JSON.stringify({
          id: 1,
          method: "Runtime.evaluate",
          params: {
            expression:
              '(() => { const b = document.querySelector(".consent-accept");' +
              " if (!b) return false; b.click(); return true; })()",
            returnByValue: true,
            userGesture: true,
          },
        }),
      );
    });
    socket.on("message", (raw) => {
      const message = JSON.parse(raw.toString());
      if (message.id === 1) done(message.result?.result?.value === true);
    });
    socket.on("error", () => done(false));
    setTimeout(() => done(false), 3_000);
  });
}

/**
 * Wait for the renderer to reach the consent dialog, then accept it.
 *
 * Polls rather than waiting a fixed interval: the dialog only appears
 * after the media-config round trip to the gateway resolves, and how long
 * that takes is not something this script should be guessing at.
 */
async function acceptConsent() {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`);
      const targets = await response.json();
      const page = targets.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (page && (await clickConsent(page.webSocketDebuggerUrl))) return true;
    } catch {
      // CDP endpoint not listening yet, or the page is still loading.
    }
    await delay(300);
  }
  return false;
}

async function pollSession() {
  for (const sessionId of observed.sessions) {
    const response = await fetch(`${GATEWAY}/v1/sessions/${sessionId}`);
    if (response.ok) return response.json();
  }
  return null;
}

function report(status) {
  const checks = [];
  const add = (ok, label, detail = "") => checks.push({ ok, label, detail });

  // Checked first, and on its own line, because everything downstream
  // depends on it: if the gate never cleared, the camera never opened,
  // and every capture-related check below fails for that one reason
  // rather than for anything it claims to be testing.
  add(consentAccepted, "consent gate appeared and was accepted");

  const received = status?.events_received ?? 0;
  add(received > 0, "telemetry reached the gateway", `${received} events`);
  add(
    status?.integrity_breaches?.length === 0,
    "no integrity breaches on an honest client",
    JSON.stringify(status?.integrity_breaches ?? []).slice(0, 160),
  );
  add(
    status?.last_seq === received - 1,
    "sequence is contiguous",
    `last_seq=${status?.last_seq}`,
  );
  add(Boolean(status?.attested_build), "client attested its build", status?.attested_build ?? "");

  // A floor, not merely "more than zero". An earlier version of this
  // script reported 11/11 PASS while the detection loop was frozen and
  // had emitted a single face sample in twelve seconds: every check was
  // satisfied by that one stray frame. Telemetry is downsampled to 10Hz,
  // so ~2/s is far below what a working pipeline produces and far above
  // what a stalled one can fake.
  const floor = Math.max(4, SECONDS * 2);
  const faces = observed.signals.get("signal.face") ?? 0;
  add(
    faces >= floor,
    `renderer ran MediaPipe at a plausible rate (>=${floor} face signals)`,
    `${faces} samples`,
  );
  add(observed.signals.has("signal.environment"), "main produced OS environment signals");
  add(observed.signals.has("heartbeat"), "heartbeats flowed");
  add(
    observed.violations.some((v) => v.rule_id === "blacklisted_process") === false,
    "no blacklisted-process false positive on this machine",
  );

  // The face-present branch only runs when the feed actually contains a
  // detectable face. Asserting it unconditionally would make the faceless
  // fake-device run fail for the right behaviour.
  const facePresent = observed.signals.has("signal.head_pose");
  if (facePresent) {
    const gaze = observed.signals.get("signal.gaze") ?? 0;
    const pose = observed.signals.get("signal.head_pose") ?? 0;
    add(gaze >= floor, `gaze derived from iris landmarks (>=${floor})`, `${gaze} samples`);
    add(
      pose >= floor,
      `head pose decoded from the transformation matrix (>=${floor})`,
      `${pose} samples`,
    );
  } else {
    add(
      observed.violations.some((v) => v.rule_id === "candidate_absent"),
      "faceless feed raised candidate_absent through the fusion engine",
    );
    console.log(
      "\n  note: no face detected in this feed, so the head-pose/gaze branch\n" +
        "  did not run. Use --video fixtures/face.y4m to exercise it.",
    );
  }

  console.log("\n──────── end-to-end result ────────");
  for (const check of checks) {
    console.log(`  ${check.ok ? "PASS" : "FAIL"}  ${check.label}${check.detail ? `  (${check.detail})` : ""}`);
  }

  console.log("\n  signals observed by the gateway:");
  for (const [kind, count] of [...observed.signals].sort()) {
    console.log(`    ${String(count).padStart(5)}  ${kind}`);
  }

  const opened = observed.violations.filter((v) => !v.resolved);
  console.log(`\n  violations raised: ${opened.length}`);
  for (const violation of opened.slice(0, 10)) {
    console.log(
      `    ${violation.severity.padEnd(5)} ${violation.rule_id.padEnd(22)} ` +
        `${String(violation.duration_ms).padStart(6)}ms  evidence=${violation.evidence.length}`,
    );
  }

  return checks.every((check) => check.ok);
}

function collectSignalKinds(status) {
  for (const [kind, count] of Object.entries(status?.signal_counts ?? {})) {
    observed.signals.set(kind, count);
  }
}

try {
  console.log(`starting gateway on :${PORT}…`);
  gateway = await startGateway();
  const proctor = watchProctor();
  await delay(300);

  console.log(`launching electron (${VIDEO ? `video: ${VIDEO}` : "fake device"})…`);
  electron = startElectron();

  console.log("waiting for the consent gate…");
  consentAccepted = await acceptConsent();
  console.log(
    consentAccepted
      ? "  consent accepted; capture starts now"
      : "  WARNING: consent gate never appeared or could not be clicked",
  );

  // Timed from *after* consent, not from launch: nothing is captured
  // before the gate clears, so starting the clock at launch would spend
  // most of a short run watching a modal.
  console.log(`observing for ${SECONDS}s…\n`);
  await delay(SECONDS * 1000);

  const status = await pollSession();
  collectSignalKinds(status);

  electron.kill("SIGTERM");
  await delay(1200);
  proctor.close();

  const ok = report(status);
  gateway.kill("SIGTERM");
  process.exit(ok ? 0 : 1);
} catch (error) {
  console.error("e2e run failed:", error);
  electron?.kill("SIGKILL");
  gateway?.kill("SIGKILL");
  process.exit(1);
}
