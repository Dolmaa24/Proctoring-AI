/**
 * Build the Electron app: compile TypeScript, then place renderer assets.
 *
 * Two Electron-specific fixups happen here rather than in tsc:
 *
 * 1. The preload script is renamed to `.mjs`. Electron only treats a
 *    preload as ESM when it carries that extension, and tsc emits `.js`.
 *
 * 2. The vendored models are symlinked rather than copied. They are 37MB
 *    and rebuilding should not move them every time.
 */

import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { copyFile, mkdir, rename, rm, symlink } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const clientRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dist = path.join(clientRoot, "dist");
const srcRenderer = path.join(clientRoot, "src", "renderer");
const distRenderer = path.join(dist, "renderer");

const models = path.join(srcRenderer, "models");
if (!existsSync(path.join(models, "face_landmarker.task"))) {
  console.error(
    "MediaPipe assets are missing. Run: node scripts/vendor-assets.mjs",
  );
  process.exit(1);
}

console.log("compiling typescript…");
execFileSync("npx", ["tsc"], { cwd: clientRoot, stdio: "inherit" });

await mkdir(distRenderer, { recursive: true });
await copyFile(path.join(srcRenderer, "index.html"), path.join(distRenderer, "index.html"));

const preloadJs = path.join(dist, "main", "preload.js");
if (existsSync(preloadJs)) {
  await rename(preloadJs, path.join(dist, "main", "preload.mjs"));
}

const modelLink = path.join(distRenderer, "models");
await rm(modelLink, { recursive: true, force: true });
await symlink(models, modelLink, "dir");

console.log("build complete: dist/");
