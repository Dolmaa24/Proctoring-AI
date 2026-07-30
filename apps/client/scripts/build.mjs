/**
 * Build the Electron app: compile TypeScript, then place renderer assets.
 *
 * Three Electron-specific fixups happen here rather than in tsc:
 *
 * 1. The preload script is renamed to `.mjs`. Electron only treats a
 *    preload as ESM when it carries that extension, and tsc emits `.js`.
 *
 * 2. The vendored models are symlinked rather than copied. They are 37MB
 *    and rebuilding should not move them every time.
 *
 * 3. The inline import map's CSP hash is checked against its actual
 *    content. A hand-maintained hash is a trap: get it wrong and the page
 *    fails almost silently — the import map is refused by CSP, imports
 *    resolve to nothing, and the only trace is a console warning nobody
 *    is watching during a real exam. Failing the build instead of the
 *    session is the whole point of catching it here.
 */

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { copyFile, mkdir, readFile, rename, rm, symlink } from "node:fs/promises";
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

async function checkImportMapHash() {
  const html = await readFile(path.join(srcRenderer, "index.html"), "utf8");
  const startTag = '<script type="importmap">';
  const endTag = "</script>";
  const start = html.indexOf(startTag) + startTag.length;
  const end = html.indexOf(endTag, start);
  const body = html.slice(start, end);
  const digest = createHash("sha256").update(body, "utf8").digest("base64");
  const expected = `sha256-${digest}`;
  if (!html.includes(expected)) {
    console.error(
      `index.html's CSP does not carry ${expected}, the hash of the current\n` +
        "import map contents. Update the 'sha256-...' entry in script-src to that value.",
    );
    process.exit(1);
  }
}

await checkImportMapHash();

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
