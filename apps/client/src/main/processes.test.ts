/**
 * Run: node --experimental-strip-types --test apps/client/src/main/processes.test.ts
 *
 * The false-positive tests matter more than the detection tests. A missed
 * screen-sharing app is a gap; a false hit is a real person accused of
 * cheating because of what their operating system happens to run.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BLACKLISTED_NAMES,
  findBlacklisted,
  parsePosixProcessList,
  parseWindowsProcessList,
  suggestsScreenShare,
} from "./processes.ts";

test("detects screen sharing and remote control tools", () => {
  const running = ["Finder", "obs64", "Google Chrome", "AnyDesk", "node"];
  assert.deepEqual(findBlacklisted(running), ["AnyDesk", "obs64"]);
});

test("matches case-insensitively and ignores the directory path", () => {
  const running = ["/Applications/AnyDesk.app/Contents/MacOS/AnyDesk"];
  assert.equal(findBlacklisted(running).length, 1);
});

test("strips the .exe suffix so one list covers every platform", () => {
  assert.deepEqual(findBlacklisted(["AnyDesk.exe"]), ["AnyDesk.exe"]);
  assert.deepEqual(findBlacklisted(["obs64.exe"]), ["obs64.exe"]);
});

test("does not flag macOS system daemons", () => {
  // Regression. The first version of this matched the substring "parsec"
  // to catch the game-streaming app, and hit Apple's CoreParsec daemons —
  // the Spotlight suggestions service, running on every Mac. It was found
  // by an end-to-end run on a developer laptop, not by this file, because
  // the earlier tests only used names someone had thought of.
  const appleDaemons = [
    "/System/Library/PrivateFrameworks/CoreParsec.framework/parsecd",
    "/System/Library/PrivateFrameworks/CoreParsec.framework/parsec-fbf",
    "/usr/libexec/opendirectoryd",
    "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder",
  ];
  assert.deepEqual(findBlacklisted(appleDaemons), []);
  assert.equal(suggestsScreenShare(findBlacklisted(appleDaemons)), false);
});

test("does not flag windows system processes", () => {
  const windows = ["C:\\Windows\\System32\\svchost.exe", "C:\\Windows\\explorer.exe"];
  assert.deepEqual(findBlacklisted(windows), []);
});

test("still flags a user-installed copy of a colliding name", () => {
  // Parsec proper lives in /Applications, not /System. Excluding vendor
  // paths must not blind the check to the app it was written for.
  const running = ["/Applications/Parsec.app/Contents/MacOS/parsecd"];
  assert.deepEqual(findBlacklisted(running), [
    "/Applications/Parsec.app/Contents/MacOS/parsecd",
  ]);
});

test("does not flag ordinary software with colliding names", () => {
  // Substring matching made every one of these a false positive: "obs"
  // matches Obsidian and observer-daemon; "team" matches Microsoft Teams,
  // which is not remote-control software.
  const innocent = [
    "Obsidian",
    "observer-daemon",
    "Microsoft Teams",
    "teams-helper",
    "com.apple.WebKit.WebContent",
    "Slack",
    "zoom.us",
    "vncthing-unrelated",
    "my-obs-notes",
  ];
  assert.deepEqual(findBlacklisted(innocent), []);
});

test("ignores blank and whitespace-only entries", () => {
  assert.deepEqual(findBlacklisted(["", "   ", "\t"]), []);
});

test("deduplicates repeated matches of the same process", () => {
  assert.deepEqual(findBlacklisted(["obs64", "obs64", "obs64"]), ["obs64"]);
});

test("parses windows tasklist csv output", () => {
  const stdout = [
    '"System Idle Process","0","Services","0","8 K"',
    '"AnyDesk.exe","4242","Console","1","24,132 K"',
    "",
  ].join("\n");
  const names = parseWindowsProcessList(stdout);
  assert.deepEqual(names, ["System Idle Process", "AnyDesk.exe"]);
  assert.deepEqual(findBlacklisted(names), ["AnyDesk.exe"]);
});

test("parses posix ps output", () => {
  const stdout = "/usr/sbin/cfprefsd\n/Applications/obs64\n\n  \n";
  const names = parsePosixProcessList(stdout);
  assert.deepEqual(names, ["/usr/sbin/cfprefsd", "/Applications/obs64"]);
  assert.deepEqual(findBlacklisted(names), ["/Applications/obs64"]);
});

test("screen share inference stays narrow", () => {
  assert.equal(suggestsScreenShare(["obs64"]), true);
  assert.equal(suggestsScreenShare(["AnyDesk"]), false, "remote control is not capture");
  assert.equal(suggestsScreenShare([]), false);
});

test("every blacklist entry is a complete executable name", () => {
  // Guards the rule that produced the CoreParsec incident: fragments in
  // this list are what collide with unrelated system binaries.
  for (const name of BLACKLISTED_NAMES) {
    assert.equal(name, name.toLowerCase(), `${name} must be lowercase`);
    assert.ok(!name.endsWith(".exe"), `${name} must not carry an extension`);
    assert.ok(!name.includes(" "), `${name} must be a basename, not a phrase`);
    assert.ok(name.length >= 3, `${name} is too short to be specific`);
  }
});

test("the real process table on this machine is clean", async () => {
  // The check that would have caught CoreParsec. Runs against whatever is
  // actually running here: a developer laptop is a realistic sample of the
  // system noise a candidate's machine produces, and this is the cheapest
  // possible guard against the next vendor-daemon collision.
  const { execFile } = await import("node:child_process");
  const { promisify } = await import("node:util");
  if (process.platform === "win32") return;

  const { stdout } = await promisify(execFile)("ps", ["-Ao", "comm="]);
  const hits = findBlacklisted(parsePosixProcessList(stdout));
  assert.deepEqual(
    hits,
    [],
    `flagged on a machine with no proctoring-relevant software: ${hits.join(", ")}`,
  );
});
