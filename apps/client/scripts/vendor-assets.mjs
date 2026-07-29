/**
 * Vendor the MediaPipe runtime and model into the app bundle.
 *
 * The exam client must work on a locked-down network, and a model fetched
 * from the internet at exam time is a model nobody has attested. Both the
 * WASM runtime and the .task file are therefore shipped inside the app.
 *
 * The WASM comes from the installed npm package (no network). The model is
 * downloaded once from Google's official MediaPipe host and its digest is
 * recorded, so a changed file is visible rather than silent.
 *
 *   node scripts/vendor-assets.mjs
 *   node scripts/vendor-assets.mjs --verify   # digests only, no download
 */

import { createHash } from "node:crypto";
import { mkdir, copyFile, readFile, writeFile, readdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const clientRoot = path.resolve(here, "..");
const vendorRoot = path.join(clientRoot, "src", "renderer", "models");
const wasmSource = path.join(clientRoot, "node_modules", "@mediapipe", "tasks-vision", "wasm");
const bundleSource = path.join(
  clientRoot,
  "node_modules",
  "@mediapipe",
  "tasks-vision",
  "vision_bundle.mjs",
);

const MODEL = {
  name: "face_landmarker.task",
  // Float16 face landmarker with iris refinement — the iris points are what
  // make gaze estimation possible at all; the 468-point model has none.
  url: "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
};

const MANIFEST = path.join(vendorRoot, "manifest.json");

async function sha256(file) {
  return "sha256:" + createHash("sha256").update(await readFile(file)).digest("hex");
}

async function vendorWasm() {
  await mkdir(path.join(vendorRoot, "wasm"), { recursive: true });
  const files = await readdir(wasmSource);
  for (const file of files) {
    await copyFile(path.join(wasmSource, file), path.join(vendorRoot, "wasm", file));
  }
  // The ESM bundle is copied alongside so the renderer can import it by
  // relative path. Loaded over file://, a bare specifier does not resolve
  // and there is no bundler in this build.
  await copyFile(bundleSource, path.join(vendorRoot, "vision_bundle.mjs"));
  return files.length + 1;
}

async function vendorModel() {
  const target = path.join(vendorRoot, MODEL.name);
  if (existsSync(target)) {
    console.log(`  ${MODEL.name} already present, skipping download`);
    return target;
  }
  console.log(`  downloading ${MODEL.name} from ${new URL(MODEL.url).host}`);
  const response = await fetch(MODEL.url);
  if (!response.ok) {
    throw new Error(`model download failed: ${response.status} ${response.statusText}`);
  }
  const bytes = Buffer.from(await response.arrayBuffer());
  await writeFile(target, bytes);
  console.log(`  wrote ${(bytes.length / 1e6).toFixed(1)} MB`);
  return target;
}

async function writeManifest() {
  const model = path.join(vendorRoot, MODEL.name);
  const manifest = {
    generated: new Date().toISOString(),
    // Recorded so the client can attest what it is running, and so a
    // swapped model file is detectable rather than silent.
    assets: {
      [MODEL.name]: { source: MODEL.url, digest: await sha256(model) },
      "vision_bundle.mjs": {
        source: "@mediapipe/tasks-vision (npm)",
        digest: await sha256(path.join(vendorRoot, "vision_bundle.mjs")),
      },
      "wasm/vision_wasm_internal.wasm": {
        source: "@mediapipe/tasks-vision (npm)",
        digest: await sha256(path.join(vendorRoot, "wasm", "vision_wasm_internal.wasm")),
      },
    },
  };
  await writeFile(MANIFEST, JSON.stringify(manifest, null, 2) + "\n");
  return manifest;
}

const verifyOnly = process.argv.includes("--verify");

if (verifyOnly) {
  if (!existsSync(MANIFEST)) {
    console.error("no manifest; run: node scripts/vendor-assets.mjs");
    process.exit(1);
  }
  const recorded = JSON.parse(await readFile(MANIFEST, "utf8"));
  let ok = true;
  for (const [name, entry] of Object.entries(recorded.assets)) {
    const actual = await sha256(path.join(vendorRoot, name));
    const match = actual === entry.digest;
    ok &&= match;
    console.log(`${match ? "ok  " : "FAIL"} ${name} ${actual.slice(0, 23)}`);
  }
  process.exit(ok ? 0 : 1);
}

await mkdir(vendorRoot, { recursive: true });
console.log("vendoring MediaPipe assets:");
const copied = await vendorWasm();
console.log(`  copied ${copied} runtime files from node_modules`);
await vendorModel();
const manifest = await writeManifest();
console.log("\ndigests:");
for (const [name, entry] of Object.entries(manifest.assets)) {
  console.log(`  ${name}  ${entry.digest.slice(0, 23)}…`);
}
